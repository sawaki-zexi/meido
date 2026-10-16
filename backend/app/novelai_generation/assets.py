"""Optional role-card CAS. Its local failures never repeat paid generation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from ..chat_attachments import resolve_attachment
from .ledger import GLOBAL_MEDIA_BYTES, ROLE_MEDIA_BYTES
from .media import validate_image


class AssetWriteback:
    def __init__(self, application):
        self.app = application
        with self.app.ledger.connect() as db:
            db.execute("UPDATE novelai_asset_writes SET status='uncertain' WHERE status='writing'")

    def apply(self, row, source):
        if row['purpose'] != 'scene_cg':
            return
        with self.app._lock:
            role = self.app.roles.get(row['role_id'])
            if role is None or not role.agentConfig.novelai.autoWritebackRoleAssets or not self.app._authorized(source, row['service_revision'], row['role_revision']):
                return
            attachments = json.loads(row['attachments'])
            if len(attachments) != 1:
                return
            with self.app.ledger.connect() as db:
                if db.execute('SELECT 1 FROM novelai_asset_writes WHERE attempt_id=?', (row['attempt_id'],)).fetchone():
                    return
            image = resolve_attachment(self.app.roles.workspace_path(role.id), attachments[0]['id'])
            if image is None:
                return
            input_path = image.get('path')
            if not isinstance(input_path, str):
                return
            with Path(input_path).open('rb') as handle:
                content = handle.read(attachments[0]['size'] + 1)
            if len(content) != attachments[0]['size']:
                raise ValueError('generated card bytes changed')
            parameters = json.loads(row['parameters'])
            validate_image(content, max_pixels=parameters['width'] * parameters['height'])
            expected = json.loads(row['scene_input'])['cardImageUrl']
            digest = hashlib.sha256(content).hexdigest()
            with self.app.ledger.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                used = db.execute('SELECT role_id,SUM(media_bytes+reserved_bytes) AS bytes FROM novelai_attempts GROUP BY role_id').fetchall()
                assets = db.execute('SELECT * FROM novelai_asset_usage').fetchall()
                busy = any(item['role_id'] == role.id and item['reserved_bytes'] for item in assets)
                total = sum(item['bytes'] for item in used) + sum(item['bytes'] + item['reserved_bytes'] for item in assets)
                own = sum(item['bytes'] for item in used if item['role_id'] == role.id) + sum(item['bytes'] + item['reserved_bytes'] for item in assets if item['role_id'] == role.id)
                status = 'failed' if busy or total + len(content) > GLOBAL_MEDIA_BYTES or own + len(content) > ROLE_MEDIA_BYTES else 'writing'
                db.execute('INSERT INTO novelai_asset_writes VALUES(?,?,?,?,?,?)', (row['attempt_id'], role.id, status, len(content), digest, expected))
                if status == 'failed':
                    return
                db.execute('''INSERT INTO novelai_asset_usage(role_id,reserved_bytes,attempt_id) VALUES(?,?,?)
                    ON CONFLICT(role_id) DO UPDATE SET reserved_bytes=excluded.reserved_bytes,attempt_id=excluded.attempt_id''', (role.id, len(content), row['attempt_id']))
            try:
                self.app.roles.set_avatar(role.id, content, image['mediaType'], card=True, expected_card_url=expected,
                    authorized=lambda: self.app._authorized(source, row['service_revision'], row['role_revision']),
                    operation_id=row['attempt_id'])
            except PermissionError:
                self._settle(row['attempt_id'], role.id, 'failed', 0)
                return
            except OSError:
                # The manifest may already have committed. Keep bytes reserved
                # until read-only local reconciliation can establish the fact.
                with self.app.ledger.connect() as db:
                    db.execute("UPDATE novelai_asset_writes SET status='uncertain' WHERE attempt_id=?", (row['attempt_id'],))
                return
            self._settle(row['attempt_id'], role.id, 'applied', len(content))

    def _settle(self, attempt_id, role_id, status, size):
        with self.app.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE novelai_asset_writes SET status=? WHERE attempt_id=?', (status, attempt_id))
            if status == 'applied':
                db.execute('UPDATE novelai_asset_usage SET bytes=?,reserved_bytes=0 WHERE role_id=? AND attempt_id=?', (size, role_id, attempt_id))
            else:
                db.execute('UPDATE novelai_asset_usage SET reserved_bytes=0 WHERE role_id=? AND attempt_id=?', (role_id, attempt_id))

    def recover(self):
        with self.app.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('SELECT after_attempt FROM novelai_asset_cursor WHERE singleton=1').fetchone()[0]
            query = "SELECT * FROM novelai_asset_writes WHERE status IN ('writing','uncertain')"
            rows = db.execute(query + ' AND attempt_id>? ORDER BY attempt_id LIMIT 100', (cursor,)).fetchall()
            if not rows:
                rows = db.execute(query + ' ORDER BY attempt_id LIMIT 100').fetchall()
            if rows:
                db.execute('UPDATE novelai_asset_cursor SET after_attempt=? WHERE singleton=1', (rows[-1]['attempt_id'],))
        for row in rows:
            role = self.app.roles.get(row['role_id'])
            if role is None:
                continue
            try:
                applied = self.app.roles.reconcile_generated_card(role.id, operation_id=row['attempt_id'],
                    expected_card_url=row['expected_url'], digest=row['digest'], size=row['size'])
                if applied is None:
                    continue
                self._settle(row['attempt_id'], role.id, 'applied' if applied else 'failed', row['size'] if applied else 0)
            except (OSError, sqlite3.Error, KeyError, ValueError):
                continue
