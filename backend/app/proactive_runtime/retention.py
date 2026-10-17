"""Caller-owned SQLite storage admission and conservative retention for #177.

Call ensure_schema inside the application's BEGIN IMMEDIATE, then reserve BEFORE
inserting the same operation_id into autonomy_runs, in that SAME transaction.
reserve returns True for a new claim, False for an existing immutable identity;
False never authorizes replay. Quota errors roll back only this helper's changes.
This module never opens a connection, begins/commits the caller's transaction,
deletes business keys, or invokes Runtime/MCP. Limits are trusted helper options.

store_snapshot writes runs.snapshot once (512 KiB). record_transcript takes a
JSON value, appends one <=64 KiB row, and enforces 2 MiB including row overhead
per operation. A tick reserves 3 MiB; poll 64 KiB, ACK 32 KiB. Writes consume
their reserved bytes; untracked business/config rows are conservatively charged
in addition. Accounting counts UTF-8 plus 256 bytes per row across autonomy_*
tables, never the shared sessions.db file or unrelated domain tables/files.

After Runtime ACK AND owned cleanup, call settle; active returns status=active.
It releases remaining unused space, retaining up to 64 KiB/8 rows/8 KiB future
recovery headroom for unresolved delivery/ACK obligations (status=held). Actual
unknown data always remains charged. Repeating settle is harmless and can free
that headroom after reconciliation (status=settled). No old attempt is reopened.
prune uses caller UTC time: source snapshots expire after 24h, transcripts after
7d, resolved details after 90d; active/unknown operation, delivery and ACK identity
and business keys survive. Expiring source text does not resolve those duties.
Existing runs are backfilled with identity-only reservations, never replayed.
Filesystem integration records actual managed allocation and pending journals;
it does not estimate the physical sessions.db file or replay interrupted writes.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math

KIB=1024
MIB=1024*KIB
ROW_BYTES=256
SNAPSHOT_BYTES=512*KIB
TRANSCRIPT_MESSAGE_BYTES=64*KIB
TRANSCRIPT_BYTES=2*MIB
RESERVES={"autonomy_tick":(3*MIB,128,128*KIB),
          "autonomy_poll":(64*KIB,8,8*KIB),"autonomy_ack":(32*KIB,8,8*KIB),
          "autonomy_verify":(16*MIB+64*KIB,16,16*KIB)}
_PAYLOAD_COLUMNS={
    "autonomy_runs":{"snapshot","runtime_result","error"},
    "autonomy_turns":{"draft","final_draft","finish","receipt"},
    "autonomy_deliveries":{"draft","receipt","error"},
    "autonomy_ack_outbox":{"error"},
    "autonomy_roles":{"config"},"autonomy_sources":{"definition"},
}


class StorageLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    global_bytes:int=1024*MIB
    role_records:int=20000
    role_bytes:int=16*MIB

    def __post_init__(self):
        if any(type(value) is not int or value<=0 for value in (self.global_bytes,self.role_records,self.role_bytes)):
            raise ValueError("positive storage limits required")


DEFAULT_LIMITS=Limits()


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'autonomy_*'")}


def _quote(identifier):
    return '"'+identifier.replace('"','""')+'"'


def _one(db,sql,args=()):
    cursor=db.execute(sql,args)
    row=cursor.fetchone()
    return dict(zip((item[0] for item in cursor.description),row)) if row is not None else None


@contextmanager
def _atomic(db):
    if not db.in_transaction:
        raise RuntimeError("retention requires the caller's write transaction")
    db.execute("SAVEPOINT autonomy_retention_write")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK TO autonomy_retention_write")
        db.execute("RELEASE autonomy_retention_write")
        raise
    else:
        db.execute("RELEASE autonomy_retention_write")


def ensure_schema(db):
    with _atomic(db):
        db.execute("""CREATE TABLE IF NOT EXISTS autonomy_storage_reservations (
            operation_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,kind TEXT NOT NULL,created_at REAL NOT NULL,
            remaining_bytes INTEGER NOT NULL,rows_reserved INTEGER NOT NULL,metadata_reserved INTEGER NOT NULL,
            transcript_bytes INTEGER NOT NULL DEFAULT 0,snapshot_at REAL,snapshot_expired INTEGER NOT NULL DEFAULT 0,
            settled INTEGER NOT NULL DEFAULT 0,settled_at REAL,compacted INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS autonomy_transcripts (
            tick_id TEXT NOT NULL,sequence INTEGER NOT NULL,created_at REAL NOT NULL,
            message TEXT NOT NULL,bytes INTEGER NOT NULL,PRIMARY KEY(tick_id,sequence))""")
        if "autonomy_runs" in _tables(db):
            db.execute("""INSERT OR IGNORE INTO autonomy_storage_reservations
                (operation_id,role_id,kind,created_at,remaining_bytes,rows_reserved,metadata_reserved,snapshot_at)
                SELECT tick_id,role_id,kind,started_at,0,0,0,CASE WHEN snapshot IS NOT NULL THEN started_at END FROM autonomy_runs""")
        if "autonomy_ack_outbox" in _tables(db):
            db.execute("CREATE INDEX IF NOT EXISTS autonomy_retention_ack_tick ON autonomy_ack_outbox(tick_id)")


def _size_expression(columns):
    return str(ROW_BYTES)+"+"+"+".join(
        f"CASE WHEN typeof(t.{_quote(column)}) IN ('integer','real') THEN 8 ELSE COALESCE(length(CAST(t.{_quote(column)} AS BLOB)),0) END"
        for column in columns) if columns else str(ROW_BYTES)


def _usage(db):
    tables=_tables(db)
    total = 0
    counts: dict[str, int] = {}
    metadata: dict[str, int] = {}
    for table in tables:
        columns=[row[1] for row in db.execute(f"PRAGMA table_info({_quote(table)})")]
        full=_size_expression(columns)
        total+=db.execute(f"SELECT COALESCE(SUM({full}),0) FROM {_quote(table)} t").fetchone()[0]
        if table=="autonomy_transcripts":
            continue
        source=f"{_quote(table)} t"
        if "role_id" in columns:
            role="t.role_id"
        elif table=="autonomy_turns" and "autonomy_runs" in tables:
            source+=" JOIN autonomy_runs r ON r.tick_id=t.tick_id"
            role="r.role_id"
        elif table=="autonomy_ack_attempts" and "autonomy_ack_outbox" in tables:
            source+=" JOIN autonomy_ack_outbox r ON r.ack_id=t.ack_id"
            role="r.role_id"
        else:
            continue
        fields=[column for column in columns if column not in _PAYLOAD_COLUMNS.get(table,set())]
        for identifier,count,size in db.execute(f"SELECT {role},COUNT(*),SUM({_size_expression(fields)}) FROM {source} GROUP BY {role}"):
            counts[identifier]=counts.get(identifier,0)+count
            metadata[identifier]=metadata.get(identifier,0)+size
    if "autonomy_storage_reservations" in tables:
        for role,size,count,meta in db.execute("SELECT role_id,SUM(remaining_bytes),SUM(rows_reserved),SUM(metadata_reserved) FROM autonomy_storage_reservations GROUP BY role_id"):
            total+=size
            counts[role]=counts.get(role,0)+count
            metadata[role]=metadata.get(role,0)+meta
    if 'autonomy_drift_allocations' in tables:
        total += db.execute('SELECT COALESCE(SUM(committed_bytes+reserved_bytes),0) FROM autonomy_drift_allocations').fetchone()[0]
    if 'autonomy_meme_allocations' in tables:
        total += db.execute('SELECT COALESCE(SUM(committed_bytes),0) FROM autonomy_meme_allocations').fetchone()[0]
    if 'autonomy_meme_reservations' in tables:
        total += db.execute('SELECT COALESCE(SUM(bytes),0) FROM autonomy_meme_reservations').fetchone()[0]
    if 'autonomy_drift_logs' in tables:
        total += db.execute('SELECT COALESCE(SUM(bytes),0) FROM autonomy_drift_logs').fetchone()[0]
    return total,counts,metadata


def usage(db,role_id):
    """Logical current allocation, including unfinished reservations."""
    total,counts,metadata=_usage(db)
    files = db.execute('SELECT committed_bytes+reserved_bytes FROM autonomy_drift_allocations WHERE role_id=?', (role_id,)).fetchone() if 'autonomy_drift_allocations' in _tables(db) else None
    meme_files = db.execute('SELECT committed_bytes FROM autonomy_meme_allocations WHERE role_id=?', (role_id,)).fetchone() if 'autonomy_meme_allocations' in _tables(db) else None
    meme_reserved = db.execute('SELECT COALESCE(SUM(bytes),0) FROM autonomy_meme_reservations WHERE role_id=?', (role_id,)).fetchone() if 'autonomy_meme_reservations' in _tables(db) else None
    return {"globalBytes":total,"roleRecords":counts.get(role_id,0),"roleBytes":metadata.get(role_id,0),
        'filesystemBytes': (files[0] if files else 0) + (meme_files[0] if meme_files else 0) + (meme_reserved[0] if meme_reserved else 0)}


def _check(db,role_id,limits):
    current=usage(db,role_id)
    if (current["globalBytes"]>limits.global_bytes or current["roleRecords"]>limits.role_records
            or current["roleBytes"]>limits.role_bytes):
        raise StorageLimitError("autonomous storage quota exhausted; unresolved obligations and tombstones retained")


def check_capacity(db, role_id):
    """Validate business/config writes before their caller-owned commit."""
    _check(db, role_id, DEFAULT_LIMITS)


def retain_runtime_log(db, tick_id, actual_bytes):
    """Ended Runtime + owned join proves the original 64 KiB log cannot grow."""
    if 'autonomy_drift_logs' in _tables(db):
        db.execute('UPDATE autonomy_drift_logs SET bytes=? WHERE tick_id=?', (actual_bytes, tick_id))


def _time(now):
    if isinstance(now,bool) or not isinstance(now,(int,float)) or not math.isfinite(now):
        raise ValueError("finite host UTC timestamp required")
    return float(now)


def reserve(db,role_id,operation_id,kind,now,*,limits=DEFAULT_LIMITS):
    if kind not in RESERVES or not role_id or not operation_id:
        raise ValueError("valid autonomous identity/kind required")
    with _atomic(db):
        prior=_one(db,"SELECT role_id,kind FROM autonomy_storage_reservations WHERE operation_id=?",(operation_id,))
        if prior:
            if (prior["role_id"],prior["kind"])!=(role_id,kind):
                raise ValueError("operation identity belongs to another immutable grant")
            return False
        size,rows,meta=RESERVES[kind]
        db.execute("""INSERT INTO autonomy_storage_reservations
            (operation_id,role_id,kind,created_at,remaining_bytes,rows_reserved,metadata_reserved)
            VALUES(?,?,?,?,?,?,?)""",(operation_id,role_id,kind,_time(now),size,rows,meta))
        _check(db,role_id,limits)
        return True


def _writable(db,tick_id):
    row=_one(db,"SELECT * FROM autonomy_storage_reservations WHERE operation_id=?",(tick_id,))
    run=_one(db,"SELECT * FROM autonomy_runs WHERE tick_id=?",(tick_id,))
    if row is None or run is None or row["settled"] or run["runtime_acknowledged"]:
        raise ValueError("operation has no live storage reservation")
    return row,run


def _consume(db,tick_id,amount):
    if db.execute("UPDATE autonomy_storage_reservations SET remaining_bytes=remaining_bytes-? WHERE operation_id=? AND remaining_bytes>=?",
                  (amount,tick_id,amount)).rowcount!=1:
        raise StorageLimitError("operation storage reservation exhausted")


def _text(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",",":"),sort_keys=True)


def store_snapshot(db,tick_id,snapshot,now,*,limits=DEFAULT_LIMITS):
    if not isinstance(snapshot,dict):
        raise ValueError("normalized source snapshot must be an object")
    encoded=_text(snapshot)
    size=len(encoded.encode("utf-8"))
    if size>SNAPSHOT_BYTES:
        raise StorageLimitError("source snapshot exceeds 512 KiB UTF-8")
    with _atomic(db):
        reservation,run=_writable(db,tick_id)
        if run["snapshot"] is not None:
            if _text(json.loads(run["snapshot"]))!=encoded:
                raise ValueError("static source snapshot cannot be replaced")
            return False
        _consume(db,tick_id,size)
        db.execute("UPDATE autonomy_runs SET snapshot=? WHERE tick_id=?",(encoded,tick_id))
        db.execute("UPDATE autonomy_storage_reservations SET snapshot_at=? WHERE operation_id=?",(_time(now),tick_id))
        _check(db,reservation["role_id"],limits)
        return True


def retain_disposition_inputs(db, tick_id, encoded):
    """Charge the bounded recovery input before the dispatch claim commits."""
    size = len(encoded.encode('utf-8'))
    if size > 32*KIB:
        raise StorageLimitError('disposition recovery input exceeds 32 KiB')
    with _atomic(db):
        reservation, _ = _writable(db, tick_id)
        _consume(db, tick_id, size + ROW_BYTES)
        _check(db, reservation['role_id'], DEFAULT_LIMITS)


def record_transcript(db,tick_id,message,now,*,limits=DEFAULT_LIMITS):
    encoded=_text(message)
    if len(encoded.encode("utf-8"))>TRANSCRIPT_MESSAGE_BYTES:
        raise StorageLimitError("transcript message exceeds 64 KiB UTF-8")
    with _atomic(db):
        reservation,_=_writable(db,tick_id)
        sequence=db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM autonomy_transcripts WHERE tick_id=?",(tick_id,)).fetchone()[0]
        size=ROW_BYTES+len(tick_id.encode("utf-8"))+len(encoded.encode("utf-8"))+24
        if reservation["transcript_bytes"]+size>TRANSCRIPT_BYTES:
            raise StorageLimitError("operation transcript exceeds 2 MiB")
        _consume(db,tick_id,size)
        db.execute("INSERT INTO autonomy_transcripts VALUES(?,?,?,?,?)",(tick_id,sequence,_time(now),encoded,size))
        db.execute("UPDATE autonomy_storage_reservations SET transcript_bytes=transcript_bytes+? WHERE operation_id=?",(size,tick_id))
        _check(db,reservation["role_id"],limits)
        return sequence


def _finished(run):
    return bool(run and run["runtime_acknowledged"] and not run["result_ack_pending"] and not run["launch_pending"]
                and run["status"] not in {"preparing","running","pending","inflight","dispatching"})


def _resolved(db,tick_id):
    tables=_tables(db)
    if 'autonomy_delivery_recovery' in tables and db.execute(
            'SELECT 1 FROM autonomy_delivery_recovery WHERE tick_id=? AND projection_pending=1', (tick_id,)).fetchone():
        return False
    if 'autonomy_drift_write_journal' in tables and db.execute(
            "SELECT 1 FROM autonomy_drift_write_journal WHERE operation_id=? AND status='pending'", (tick_id,)).fetchone():
        return False
    if 'autonomy_meme_operations' in tables and db.execute(
            "SELECT 1 FROM autonomy_meme_operations WHERE run_id=? AND status NOT IN ('recorded','not_published')", (tick_id,)).fetchone():
        return False
    if 'autonomy_drift_progress' in tables and db.execute(
            'SELECT 1 FROM autonomy_drift_progress WHERE last_tick_id=? AND projection_pending=1', (tick_id,)).fetchone():
        return False
    definitive_drift_failure = False
    if 'autonomy_deliveries' in tables:
        run = _one(db, 'SELECT mode FROM autonomy_runs WHERE tick_id=?', (tick_id,))
        if run and run['mode'] == 'drift':
            from .drift_delivery import actual_outcome
            claim = _one(db, 'SELECT * FROM autonomy_deliveries WHERE tick_id=?', (tick_id,))
            try:
                definitive_drift_failure = actual_outcome(claim) == 'failed'
            except (ValueError, TypeError, KeyError):
                return False
        if not definitive_drift_failure and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_delivery_attempts'").fetchone():
            # An external A01 may finish with a definite platform rejection.
            # Only its original, matching durable attempt resolves that claim;
            # a manual report or an unknown transport result cannot do so.
            definitive_drift_failure = db.execute('''SELECT 1 FROM autonomy_deliveries d
                JOIN channel_delivery_attempts a ON a.attempt_id=CASE WHEN json_valid(d.receipt)
                    THEN json_extract(d.receipt,'$.attemptId') END
                WHERE d.tick_id=? AND d.status='failed' AND d.dispatched=1 AND a.status='failed'
                AND json_extract(d.receipt,'$.status')='failed' AND json_extract(d.receipt,'$.intentId')=d.intent_id
                AND a.role_id=d.role_id AND a.source_kind='autonomy' AND a.source_run_id=d.tick_id
                AND a.source_tool_call_id=d.intent_id AND a.account_id=json_extract(d.target,'$.accountId')
                AND a.target_chat_id=json_extract(d.target,'$.chatId') AND a.content=json_extract(d.draft,'$.message')''',
                (tick_id,)).fetchone() is not None
    if not definitive_drift_failure and "autonomy_deliveries" in tables and db.execute("""SELECT 1 FROM autonomy_deliveries WHERE tick_id=? AND NOT (
        (status='sent' AND message_id IS NOT NULL AND CASE WHEN json_valid(receipt)
            THEN COALESCE(json_extract(receipt,'$.status')='sent' AND json_extract(receipt,'$.messageId')=message_id,0) ELSE 0 END)
        OR (status IN ('failed','cancelled') AND dispatched=0))""",(tick_id,)).fetchone():
        return False
    if "autonomy_ack_outbox" in tables:
        linked="tick_id=?"
        args=[tick_id]
        dispatched="0"
        if "autonomy_ack_attempts" in tables:
            linked+=" OR ack_id IN (SELECT ack_id FROM autonomy_ack_attempts WHERE operation_id=?)"
            args.append(tick_id)
            dispatched="EXISTS(SELECT 1 FROM autonomy_ack_attempts a WHERE a.ack_id=o.ack_id AND a.dispatched=1)"
        if db.execute(f"""SELECT 1 FROM autonomy_ack_outbox o WHERE ({linked}) AND NOT
            (status='acknowledged' OR (status IN ('unsupported','revoked','expired') AND NOT ({dispatched})))""",args).fetchone():
            return False
    return True


def settle(db,tick_id,*,now=None):
    with _atomic(db):
        row=_one(db,"SELECT * FROM autonomy_storage_reservations WHERE operation_id=?",(tick_id,))
        run=_one(db,"SELECT * FROM autonomy_runs WHERE tick_id=?",(tick_id,))
        if row is None or not _finished(run):
            return {"status":"active","releasedBytes":0}
        if now is None:
            clock=_one(db,"SELECT value FROM autonomy_meta WHERE key='clock'") if "autonomy_meta" in _tables(db) else None
            now=max(row["created_at"],clock["value"] if clock else row["created_at"])
        resolved=_resolved(db,tick_id)
        remaining=0 if resolved else min(row["remaining_bytes"],64*KIB)
        rows=0 if resolved else min(row["rows_reserved"],8)
        meta=0 if resolved else min(row["metadata_reserved"],8*KIB)
        db.execute("""UPDATE autonomy_storage_reservations SET settled=1,settled_at=COALESCE(settled_at,?),
            remaining_bytes=?,rows_reserved=?,metadata_reserved=? WHERE operation_id=?""",(_time(now),remaining,rows,meta,tick_id))
        return {"status":"settled" if resolved else "held","releasedBytes":row["remaining_bytes"]-remaining}


def prune(db,now):
    now=_time(now)
    result={"expiredSnapshots":0,"deletedTranscripts":0,"compactedRuns":0,"protectedRuns":0}
    with _atomic(db):
        identifiers=[row[0] for row in db.execute("SELECT operation_id FROM autonomy_storage_reservations")]
        tables=_tables(db)
        for identifier in identifiers:
            run=_one(db,"SELECT * FROM autonomy_runs WHERE tick_id=?",(identifier,))
            row=_one(db,"SELECT * FROM autonomy_storage_reservations WHERE operation_id=?",(identifier,))
            if row["snapshot_at"] is not None and not row["snapshot_expired"] and now-row["snapshot_at"]>=86400:
                snapshot=json.loads(run["snapshot"]) if run["snapshot"] else {}
                summary={"sources":[{key:value for key,value in source.items() if key in
                    {"sourceId","revision","channel","observedAt","status","truncated"}} for source in snapshot.get("sources",[])],"expired":True}
                db.execute("UPDATE autonomy_runs SET snapshot=? WHERE tick_id=?",(_text(summary),identifier))
                db.execute("UPDATE autonomy_storage_reservations SET snapshot_expired=1 WHERE operation_id=?",(identifier,))
                result["expiredSnapshots"]+=1
            result["deletedTranscripts"]+=db.execute("DELETE FROM autonomy_transcripts WHERE tick_id=? AND created_at<=?",(identifier,now-7*86400)).rowcount
            if "autonomy_drift_tool_calls" in tables and _finished(run) and now-row["created_at"]>=7*86400:
                # Complete tool output is another internal transcript copy.
                # Keep call/hash tombstones and separate unresolved obligations;
                # an unacknowledged Runtime may still need its original result.
                db.execute("UPDATE autonomy_drift_tool_calls SET result=NULL WHERE tick_id=?",(identifier,))
            if not _finished(run) or not _resolved(db,identifier):
                result["protectedRuns"]+=1
                if _finished(run): settle(db,identifier,now=now)
                continue
            settle(db,identifier,now=now)
            row=_one(db,"SELECT * FROM autonomy_storage_reservations WHERE operation_id=?",(identifier,))
            details_expired=now-max(row["created_at"],row["settled_at"])>=90*86400
            if details_expired and "autonomy_drift_turns" in tables:
                # Also clean already-compacted ledgers from the old version,
                # which did not include these Drift detail columns.
                db.execute("UPDATE autonomy_drift_turns SET finish=NULL,receipt=NULL WHERE tick_id=?",(identifier,))
            if not row["compacted"] and details_expired:
                db.execute("UPDATE autonomy_runs SET frozen_config='{}',bindings='{}',runtime_result=NULL,payload=NULL,error='',task_id=NULL WHERE tick_id=?",(identifier,))
                if "autonomy_turns" in tables:
                    db.execute("UPDATE autonomy_turns SET draft=NULL,final_draft=NULL,finish=NULL,receipt=NULL,interesting='[]',discarded='[]' WHERE tick_id=?",(identifier,))
                if "autonomy_deliveries" in tables:
                    # Existing semantic guard reads draft.message; retain its
                    # shape while expiring the message text and other details.
                    db.execute("UPDATE autonomy_deliveries SET draft='{\"message\":\"\"}',error='' WHERE tick_id=?",(identifier,))
                if 'autonomy_delivery_recovery' in tables:
                    db.execute('UPDATE autonomy_delivery_recovery SET disposition_inputs=NULL WHERE tick_id=?', (identifier,))
                if "autonomy_ack_outbox" in tables:
                    db.execute("UPDATE autonomy_ack_outbox SET source_grant='{}',error='' WHERE tick_id=?",(identifier,))
                db.execute("UPDATE autonomy_storage_reservations SET compacted=1 WHERE operation_id=?",(identifier,))
                result["compactedRuns"]+=1
    return result
