"""Caller-owned SQLite contract for body-free autonomous ACK recovery inputs."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from backend.app.proactive_runtime.config import SourceGrant


def _grant(**changes):
    return SourceGrant.model_validate({
        "sourceId": "news", "revision": 3, "channel": "content", "serverId": "feed",
        "configRevision": "config-original", "grantRevision": "grant-original",
        "getTool": "get_news", "getDefinitionHash": "a" * 64,
        "ackTool": "ack_news", "ackDefinitionHash": "b" * 64,
        "ackContract": "absolute-idempotent", "eventDomain": "news-v1", "enabled": True,
        **changes,
    })


def _item(identifier="candidate", **changes):
    return {"id": identifier, "sourceId": "news", "sourceRevision": 3,
            "eventDomain": "news-v1", "remoteId": "remote-original", "kind": "content",
            "title": "PRIVATE TITLE", "summary": "PRIVATE SUMMARY",
            "url": "https://private.invalid/article", "data": {"secret": "PRIVATE DATA"},
            **changes}


def _database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS saved_inputs (encoded TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS autonomy_turns
            (tick_id TEXT PRIMARY KEY,interesting TEXT NOT NULL,discarded TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS autonomy_event_claims
            (role_id TEXT NOT NULL,event_key TEXT NOT NULL,tick_id TEXT NOT NULL,
             expires_at REAL,status TEXT NOT NULL,PRIMARY KEY(role_id,event_key));
        CREATE TABLE IF NOT EXISTS autonomy_ack_outbox
            (ack_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,tick_id TEXT NOT NULL,
             source_id TEXT NOT NULL,source_revision INTEGER NOT NULL,event_id TEXT NOT NULL,
             decision_at REAL NOT NULL,expires_at REAL,permanent INTEGER NOT NULL,
             reason TEXT NOT NULL,source_grant TEXT NOT NULL,status TEXT NOT NULL,
             attempts INTEGER NOT NULL DEFAULT 0,retry_at REAL,task_id TEXT,error TEXT NOT NULL DEFAULT '');
    """)
    return db


def _turn(items=None, grants=None):
    snapshot = {"alerts": [], "contents": [_item()] if items is None else items,
                "contexts": [], "bodies": {"candidate": "PRIVATE BODY"}}
    return SimpleNamespace(origin=SimpleNamespace(role_id="role-original", tick_id="tick-original"),
                           config=SimpleNamespace(sourceGrants=[_grant()] if grants is None else grants),
                           snapshot=snapshot)


class HostClock:
    """The helper receives only the original host clock and event-key interfaces."""
    def __init__(self):
        self.now = 1_700_000_000.0

    def _now(self, db):
        return self.now

    def _event_key(self, grant, remote_id):
        return f"{grant.serverId}/{grant.eventDomain}/{grant.channel}/{remote_id}"


def test_persisted_inputs_restore_original_sent_decision_in_caller_transaction(tmp_path):
    from backend.app.proactive_runtime.dispositions import freeze_inputs, plan_from_inputs

    path = tmp_path / "business.db"
    turn, host = _turn(), HostClock()
    with _database(path) as db:
        db.execute("INSERT INTO autonomy_turns VALUES(?,?,?)", ("tick-original", '["candidate"]', '[]'))
        encoded = freeze_inputs(turn, db, "intent-original", {"evidence": ["candidate"], "message": "PRIVATE MESSAGE"})
        db.execute("INSERT INTO saved_inputs VALUES(?)", (encoded,))
    db.close()
    assert not any(secret in encoded for secret in (
        "PRIVATE", "private.invalid", "bodies", "title", "summary", '"data"', '"message"'))
    assert json.loads(encoded)["intentId"] == "intent-original"

    # Recovery has no original snapshot or current source configuration available.
    del turn
    db = _database(path)
    try:
        saved = db.execute("SELECT encoded FROM saved_inputs").fetchone()[0]
        db.execute("BEGIN IMMEDIATE")
        assert plan_from_inputs(host, db, saved, "sent") == 1_700_000_000.0
        with sqlite3.connect(path) as observer:
            assert observer.execute("SELECT COUNT(*) FROM autonomy_ack_outbox").fetchone()[0] == 0
        db.commit()
        original = dict(db.execute("SELECT * FROM autonomy_ack_outbox").fetchone())
        assert original["ack_id"] == "6352dbfe5b82917f4739e0a4100d34e0077166182b5e7f4acf10322db6a72208"
        assert {key: original[key] for key in (
            "role_id", "tick_id", "source_id", "source_revision", "event_id", "decision_at",
            "expires_at", "permanent", "reason", "status", "retry_at")} == {
                "role_id": "role-original", "tick_id": "tick-original", "source_id": "news",
                "source_revision": 3, "event_id": "remote-original", "decision_at": 1_700_000_000.0,
                "expires_at": 1_700_604_800.0, "permanent": 0, "reason": "referenced",
                "status": "pending", "retry_at": 1_700_000_000.0}
        assert json.loads(original["source_grant"]) == _grant().model_dump(mode="json")
        claim = dict(db.execute("SELECT * FROM autonomy_event_claims").fetchone())
        assert claim["event_key"] == "feed/news-v1/content/remote-original"
        assert claim["expires_at"] == 1_700_604_800.0
        host.now += 90_000
        plan_from_inputs(host, db, saved, "sent")
        assert dict(db.execute("SELECT * FROM autonomy_ack_outbox").fetchone()) == original
        assert dict(db.execute("SELECT * FROM autonomy_event_claims").fetchone()) == claim
    finally:
        db.close()


