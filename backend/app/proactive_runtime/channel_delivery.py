"""One channel dispatch; later work only repairs its original durable facts."""
import json
import sqlite3

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import retention
from .targets import TargetConflict

SOURCE_KIND = 'autonomy'


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS autonomy_delivery_recovery (
        intent_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,tick_id TEXT NOT NULL,
        disposition_inputs TEXT,sent_disposition_decision_at REAL,
        projection_pending INTEGER NOT NULL DEFAULT 0,resolution_revision INTEGER NOT NULL DEFAULT 0,resolution TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS autonomy_delivery_reconciliations (
        request_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,intent_id TEXT NOT NULL,
        request TEXT NOT NULL,receipt TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS autonomy_channel_receipt_scan (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),last_delivery_rowid INTEGER NOT NULL)''')
    db.execute('INSERT OR IGNORE INTO autonomy_channel_receipt_scan VALUES(1,0)')


def unresolved(db, role_id, *, exclude_tick=None):
    return db.execute('''SELECT 1 FROM autonomy_deliveries d LEFT JOIN autonomy_delivery_recovery r ON r.intent_id=d.intent_id
        WHERE d.role_id=? AND d.status IN ('dispatching','pending','uncertain')
        AND (? IS NULL OR d.tick_id<>?) AND COALESCE(json_extract(r.resolution,'$.allowFuture'),0)=0 LIMIT 1''',
        (role_id, exclude_tick, exclude_tick)).fetchone() is not None


def retain_inputs(turn, db, intent_id, draft):
    from .dispositions import freeze_inputs
    encoded = freeze_inputs(turn, db, intent_id, draft)
    retention.retain_disposition_inputs(db, turn.origin.tick_id, encoded)
    db.execute('''INSERT INTO autonomy_delivery_recovery(intent_id,role_id,tick_id,disposition_inputs,projection_pending)
        VALUES(?,?,?,?,1)''', (intent_id, turn.origin.role_id, turn.origin.tick_id, encoded))
    retention.check_capacity(db, turn.origin.role_id)


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    requestId: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    expectedResolutionRevision: int = Field(ge=0)
    expectedStatus: str = Field(pattern=r'^(dispatching|pending|uncertain)$')
    decision: str = Field(pattern=r'^(reported_sent|reported_not_sent|keep_pending)$')
    allowFuture: bool
    note: str = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def explicit_report(self):
        if not self.note.strip() or self.decision == 'keep_pending' and self.allowFuture:
            raise ValueError('a pending report cannot reopen future sends')
        return self


def reconcile_owner(app, role_id, intent_id, request):
    app._require_role(role_id)
    encoded = json.dumps(request.model_dump(), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    with app._transaction() as db:
        prior = db.execute('SELECT * FROM autonomy_delivery_reconciliations WHERE request_id=?', (request.requestId,)).fetchone()
        if prior:
            if (prior['role_id'], prior['intent_id'], prior['request']) != (role_id, intent_id, encoded):
                raise TargetConflict('resolution request identity conflicts')
        else:
            claim = db.execute('SELECT * FROM autonomy_deliveries WHERE role_id=? AND intent_id=?', (role_id, intent_id)).fetchone()
            if claim is None:
                raise KeyError(intent_id)
            row = db.execute('SELECT * FROM autonomy_delivery_recovery WHERE intent_id=?', (intent_id,)).fetchone()
            revision = row['resolution_revision'] if row else 0
            if revision != request.expectedResolutionRevision or claim['status'] != request.expectedStatus:
                raise TargetConflict('original receipt or resolution revision changed')
            receipt = {'requestId': request.requestId, 'decision': request.decision, 'allowFuture': request.allowFuture,
                'note': request.note.strip(), 'reportedAt': app._now(db), 'revision': revision + 1}
            db.execute('''INSERT INTO autonomy_delivery_recovery(intent_id,role_id,tick_id,resolution_revision,resolution)
                VALUES(?,?,?,?,?) ON CONFLICT(intent_id) DO UPDATE SET
                resolution_revision=excluded.resolution_revision,resolution=excluded.resolution''',
                (intent_id, role_id, claim['tick_id'], revision + 1, json.dumps(receipt, ensure_ascii=False)))
            db.execute('INSERT INTO autonomy_delivery_reconciliations VALUES(?,?,?,?,?)',
                (request.requestId, role_id, intent_id, encoded, json.dumps(receipt, ensure_ascii=False)))
            retention.check_capacity(db, role_id)
    return app.query(role_id)


def public_deliveries(db, role_id, *, tick_id=None):
    result = []
    for row in db.execute('''SELECT d.*,r.projection_pending,r.resolution_revision,r.resolution
        FROM autonomy_deliveries d LEFT JOIN autonomy_delivery_recovery r ON r.intent_id=d.intent_id
        WHERE d.role_id=? AND (? IS NULL OR d.tick_id=?) ORDER BY d.created_at DESC,d.intent_id LIMIT 100''', (role_id, tick_id, tick_id)):
        receipt = json.loads(row['receipt'] or '{}') or {}
        target = json.loads(row['target'])
        result.append({'intentId': row['intent_id'], 'status': row['status'], 'messageId': row['message_id'],
            'target': {key: value for key, value in target.items() if key in {'ref','channel','accountId','targetId','chatId','displayName'}},
            'attemptId': receipt.get('attemptId'), 'platformMessageId': receipt.get('platformMessageId'),
            'error': row['error'], 'projectionPending': bool(row['projection_pending']),
            'resolutionRevision': row['resolution_revision'] or 0, 'resolution': json.loads(row['resolution']) if row['resolution'] else None})
    return result


class ChannelAutonomyDelivery:
    def __init__(self, app):
        self.app = app

    def _authorized(self, turn, draft):
        if not self.app._authorized(turn.origin):
            return False
        identifiers = tuple(draft.get('attachment_ids', ()))
        if identifiers:
            from .drift_attachments import selected
            try:
                return selected(turn, identifiers) == tuple(draft['attachments'])
            except (ValueError, PermissionError, OSError, KeyError):
                return False
        return True

    async def deliver(self, turn, intent_id, draft):
        app = self.app
        with turn.transaction() as db:
            claim = db.execute('SELECT * FROM autonomy_deliveries WHERE intent_id=? AND tick_id=?', (intent_id, turn.origin.tick_id)).fetchone()
            if claim is None or not claim['dispatched'] or claim['status'] != 'dispatching':
                raise PermissionError('channel delivery has no original irreversible claim')
            target = json.loads(claim['target'])
        try:
            frozen = None
            if draft.get('attachment_ids'):
                from .drift_attachments import frozen_bytes
                frozen = frozen_bytes(turn, tuple(draft['attachment_ids']))
            await app.targets.host.account_send(turn.origin.role_id, target['accountId'], target['targetId'], draft['message'],
                actor='push', record_message=False, attachment_ids=tuple(draft.get('attachment_ids', ())),
                frozen_attachment_bytes=frozen,
                source_kind=SOURCE_KIND, source_run_id=turn.origin.tick_id, source_tool_call_id=intent_id,
                authorize_source=lambda: self._authorized(turn, draft))
        except BaseException as error:
            with app._transaction() as db:
                db.execute("UPDATE autonomy_deliveries SET status='uncertain',error='original channel attempt has no definitive local receipt' WHERE intent_id=? AND status='dispatching'", (intent_id,))
            if not isinstance(error, Exception):
                raise
        return await app._owned(self.sync, intent_id)

    def sync(self, intent_id):
        app = self.app
        event = None
        with app._transaction() as db:
            claim = db.execute('SELECT d.*,r.mode FROM autonomy_deliveries d JOIN autonomy_runs r ON r.tick_id=d.tick_id WHERE d.intent_id=?', (intent_id,)).fetchone()
            if claim is None:
                raise KeyError(intent_id)
            target, draft = json.loads(claim['target']), json.loads(claim['draft'])
            attempt = app.targets.host.store.delivery_receipt_for_source(claim['role_id'],
                source_kind=SOURCE_KIND, source_tool_call_id=intent_id, connection=db)
            if attempt is not None and (attempt['source_run_id'] != claim['tick_id'] or attempt['account_id'] != target['accountId']
                    or attempt['target_chat_id'] != target['chatId'] or attempt['content'] != draft['message']):
                raise ValueError('original channel receipt conflicts with its immutable intent')
            status = attempt['status'] if attempt is not None else 'uncertain'
            if status not in {'sent', 'failed', 'pending', 'uncertain'}:
                raise ValueError('invalid original channel receipt status')
            receipt = {'status': status, 'intentId': intent_id, 'targetRef': target['ref'],
                'attemptId': attempt['attempt_id'] if attempt else None,
                'platformMessageId': attempt['platform_message_id'] if attempt else None}
            if status == 'sent':
                receipt, event = app.targets.host.store.project_autonomy_delivery_once(claim['role_id'], claim['tick_id'], intent_id,
                    claim['mode'], target, draft, connection=db)
                recovery = db.execute('SELECT * FROM autonomy_delivery_recovery WHERE intent_id=?', (intent_id,)).fetchone()
                if recovery is None or not recovery['disposition_inputs']:
                    raise ValueError('original channel claim has no durable disposition inputs')
                if recovery['sent_disposition_decision_at'] is None:
                    from .dispositions import plan_from_inputs
                    now = app._now(db)
                    plan_from_inputs(app, db, recovery['disposition_inputs'], 'sent', decision_at=now)
                    db.execute('UPDATE autonomy_delivery_recovery SET sent_disposition_decision_at=? WHERE intent_id=? AND sent_disposition_decision_at IS NULL', (now, intent_id))
                db.execute('UPDATE autonomy_delivery_recovery SET projection_pending=0 WHERE intent_id=?', (intent_id,))
            elif status == 'failed':
                recovery = db.execute('SELECT disposition_inputs FROM autonomy_delivery_recovery WHERE intent_id=?', (intent_id,)).fetchone()
                if recovery and recovery[0]:
                    from .dispositions import plan_from_inputs
                    plan_from_inputs(app, db, recovery[0], 'failed')
                db.execute('UPDATE autonomy_delivery_recovery SET projection_pending=0 WHERE intent_id=?', (intent_id,))
            db.execute('UPDATE autonomy_deliveries SET status=?,receipt=?,message_id=?,error=? WHERE intent_id=?',
                (status, json.dumps(receipt), receipt.get('messageId'), '' if status == 'sent' else 'original channel receipt: ' + status, intent_id))
            retention.check_capacity(db, claim['role_id'])
            retention.settle(db, claim['tick_id'], now=app._now(db))
        if event is not None:
            app.targets.host.store.publish(claim['role_id'], event)
        return receipt

    async def recover(self):
        if self.app.targets.host is None:
            return
        app = self.app
        with app._transaction() as db:
            cursor = db.execute('SELECT last_delivery_rowid FROM autonomy_channel_receipt_scan WHERE singleton=1').fetchone()[0]
            query = '''SELECT d.rowid,d.intent_id FROM autonomy_deliveries d
                JOIN autonomy_runs r ON r.tick_id=d.tick_id WHERE r.runtime_acknowledged=1
                AND r.runtime_result IS NOT NULL
                AND CASE WHEN json_valid(d.target) THEN json_extract(d.target,'$.channel') END<>'desktop'
                AND d.status IN ('dispatching','pending','uncertain')'''
            claims = db.execute(query + ' AND d.rowid>? ORDER BY d.rowid LIMIT 32', (cursor,)).fetchall()
            if not claims:
                claims = db.execute(query + ' AND d.rowid<=? ORDER BY d.rowid LIMIT 32', (cursor,)).fetchall()
        for claim in claims:
            with app._transaction() as db:
                db.execute('UPDATE autonomy_channel_receipt_scan SET last_delivery_rowid=? WHERE singleton=1', (claim[0],))
            try:
                await app._owned(self.sync, claim['intent_id'])
            except (ValueError, PermissionError, KeyError, sqlite3.IntegrityError):
                # The original duty and cursor survive; neither a projection
                # conflict nor absence of evidence authorizes another send.
                continue
