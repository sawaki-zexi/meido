"""One irreversible send opportunity, followed by receipt reconciliation only.

The claim is committed before calling the delivery interface. A desktop receipt
comes from the canonical SessionStore transaction, never a model success flag.
"""
import hashlib
import json
import sqlite3


class DesktopDriftDelivery:
    def __init__(self, app):
        self.app = app

    async def deliver(self, turn, intent_id, draft):
        if draft.get('targetRef', 'desktop') != 'desktop':
            return await self.app.channel_delivery.deliver(turn, intent_id, draft)
        try:
            return self._commit(turn, intent_id, draft)
        except sqlite3.IntegrityError:
            # A local constraint rejection rolls back the canonical transaction;
            # no external service is involved in this desktop implementation.
            receipt = {'status': 'failed', 'intentId': intent_id, 'targetRef': 'desktop'}
            with turn.transaction() as db:
                db.execute("UPDATE autonomy_deliveries SET status='failed',receipt=?,error='desktop transaction rejected' WHERE intent_id=? AND status='dispatching'",
                    (json.dumps(receipt), intent_id))
            return receipt

    async def receipt_for(self, origin, claim):
        """Read the original canonical fact; this method never sends a message."""
        def read():
            with self.app._transaction() as db:
                message_id = f"role:{origin.role_id}:autonomy:{claim['intent_id']}"
                message = db.execute('SELECT session_key,content,status,message_type,metadata_json FROM messages WHERE message_id=?', (message_id,)).fetchone()
                if message is None:
                    return None  # Absence after an interrupted send is not failure proof.
                metadata, draft = json.loads(message['metadata_json']), json.loads(claim['draft'])
                if (message['session_key'] != f'role:{origin.role_id}' or message['status'] != 'completed'
                        or message['message_type'] != 'internal_completion' or message['content'] != draft['message']
                        or any(metadata.get(key) != value for key, value in {
                            'internalEvent': 'drift', 'autonomyTickId': origin.tick_id,
                            'autonomyIntentId': claim['intent_id'], 'channel': 'desktop', 'deliveryStatus': 'sent',
                            'mood': draft['mood'], 'thought': draft['thought']}.items())
                        or metadata.get('attachments', []) != draft.get('attachments', [])):
                    raise ValueError('original desktop delivery fact conflicts with its claim')
                return {'status': 'sent', 'messageId': message_id, 'targetRef': 'desktop', 'intentId': claim['intent_id']}
        return await self.app._owned(read)

    def _commit(self, turn, intent_id, draft):
        with turn.transaction() as db:
            claim = db.execute('SELECT * FROM autonomy_deliveries WHERE intent_id=?', (intent_id,)).fetchone()
            if claim is None or claim['status'] != 'dispatching' or not claim['dispatched']:
                raise PermissionError('Drift delivery has no original dispatch claim')
            message = self.app.sessions.append_autonomy_message_once(turn.origin.role_id,
                turn.origin.tick_id, intent_id, draft['message'], connection=db,
                authorized=lambda: self.app._authorized_in(db, turn.origin),
                mood=draft['mood'], thought=draft['thought'], mode='drift', attachments=tuple(draft.get('attachments', ())))
            receipt = {'status': 'sent', 'messageId': message.id, 'targetRef': 'desktop', 'intentId': intent_id}
            db.execute("UPDATE autonomy_deliveries SET status='sent',message_id=?,receipt=? WHERE intent_id=?",
                (message.id, json.dumps(receipt), intent_id))
            return receipt


async def send_once(turn, arguments):
    if turn.skill is None or 'message_push' not in turn.skill.tools or not turn.app.tool_permitted(turn.origin, 'message_push'):
        raise PermissionError('Drift send was not granted to the active skill')
    target = turn.app.targets.selected(turn, arguments)
    text = arguments['message'].strip()
    if not text:
        raise ValueError('Drift message must be nonempty')
    from .drift_attachments import selected
    attachments = selected(turn, arguments.get('attachment_ids', ()))
    if attachments and target['channel'] != 'desktop' and not target['media']:
        raise PermissionError('the selected channel does not support the original attachments')
    draft = {**arguments, 'message': text, 'attachments': attachments, 'targetRef': target['ref']}
    intent_id = hashlib.sha256(('drift:' + turn.origin.role_id + ':' + turn.origin.tick_id).encode()).hexdigest()
    with turn.transaction() as db:
        from .channel_delivery import unresolved, retain_inputs
        if unresolved(db, turn.origin.role_id, exclude_tick=turn.origin.tick_id) or db.execute('SELECT 1 FROM autonomy_deliveries WHERE tick_id=?', (turn.origin.tick_id,)).fetchone():
            raise PermissionError('Drift send opportunity was already used or awaits a real receipt')
        from .delivery import _keys
        keys = _keys(turn, draft)
        now = turn.app._now(db)
        if any(db.execute('''SELECT 1 FROM autonomy_delivery_keys k WHERE role_id=? AND key=? AND
                (expires_at>? OR EXISTS(SELECT 1 FROM autonomy_deliveries d WHERE d.intent_id=k.intent_id
                AND d.status IN ('dispatching','pending','uncertain')))''', (turn.origin.role_id, key, now)).fetchone() for key in keys):
            raise PermissionError('original autonomous content is already occupied')
        db.execute('''INSERT INTO autonomy_deliveries(intent_id,tick_id,role_id,target,draft,status,dispatched,created_at)
            VALUES(?,?,?,?,?,'dispatching',1,?)''', (intent_id, turn.origin.tick_id, turn.origin.role_id,
            json.dumps(target, ensure_ascii=False), json.dumps(draft, ensure_ascii=False), now))
        for key in keys:
            db.execute('''INSERT INTO autonomy_delivery_keys VALUES(?,?,?,?) ON CONFLICT(role_id,key)
                DO UPDATE SET intent_id=excluded.intent_id,expires_at=excluded.expires_at''', (turn.origin.role_id, key, intent_id, now + 86400))
        if target['channel'] != 'desktop':
            retain_inputs(turn, db, intent_id, draft)
    # Cancellation or an unknown failure cannot restore the committed opportunity.
    try:
        return await turn.app.drift_delivery.deliver(turn, intent_id, draft)
    except BaseException as error:
        with turn.app._transaction() as db:
            db.execute("UPDATE autonomy_deliveries SET status='uncertain',error='original send did not return a definitive receipt' WHERE intent_id=? AND status='dispatching'", (intent_id,))
            current = db.execute('SELECT status,receipt FROM autonomy_deliveries WHERE intent_id=?', (intent_id,)).fetchone()
        if not isinstance(error, Exception):
            raise
        if current is not None and current['status'] == 'sent' and current['receipt']:
            return json.loads(current['receipt'])
        return {'status': 'uncertain', 'intentId': intent_id, 'targetRef': target['ref']}


