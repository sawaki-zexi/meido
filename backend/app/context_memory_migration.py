"""Restart-safe #302 import. Historical files remain restorable, never dual-written."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path


def migrate_context_memory(data_root: Path, roles_root: Path):
    data_root, roles_root = Path(data_root), Path(roles_root)
    marker = data_root / 'context-memory-v2.json'
    if marker.exists():
        return
    backup = data_root / 'backups' / 'context-memory-v1'
    backup.mkdir(parents=True, exist_ok=True)
    # Take a consistent SQLite copy before schema import. Each completed copy
    # is immutable across retries; a .tmp is never accepted as a backup.
    for name in ('sessions.db', 'memory.db', 'memory2.db', 'advanced-settings.db', 'embedding-config.json', 'memory-settings.json'):
        source, target = data_root / name, backup / name
        if source.exists() and not target.exists():
            temporary = target.with_suffix(target.suffix + '.tmp')
            if source.suffix == '.db':
                with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(temporary)) as dst:
                    src.backup(dst)
            else:
                shutil.copy2(source, temporary)
            os.replace(temporary, target)
    if roles_root.exists():
        for role in roles_root.iterdir():
            memory = role / 'memory'
            if not role.is_dir() or role.name.startswith('.') or not memory.is_dir():
                continue
            for source in memory.rglob('*'):
                if not source.is_file():
                    continue
                target = backup / 'roles' / role.name / 'memory' / source.relative_to(memory)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_suffix(target.suffix + '.tmp')
                    shutil.copy2(source, temporary)
                    os.replace(temporary, target)
            for name in ('SELF.md', 'MEMORY.md'):
                path = memory / name
                if path.exists():
                    original = path.read_text(encoding='utf-8')
                    updated = original.replace('<!-- meido-optimizer:start -->', '').replace('<!-- meido-optimizer:end -->', '')
                    if updated != original:
                        temporary = path.with_suffix('.migration.tmp')
                        temporary.write_text(updated, encoding='utf-8')
                        os.replace(temporary, path)
    independent = data_root / 'memory-embedding-config.json'
    shared = data_root / 'embedding-config.json'
    if shared.exists() and not independent.exists():
        temporary = independent.with_suffix('.tmp')
        shutil.copy2(shared, temporary)
        os.replace(temporary, independent)
    temporary = marker.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 2, 'backup': str(backup.resolve()),
        'upstream': 'c14152ed225954a2a50da775075fe64dbf7b8b50'}, indent=2), encoding='utf-8')
    os.replace(temporary, marker)
