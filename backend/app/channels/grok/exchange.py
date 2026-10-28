"""Bounded reply-chain admission; outbound delivery stays in the host ledger."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .protocol import BotMessage, CallbackError

MAX_RECEIPTS = 512
MAX_PENDING = 16
PROTECTED = {'queued', 'sending', 'uncertain'}


class ExchangeJournal:
    def __init__(self, database_path: str, role_id: str, account_id: str, peer_id: str):
        self.path, self.role_id, self.account_id, self.peer_id = database_path, role_id, account_id, peer_id
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS channel_grok_exchanges (
                role_id TEXT NOT NULL, account_id TEXT NOT NULL, peer_id TEXT NOT NULL,
                state_json TEXT NOT NULL, PRIMARY KEY(account_id, peer_id))''')
        with self._state() as state:
            for receipt in state['receipts'].values():
                if receipt['status'] == 'sending':
                    receipt['status'] = 'uncertain'

    @contextmanager
    def _state(self):
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state_json FROM channel_grok_exchanges WHERE account_id=? AND peer_id=?',
                             (self.account_id, self.peer_id)).fetchone()
            state = json.loads(row[0]) if row else {'receipts': {}, 'counts': {}}
            yield state
            db.execute('INSERT INTO channel_grok_exchanges VALUES(?,?,?,?) ON CONFLICT(account_id,peer_id) DO UPDATE SET state_json=excluded.state_json',
                       (self.role_id, self.account_id, self.peer_id, json.dumps(state, ensure_ascii=False)))

    def begin(self, message: BotMessage, direction: str, limit: int) -> tuple[bool, dict]:
        digest = hashlib.sha256(json.dumps([message.text, message.reply_to], ensure_ascii=False).encode()).hexdigest()
        with self._state() as state:
            receipts, counts = state['receipts'], state['counts']
            previous = receipts.get(message.message_id)
            if previous is not None:
                if previous['direction'] != direction or previous['digest'] != digest:
                    raise CallbackError(409, '消息标识已被其他内容使用')
                return False, dict(previous)
            root = message.message_id
            if message.reply_to:
                parent = receipts.get(message.reply_to)
                if parent is None or parent['direction'] == direction or parent['status'] == 'failed':
                    raise CallbackError(409, '父消息不存在、已过期或不属于当前交流')
                root = parent['root']
            if counts.get(root, 0) >= limit:
                raise CallbackError(409, '已达单次交流消息上限')
            if direction == 'in' and sum(r['status'] == 'queued' for r in receipts.values()) >= MAX_PENDING:
                raise CallbackError(429, '待处理消息过多，请稍后再试')
            # Evict complete chains only, preserving their counters and ancestors together.
            while len(receipts) >= MAX_RECEIPTS:
                protected = {r['root'] for r in receipts.values() if r['status'] in PROTECTED} | {root}
                expired = next((key for key in counts if key not in protected), None)
                if expired is None:
                    raise CallbackError(429, '交流收据容量已满，请稍后再试')
                for key in [key for key, r in receipts.items() if r['root'] == expired]:
                    del receipts[key]
                del counts[expired]
            receipt = {'root': root, 'direction': direction, 'digest': digest, 'reply_to': message.reply_to,
                       'status': 'queued' if direction == 'in' else 'sending',
                       'text': message.text if direction == 'in' else ''}
            receipts[message.message_id] = receipt
            counts[root] = counts.get(root, 0) + 1
            return True, dict(receipt)

    def finish(self, message_id: str, status: str):
        with self._state() as state:
            receipt = state['receipts'].get(message_id)
            if receipt is not None:
                receipt.update(status=status, text='')

    def pending(self) -> list[BotMessage]:
        with self._state() as state:
            return [BotMessage(key, item['text'], item['reply_to'])
                    for key, item in state['receipts'].items() if item['status'] == 'queued']