@pytest.mark.parametrize("outcome,expected", [
    ("sent", {"discard": ("discarded", 1_702_592_000.0, 0),
              "interest": ("interesting", 1_700_086_400.0, 0),
              "reference": ("referenced", 1_700_604_800.0, 0),
              "overlap": ("referenced", 1_700_604_800.0, 0),
              "alert-a": ("referenced", None, 1)}),
    ("duplicate", {"discard": ("discarded", 1_702_592_000.0, 0),
                   "interest": ("interesting", 1_700_086_400.0, 0),
                   "reference": ("referenced", 1_700_086_400.0, 0),
                   "overlap": ("referenced", 1_700_086_400.0, 0),
                   "alert-a": ("alert_handled", None, 1), "alert-b": ("alert_handled", None, 1)}),
    ("skip", {"discard": ("discarded", 1_702_592_000.0, 0), "overlap": ("discarded", 1_702_592_000.0, 0)}),
    ("failed", {"discard": ("discarded", 1_702_592_000.0, 0), "overlap": ("discarded", 1_702_592_000.0, 0)}),
])
def test_original_classification_precedence_and_alert_permanence(tmp_path, outcome, expected):
    from backend.app.proactive_runtime.dispositions import freeze_inputs, plan_from_inputs

    turn = _turn(items=[_item(name, remoteId=name) for name in (
        "discard", "interest", "reference", "overlap", "unclassified")], grants=[_grant(),
        _grant(sourceId="alerts", channel="alert"), _grant(sourceId="context", channel="context")])
    turn.snapshot["alerts"] = [_item(name, sourceId="alerts", remoteId=name, kind="alert")
                               for name in ("alert-a", "alert-b")]
    turn.snapshot["contexts"] = [_item("context", sourceId="context", kind="context")]
    with _database(tmp_path / "business.db") as db:
        db.execute("INSERT INTO autonomy_turns VALUES(?,?,?)", (
            "tick-original", '["interest","reference","overlap"]', '["discard","overlap"]'))
        encoded = freeze_inputs(turn, db, "", {"evidence": ["reference", "overlap", "alert-a"]})
        plan_from_inputs(HostClock(), db, encoded, outcome)
        rows = db.execute("SELECT event_id,reason,expires_at,permanent FROM autonomy_ack_outbox").fetchall()
        assert {row[0]: tuple(row)[1:] for row in rows} == expected
        assert "context" not in json.loads(encoded)["items"]
    db.close()


def test_empty_drift_requires_no_turn_table_and_never_invents_source_work(tmp_path):
    from backend.app.proactive_runtime.dispositions import freeze_inputs, plan_from_inputs

    db = _database(tmp_path / "business.db")
    try:
        db.execute("DROP TABLE autonomy_turns")
        turn = _turn(items=[], grants=[])
        encoded = freeze_inputs(turn, db, "intent-empty")
        assert json.loads(encoded) == {"version": 1, "roleId": "role-original", "tickId": "tick-original",
            "intentId": "intent-empty", "sourceGrants": [], "items": {}, "interesting": [],
            "discarded": [], "evidence": [], "alertIds": []}
        assert plan_from_inputs(HostClock(), db, encoded, "sent", decision_at=1_700_123_456.0) == 1_700_123_456.0
        assert db.execute("SELECT COUNT(*) FROM autonomy_ack_outbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM autonomy_event_claims").fetchone()[0] == 0
    finally:
        db.close()