def actual_outcome(row):
    if row is None:
        return 'silent'
    if not row['dispatched'] or row['intent_id'] != hashlib.sha256(('drift:' + row['role_id'] + ':' + row['tick_id']).encode()).hexdigest():
        raise ValueError('Drift outcome has no original irreversible claim')
    if row['status'] in {'sent', 'failed'}:
        receipt = json.loads(row['receipt'] or '{}')
        if receipt.get('intentId') != row['intent_id'] or receipt.get('targetRef') != json.loads(row['target']).get('ref'):
            raise ValueError('Drift receipt does not match its original intent and target')
    if row['status'] == 'sent':
        if receipt.get('status') != 'sent' or not row['message_id'] or receipt.get('messageId') != row['message_id']:
            raise ValueError('Drift sent state has no matching canonical receipt')
        return 'sent'
    if row['status'] == 'failed':
        if receipt.get('status') != 'failed' or row['message_id']:
            raise ValueError('Drift failed state has no matching definitive receipt')
        return 'failed'
    return 'uncertain'


async def reconcile_receipts(app):
    """Only ended, copied and acknowledged Runtime runs may recover send facts."""
    from .identity import AutonomyOrigin
    with app._transaction() as db:
        cursor = db.execute('SELECT last_delivery_rowid FROM autonomy_drift_receipt_scan WHERE singleton=1').fetchone()[0]
        query = """SELECT d.rowid AS delivery_rowid,d.*,r.origin FROM autonomy_deliveries d
            JOIN autonomy_runs r ON r.tick_id=d.tick_id WHERE r.mode='drift' AND r.runtime_acknowledged=1
            AND COALESCE(CASE WHEN json_valid(d.target) THEN json_extract(d.target,'$.channel') END,'desktop')='desktop'
            AND r.runtime_result IS NOT NULL AND d.status IN ('dispatching','pending','uncertain')"""
        claims = [dict(row) for row in db.execute(query + ' AND d.rowid>? ORDER BY d.rowid LIMIT 32', (cursor,))]
        if not claims:
            claims = [dict(row) for row in db.execute(query + ' AND d.rowid<=? ORDER BY d.rowid LIMIT 32', (cursor,))]
    for claim in claims:
        # This is only a durable observation position, never a successful
        # receipt or released claim. Advance before lookup so a failing reader
        # cannot permanently block the next original obligation after restart.
        with app._transaction() as db:
            db.execute('UPDATE autonomy_drift_receipt_scan SET last_delivery_rowid=? WHERE singleton=1', (claim['delivery_rowid'],))
        origin = AutonomyOrigin.restore(claim['origin'])
        # Validate the exact old claim before any receipt lookup. Revocation
        # does not erase an already-owned delivery's historical receipt duty.
        actual_outcome(claim)
        reader = getattr(app.drift_delivery, 'receipt_for', None)
        receipt = await reader(origin, claim) if reader is not None else None
        status = receipt['status'] if receipt is not None else 'uncertain'
        candidate = {**claim, 'status': status, 'receipt': json.dumps(receipt),
            'message_id': receipt.get('messageId') if receipt else None}
        actual_outcome(candidate)
        with app._transaction() as db:
            db.execute("""UPDATE autonomy_deliveries SET status=?,receipt=?,message_id=?,error=?
                WHERE intent_id=? AND tick_id=? AND status IN ('dispatching','pending','uncertain')""",
                (status, json.dumps(receipt) if receipt else None, candidate['message_id'],
                 '' if receipt else 'original send has no definitive receipt', claim['intent_id'], origin.tick_id))
