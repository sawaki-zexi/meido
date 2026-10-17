"""Independent finite ACK ordinals; only receipts can acknowledge a source."""
import asyncio
import hashlib
import json
import math
import time

from ..mcp_autonomy import AutonomyMcpGrant
from ..task_runtime import TaskOutcome
from .config import AutonomyConfig, SourceGrant
from .identity import AutonomyOrigin
from .sources import _decode
from . import retention
from .source_validation import retry_allowed


def current_source(app, db, row):
    state = db.execute("SELECT * FROM autonomy_roles WHERE role_id=?", (row["role_id"],)).fetchone()
    if state is None or app.role_getter(row["role_id"]) is None:
        return None
    config = AutonomyConfig.model_validate_json(state["config"])
    old = SourceGrant.model_validate_json(row["source_grant"])
    if not (config.activeDecisionEnabled or config.driftEnabled):
        return None
    source = next((item for item in config.sourceGrants if item.sourceId == row["source_id"]), None)
    if source != old or not source.enabled or not source.ackTool or app.mcp_host is None:
        return None
    server = app.mcp_host.store.get(source.serverId)
    if (server is None or not server.enabled or row["role_id"] not in server.role_ids
            or (server.revision, server.grant_id) != (source.configRevision, source.grantRevision)):
        return None
    return source