def test_original_missing_ack_permission_stays_unsupported_and_decision_rolls_back(tmp_path):
    from backend.app.proactive_runtime.dispositions import freeze_inputs, plan_from_inputs

    path = tmp_path / "business.db"
    db = _database(path)
    try:
        db.execute("INSERT INTO autonomy_turns VALUES(?,?,?)", ("tick-original", '[]', '[]'))
        encoded = freeze_inputs(_turn(grants=[_grant(ackTool=None, ackDefinitionHash=None, ackContract="none")]),
                                db, "original", {"evidence": ["candidate"]})
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        plan_from_inputs(HostClock(), db, encoded, "sent", decision_at=1_700_200_000.0)
        row = db.execute("SELECT status,decision_at,expires_at,source_grant FROM autonomy_ack_outbox").fetchone()
        assert tuple(row)[:3] == ("unsupported", 1_700_200_000.0, 1_700_804_800.0)
        assert json.loads(row[3])["ackTool"] is None
        db.rollback()
        assert db.execute("SELECT COUNT(*) FROM autonomy_ack_outbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM autonomy_event_claims").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize("change", [
    {"sourceId": "other"}, {"sourceRevision": 4}, {"sourceRevision": True},
    {"eventDomain": "new-domain"}, {"kind": "alert"}, {"remoteId": ""},
])
def test_freeze_rejects_snapshot_identity_mismatch_before_any_claim(tmp_path, change):
    from backend.app.proactive_runtime.dispositions import freeze_inputs

    db = _database(tmp_path / "business.db")
    try:
        db.execute("INSERT INTO autonomy_turns VALUES(?,?,?)", ("tick-original", '[]', '[]'))
        with pytest.raises(ValueError):
            freeze_inputs(_turn(items=[_item(**change)]), db, "original")
        assert db.execute("SELECT COUNT(*) FROM autonomy_ack_outbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM autonomy_event_claims").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize("field", ["interesting", "discarded", "evidence"])
def test_unknown_classification_or_evidence_cannot_be_persisted(tmp_path, field):
    from backend.app.proactive_runtime.dispositions import freeze_inputs

    db = _database(tmp_path / "business.db")
    try:
        db.execute("INSERT INTO autonomy_turns VALUES(?,?,?)", (
            "tick-original", '["foreign"]' if field == "interesting" else '[]',
            '["foreign"]' if field == "discarded" else '[]'))
        with pytest.raises(ValueError):
            freeze_inputs(_turn(), db, "original", {"evidence": ["foreign"] if field == "evidence" else []})
    finally:
        db.close()


@pytest.mark.parametrize("corruption", ["body", "unknown-reference", "kind-object", "duplicate-source"])
def test_corrupt_persisted_inputs_are_rejected_without_partial_decisions(tmp_path, corruption):
    from backend.app.proactive_runtime.dispositions import freeze_inputs, plan_from_inputs

    db = _database(tmp_path / "business.db")
    try:
        db.execute("INSERT INTO autonomy_turns VALUES(?,?,?)", ("tick-original", '[]', '[]'))
        value = json.loads(freeze_inputs(_turn(), db, "original", {"evidence": ["candidate"]}))
        if corruption == "body":
            value["items"]["candidate"]["body"] = "must never persist"
        elif corruption == "unknown-reference":
            value["evidence"] = ["foreign"]
        elif corruption == "kind-object":
            value["items"]["candidate"]["kind"] = []
        else:
            value["sourceGrants"].append(value["sourceGrants"][0])
        with pytest.raises(ValueError):
            plan_from_inputs(HostClock(), db, json.dumps(value), "sent")
        assert db.execute("SELECT COUNT(*) FROM autonomy_ack_outbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM autonomy_event_claims").fetchone()[0] == 0
    finally:
        db.close()


def test_overflow_is_rejected_without_truncating_recovery_identity(tmp_path):
    from backend.app.proactive_runtime.dispositions import freeze_inputs

    content, alert = _grant(eventDomain="界" * 128), _grant(sourceId="alerts", channel="alert", eventDomain="界" * 128)
    contents = [_item(str(index).zfill(256), eventDomain=content.eventDomain, remoteId="c" * 255 + str(index))
                for index in range(5)]
    alerts = [_item(str(index + 5).zfill(256), sourceId="alerts", kind="alert", eventDomain=alert.eventDomain,
                    remoteId="a" * 254 + str(index).zfill(2)) for index in range(20)]
    turn = _turn(items=contents, grants=[content, alert])
    turn.snapshot["alerts"] = alerts
    db = _database(tmp_path / "business.db")
    try:
        db.execute("INSERT INTO autonomy_turns VALUES(?,?,?)", ("tick-original", '[]', '[]'))
        with pytest.raises(ValueError, match="32 KiB"):
            freeze_inputs(turn, db, "original", {"evidence": [item["id"] for item in contents + alerts]})
    finally:
        db.close()


@pytest.mark.parametrize("invalid_time", [True, "1700000000", float("nan"), float("inf"), 10 ** 400])
def test_invalid_first_decision_time_never_creates_ack_or_claim(tmp_path, invalid_time):
    from backend.app.proactive_runtime.dispositions import freeze_inputs, plan_from_inputs

    db = _database(tmp_path / "business.db")
    try:
        db.execute("INSERT INTO autonomy_turns VALUES(?,?,?)", ("tick-original", '[]', '[]'))
        encoded = freeze_inputs(_turn(), db, "original", {"evidence": ["candidate"]})
        with pytest.raises(ValueError, match="timestamp"):
            plan_from_inputs(HostClock(), db, encoded, "sent", decision_at=invalid_time)
        assert db.execute("SELECT COUNT(*) FROM autonomy_ack_outbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM autonomy_event_claims").fetchone()[0] == 0
    finally:
        db.close()
