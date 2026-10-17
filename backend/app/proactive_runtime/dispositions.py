"""Body-free ACK decisions in the caller's original SQLite transaction.

These inputs preserve original source identity after the full snapshot expires.
They grant no current source permission and never perform source I/O or commit.
"""
import hashlib
import json
import math

from .config import SourceGrant


_MAX_BYTES = 32 * 1024
_FIELDS = {"version", "roleId", "tickId", "intentId", "sourceGrants", "items",
           "interesting", "discarded", "evidence", "alertIds"}
_ITEM_FIELDS = {"sourceId", "sourceRevision", "eventDomain", "remoteId", "kind"}


def _text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _identifier(value, *, empty=False):
    if not isinstance(value, str) or len(value) > 256 or (not empty and not value):
        raise ValueError("invalid disposition identity")
    return value


def _identifiers(value, items, *, content_only=False):
    if (not isinstance(value, list) or len(value) > 25
            or any(not isinstance(identifier, str) for identifier in value)
            or len(set(value)) != len(value)):
        raise ValueError("invalid disposition item collection")
    if any(identifier not in items or (content_only and items[identifier]["kind"] != "content")
           for identifier in value):
        raise ValueError("disposition references an item outside the original snapshot")
    return sorted(value)


def _validate(value):
    if not isinstance(value, dict) or set(value) != _FIELDS or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unsupported disposition inputs")
    for field in ("roleId", "tickId", "intentId"):
        _identifier(value[field], empty=field == "intentId")
    raw_grants = value["sourceGrants"]
    if not isinstance(raw_grants, list) or len(raw_grants) > 8:
        raise ValueError("disposition source limit exceeded")
    grants = {}
    for raw in raw_grants:
        source = SourceGrant.model_validate(raw)
        if source.sourceId in grants:
            raise ValueError("duplicate disposition source identity")
        grants[source.sourceId] = source
    items = value["items"]
    if not isinstance(items, dict) or len(items) > 25:
        raise ValueError("disposition item limit exceeded")
    counts = {"alert": 0, "content": 0}
    for identifier, item in items.items():
        _identifier(identifier)
        if not isinstance(item, dict) or set(item) != _ITEM_FIELDS:
            raise ValueError("invalid body-free disposition item")
        grant = grants.get(item["sourceId"]) if isinstance(item["sourceId"], str) else None
        if (grant is None or not grant.enabled or type(item["sourceRevision"]) is not int
                or item["sourceRevision"] != grant.revision or item["eventDomain"] != grant.eventDomain
                or not isinstance(item["kind"], str) or item["kind"] not in counts
                or item["kind"] != grant.channel):
            raise ValueError("disposition item differs from its original source grant")
        _identifier(item["remoteId"])
        counts[item["kind"]] += 1
    if counts["alert"] > 20 or counts["content"] > 5:
        raise ValueError("disposition snapshot limit exceeded")
    for field in ("interesting", "discarded", "evidence", "alertIds"):
        _identifiers(value[field], items, content_only=field in {"interesting", "discarded"})
    if set(value["alertIds"]) != {identifier for identifier, item in items.items() if item["kind"] == "alert"}:
        raise ValueError("disposition alert membership differs from the original snapshot")
    return grants