async def schedule_acks(app):
    admitted = []
    with app._transaction() as db:
        now = app._now(db)
        rows = db.execute("SELECT * FROM autonomy_ack_outbox WHERE status IN ('pending','retry_wait') AND retry_at<=? ORDER BY decision_at,ack_id", (now,)).fetchall()
        for row in rows:
            source = current_source(app, db, row)
            if source is None:
                db.execute("UPDATE autonomy_ack_outbox SET status='revoked' WHERE ack_id=?", (row["ack_id"],))
                retention.settle(db, row['tick_id'], now=now)
                continue
            if now >= row["decision_at"] + 600 or row["expires_at"] is not None and now >= row["expires_at"]:
                db.execute("UPDATE autonomy_ack_outbox SET status='expired' WHERE ack_id=?", (row["ack_id"],))
                retention.settle(db, row['tick_id'], now=now)
                continue
            state = db.execute("SELECT * FROM autonomy_roles WHERE role_id=?", (row["role_id"],)).fetchone()
            if state["human_busy"] or state["cleanup_pending"] or app.mcp_host.autonomy_cleanup_pending(row["role_id"]):
                continue
            # At most one in-flight ACK task per role leaves a Runtime slot for
            # the foreground and avoids consuming a queue of stale deadlines.
            if db.execute("SELECT 1 FROM autonomy_runs WHERE role_id=? AND kind='autonomy_ack' AND runtime_acknowledged=0", (row["role_id"],)).fetchone():
                continue
            ordinal = row["attempts"] + 1
            if ordinal > (3 if retry_allowed(db, row["role_id"], source) else 1):
                db.execute("UPDATE autonomy_ack_outbox SET status='ack_uncertain' WHERE ack_id=?", (row["ack_id"],))
                continue
            if not row["permanent"] and math.floor((row["expires_at"] - now) / 3600) < 1 and source.ackContract == "legacy-ttl":
                db.execute("UPDATE autonomy_ack_outbox SET status='expired' WHERE ack_id=?", (row["ack_id"],))
                retention.settle(db, row['tick_id'], now=now)
                continue
            day = int(now // 86400)
            db.execute("INSERT OR IGNORE INTO autonomy_budgets(role_id,day) VALUES(?,?)", (row["role_id"], day))
            if db.execute("SELECT acks FROM autonomy_budgets WHERE role_id=? AND day=?", (row["role_id"], day)).fetchone()[0] >= 512:
                continue
            identifier = hashlib.sha256(f"{row['ack_id']}:{ordinal}".encode()).hexdigest()
            origin = AutonomyOrigin(row["role_id"], identifier, state["revision"], state["generation"], ("source_ack",))
            try:
                retention.reserve(db, origin.role_id, identifier, "autonomy_ack", now)
            except retention.StorageLimitError:
                db.execute("UPDATE autonomy_ack_outbox SET error='autonomous storage quota exhausted' WHERE ack_id=?", (row["ack_id"],))
                continue
            payload = json.dumps({"ackId": row["ack_id"], "ordinal": ordinal})
            db.execute("INSERT INTO autonomy_runs(tick_id,role_id,origin,kind,mode,started_at,frozen_config,payload) VALUES(?,?,?,'autonomy_ack','source_ack',?,?,?)",
                       (identifier, row["role_id"], origin.serialized(), now, state["config"], payload))
            db.execute("INSERT INTO autonomy_ack_attempts(ack_id,ordinal,operation_id) VALUES(?,?,?)", (row["ack_id"], ordinal, identifier))
            db.execute("UPDATE autonomy_ack_outbox SET status='inflight',attempts=? WHERE ack_id=?", (ordinal, row["ack_id"]))
            db.execute("UPDATE autonomy_budgets SET acks=acks+1 WHERE role_id=? AND day=?", (row["role_id"], day))
            admitted.append(origin)
    for origin in admitted:
        await app._launch(origin)


def settle_attempt(app, db, operation):
    """Copy an unsuccessful ordinal once; never reset it to unattempted."""
    payload = json.loads(operation["payload"])
    row = db.execute("SELECT * FROM autonomy_ack_outbox WHERE ack_id=?", (payload["ackId"],)).fetchone()
    if row is None or row["status"] != "inflight" or row["attempts"] != payload["ordinal"]:
        return
    source = current_source(app, db, row)
    now = app._now(db)
    if source is None:
        status, retry = "revoked", None
    elif retry_allowed(db, row["role_id"], source) and row["attempts"] < 3 and now < row["decision_at"] + 600 and (row["expires_at"] is None or now < row["expires_at"]):
        status, retry = "retry_wait", row["decision_at"] + (60 if row["attempts"] == 1 else 300)
    else:
        status, retry = "ack_uncertain", None
    db.execute("UPDATE autonomy_ack_outbox SET status=?,retry_at=?,error='source receipt unavailable' WHERE ack_id=?",
               (status, retry, row["ack_id"]))


async def execute_ack(app, origin, execution):
    with app._transaction() as db:
        if not app._authorized_in(db, origin):
            raise PermissionError("source ACK origin was sealed")
        operation = db.execute("SELECT * FROM autonomy_runs WHERE tick_id=?", (origin.tick_id,)).fetchone()
        payload = json.loads(operation["payload"])
        row = db.execute("SELECT * FROM autonomy_ack_outbox WHERE ack_id=?", (payload["ackId"],)).fetchone()
        source, now = current_source(app, db, row), app._now(db)
        if source is None or row["status"] != "inflight" or row["attempts"] != payload["ordinal"]:
            raise PermissionError("source ACK version or ordinal changed")
        if payload["ordinal"] > 1 and not retry_allowed(db, row["role_id"], source):
            raise PermissionError("source ACK retry proof is no longer valid")
        remaining = min(execution.deadline - time.monotonic(), operation["started_at"] + 30 - now,
                        row["decision_at"] + 600 - now)
        if remaining <= 0 or row["expires_at"] is not None and now >= row["expires_at"]:
            raise TimeoutError("original ACK deadline expired")
        ttl = 0 if row["permanent"] else math.floor((row["expires_at"] - now) / 3600)
        if source.ackContract == "legacy-ttl" and not row["permanent"] and ttl < 1:
            raise TimeoutError("legacy ACK has no remaining full hour")
        arguments = {"event_ids": [row["event_id"]], "ttl_hours": ttl}
        if source.ackContract == "absolute-idempotent":
            arguments.update(ack_id=row["ack_id"], expires_at=row["expires_at"])
        db.execute("UPDATE autonomy_runs SET status='running' WHERE tick_id=?", (origin.tick_id,))
    grant = AutonomyMcpGrant(source.serverId, source.configRevision, source.grantRevision,
                            ((source.ackTool, source.ackDefinitionHash),))
    bundle = app.mcp_host.freeze_autonomy(origin, (grant,), authorized=lambda: app._authorized(origin))
    try:
        async with asyncio.timeout(remaining):
            with app._transaction() as db:
                if not app._authorized_in(db, origin) or current_source(app, db, row) is None:
                    raise PermissionError("source ACK revoked before dispatch")
                if payload["ordinal"] > 1 and not retry_allowed(db, row["role_id"], source):
                    raise PermissionError("source ACK retry proof changed before dispatch")
                changed = db.execute("UPDATE autonomy_ack_attempts SET dispatched=1 WHERE ack_id=? AND ordinal=? AND dispatched=0", (row["ack_id"], payload["ordinal"])).rowcount
                if changed != 1:
                    raise PermissionError("source ACK ordinal already consumed its call")
            receipt = _decode(await bundle.call(source.serverId, source.ackTool, arguments))
            if not isinstance(receipt, dict) or receipt.get("event_ids") != arguments["event_ids"]:
                raise ValueError("source did not confirm the original event")
            if source.ackContract == "absolute-idempotent" and (receipt.get("ack_id") != row["ack_id"] or "expires_at" not in receipt or receipt["expires_at"] != row["expires_at"]):
                raise ValueError("source did not confirm the fixed ACK identity and expiry")
            with app._transaction() as db:
                # A genuine receipt remains a fact even if the owner revoked
                # permission while the response was being committed.
                db.execute("UPDATE autonomy_ack_outbox SET status='acknowledged',retry_at=NULL,error='' WHERE ack_id=?", (row["ack_id"],))
                db.execute("UPDATE autonomy_runs SET status='acknowledged' WHERE tick_id=?", (origin.tick_id,))
                # A late channel receipt may have released the original
                # Runtime long before its source ACK. Settle that now-resolved
                # business duty as well; no ACK identity or expiry is changed.
                retention.settle(db, row['tick_id'], now=app._now(db))
        return TaskOutcome("succeeded", output_tail="source acknowledgement receipt committed")
    finally:
        await bundle.close()
