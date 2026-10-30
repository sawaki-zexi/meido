"""Durable model-input windows. Raw history remains authoritative and intact."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def _stamp(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, default=str).encode()).hexdigest()


class WindowConflict(ValueError):
    pass


class ContextWindowStore:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS context_windows (
                role_id TEXT NOT NULL, session_key TEXT NOT NULL, scope TEXT NOT NULL,
                version INTEGER NOT NULL, authority TEXT NOT NULL, removed_ids TEXT NOT NULL,
                fingerprint TEXT NOT NULL, summary TEXT NOT NULL,
                PRIMARY KEY(role_id, session_key, scope))''')
            # All history removal paths (including channel undo) invalidate affected summaries.
            db.execute('''CREATE TRIGGER IF NOT EXISTS context_window_message_delete AFTER DELETE ON messages
                BEGIN DELETE FROM context_windows WHERE session_key=OLD.session_key AND EXISTS
                (SELECT 1 FROM json_each(removed_ids) WHERE value=OLD.message_id); END''')
            db.execute('''CREATE TRIGGER IF NOT EXISTS context_window_message_update AFTER UPDATE ON messages
                WHEN OLD.content!=NEW.content OR OLD.status!=NEW.status OR OLD.metadata_json IS NOT NEW.metadata_json
                BEGIN DELETE FROM context_windows WHERE session_key=OLD.session_key AND EXISTS
                (SELECT 1 FROM json_each(removed_ids) WHERE value=OLD.message_id); END''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def fingerprint(db, session, ids):
        # Bound parameters for older SQLite builds; preserve original ordering
        # and duplicate IDs so existing persisted fingerprints remain valid.
        identifiers = list(dict.fromkeys(ids))
        found = {}
        for start in range(0, len(identifiers), 500):
            batch = identifiers[start:start + 500]
            rows = db.execute('SELECT message_id, role, content, status, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, metadata_json FROM messages WHERE session_key=? AND message_id IN (' + ','.join('?' for _ in batch) + ')',
                              (session, *batch)).fetchall()
            found.update((row[0], row) for row in rows)
        if len(found) != len(identifiers):
            raise WindowConflict('工作摘要来源已变化')
        return _stamp([found[identifier] for identifier in ids])

    def load(self, role, session, scope, authority):
        with self.connect() as db:
            row = db.execute('SELECT version, authority, removed_ids, fingerprint, summary FROM context_windows WHERE role_id=? AND session_key=? AND scope=?',
                             (role, session, scope)).fetchone()
            if row is None:
                return {'version': 0, 'removed': [], 'summary': None}
            version, stored_authority, ids, fingerprint, summary = row
            ids = json.loads(ids)
            try:
                valid = stored_authority == authority and self.fingerprint(db, session, ids) == fingerprint
            except WindowConflict:
                valid = False
            if not valid:
                # Keep a version tombstone so an in-flight writer cannot recreate a stale cut.
                db.execute('UPDATE context_windows SET version=version+1, authority=?, removed_ids=\'[]\', fingerprint=?, summary=\'null\' WHERE role_id=? AND session_key=? AND scope=?',
                           (authority, _stamp([]), role, session, scope))
                return {'version': version + 1, 'removed': [], 'summary': None}
            return {'version': version, 'removed': ids, 'summary': json.loads(summary)}

    def prepare(self, role, session, scope, authority, ids):
        state = self.load(role, session, scope, authority)
        with self.connect() as db:
            fingerprint = self.fingerprint(db, session, ids)
        return {**state, 'role': role, 'session': session, 'scope': scope, 'authority': authority,
                'ids': list(ids), 'fingerprint': fingerprint}

    def commit(self, prepared, summary, *, authorized, covered):
        if not covered:
            raise WindowConflict('待逐出消息尚未完成记忆维护')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT version FROM context_windows WHERE role_id=? AND session_key=? AND scope=?',
                             (prepared['role'], prepared['session'], prepared['scope'])).fetchone()
            if (not authorized() or (row[0] if row else 0) != prepared['version']
                    or self.fingerprint(db, prepared['session'], prepared['ids']) != prepared['fingerprint']
                    or not authorized()):
                raise WindowConflict('工作窗口或来源许可已变化，请重新开始回复')
            db.execute('INSERT OR REPLACE INTO context_windows VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (prepared['role'], prepared['session'], prepared['scope'], prepared['version'] + 1,
                        prepared['authority'], json.dumps(prepared['ids']), prepared['fingerprint'], json.dumps(summary, ensure_ascii=False)))

    def snapshot_role(self, role):
        with self.connect() as db:
            return db.execute('SELECT * FROM context_windows WHERE role_id=?', (role,)).fetchall()

    def restore_role(self, role, rows):
        with self.connect() as db:
            db.execute('DELETE FROM context_windows WHERE role_id=?', (role,))
            db.executemany('INSERT INTO context_windows VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)

    def delete_role(self, role):
        self.restore_role(role, [])