def freeze_inputs(turn, db, intent_id, draft=None) -> str:
    """Project the original host snapshot; refuse truncation before dispatch."""
    items, alerts = {}, []
    for field, kind, maximum in (("alerts", "alert", 20), ("contents", "content", 5)):
        collection = turn.snapshot[field]
        if not isinstance(collection, list) or len(collection) > maximum:
            raise ValueError("disposition snapshot limit exceeded")
        for item in collection:
            if not isinstance(item, dict) or item.get("kind") != kind:
                raise ValueError("disposition item differs from its snapshot collection")
            identifier = _identifier(item.get("id"))
            if identifier in items:
                raise ValueError("duplicate disposition item identity")
            try:
                items[identifier] = {key: item[key] for key in _ITEM_FIELDS}
            except KeyError:
                raise ValueError("disposition item lacks original source identity") from None
            if kind == "alert":
                alerts.append(identifier)
    state = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='autonomy_turns'").fetchone():
        state = db.execute("SELECT interesting,discarded FROM autonomy_turns WHERE tick_id=?",
                           (turn.origin.tick_id,)).fetchone()
    if state is None and items:
        raise ValueError("nonempty disposition snapshot lacks its original classification")
    if draft is not None and not isinstance(draft, dict):
        raise ValueError("invalid original disposition draft")
    value = {"version": 1, "roleId": turn.origin.role_id, "tickId": turn.origin.tick_id,
             "intentId": intent_id,
             "sourceGrants": sorted((grant.model_dump(mode="json") for grant in turn.config.sourceGrants),
                                    key=lambda grant: grant["sourceId"]),
             "items": items, "interesting": json.loads(state[0]) if state else [],
             "discarded": json.loads(state[1]) if state else [],
             "evidence": draft.get("evidence", []) if draft else [], "alertIds": alerts}
    _validate(value)
    for field in ("interesting", "discarded", "evidence", "alertIds"):
        value[field] = sorted(value[field])
    encoded = _text(value)
    if len(encoded.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("disposition inputs exceed 32 KiB UTF-8")
    return encoded


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate persisted disposition field")
        result[key] = value
    return result


def plan_from_inputs(app, db, encoded: str, outcome: str, *, decision_at: float | None = None) -> float:
    """Apply the original choice precedence, freezing each original ACK once."""
    if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("disposition inputs exceed 32 KiB UTF-8")
    value = json.loads(encoded, object_pairs_hook=_unique_object)
    grants = _validate(value)
    if outcome not in {"sent", "duplicate", "failed", "skip"}:
        raise ValueError("outcome does not establish an ACK disposition")
    now = app._now(db) if decision_at is None else decision_at
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValueError("finite host UTC timestamp required")
    try:
        now = float(now)
    except OverflowError:
        raise ValueError("finite host UTC timestamp required") from None
    if not math.isfinite(now):
        raise ValueError("finite host UTC timestamp required")
    discarded, interesting = set(value["discarded"]), set(value["interesting"])
    evidence = set(value["evidence"])
    choices = {identifier: (720, "discarded") for identifier in sorted(discarded)}
    if outcome in {"sent", "duplicate"}:
        choices.update({identifier: (24, "interesting") for identifier in sorted(interesting - evidence - discarded)})
        choices.update({identifier: (168 if outcome == "sent" else 24, "referenced") for identifier in sorted(evidence)})
        if outcome == "duplicate":
            choices.update({identifier: (0, "alert_handled") for identifier in value["alertIds"]})
    for identifier, (hours, reason) in choices.items():
        item = value["items"][identifier]
        grant = grants[item["sourceId"]]
        permanent = item["kind"] == "alert"
        expires = None if permanent else now + hours * 3600
        event_key = app._event_key(grant, item["remoteId"])
        ack_id = hashlib.sha256(json.dumps([value["roleId"], value["tickId"], event_key, reason]).encode()).hexdigest()
        if db.execute("SELECT 1 FROM autonomy_ack_outbox WHERE ack_id=?", (ack_id,)).fetchone():
            continue
        db.execute("""INSERT INTO autonomy_event_claims VALUES(?,?,?,?,?)
            ON CONFLICT(role_id,event_key) DO UPDATE SET tick_id=excluded.tick_id,
            expires_at=excluded.expires_at,status=excluded.status
            WHERE autonomy_event_claims.tick_id=excluded.tick_id OR autonomy_event_claims.expires_at<=?""",
            (value["roleId"], event_key, value["tickId"], expires, reason, now))
        db.execute("""INSERT OR IGNORE INTO autonomy_ack_outbox
            (ack_id,role_id,tick_id,source_id,source_revision,event_id,decision_at,expires_at,
             permanent,reason,source_grant,status,retry_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ack_id, value["roleId"], value["tickId"], grant.sourceId, grant.revision,
             item["remoteId"], now, expires, int(permanent), reason,
             json.dumps(grant.model_dump(mode="json")), "pending" if grant.ackTool else "unsupported", now))
    return now
