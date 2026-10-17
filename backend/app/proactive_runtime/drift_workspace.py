"""Reserve actual Drift filesystem peaks before shared FileTools performs I/O.

The ledger includes unknown on-disk entries. Journals describe local file facts,
never a request that recovery may execute. One process owns the TaskRuntime; the
global allocation lock also orders ordinary owner writes against autonomous ones.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import threading
import uuid
import asyncio

from .context import _check_chain
from .source_authoring import _directory_identity
from .source_validation import _read
from . import retention

_ALLOCATION_GATE = threading.RLock()
MIB = 1024 * 1024


@dataclass(frozen=True)
class FileLimits:
    role_bytes: int = 64 * MIB
    role_items: int = 256
    file_bytes: int = 8 * MIB
    headroom_bytes: int = 16 * 1024
    headroom_items: int = 2


def _identity(path):
    _check_chain(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PermissionError('managed file is not a unique regular file')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _scan(root):
    if not root.exists():
        return 0, 0
    _directory_identity(root)
    count, size, pending, seen = 0, 0, [root], set()
    while pending:
        directory = pending.pop()
        _check_chain(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 512:
                    raise ValueError('managed Drift directory exceeds bounded scan')
                path = Path(entry.path)
                key = path.relative_to(root).as_posix().casefold()
                if key in seen:
                    raise PermissionError('managed Drift paths cannot collide by case')
                seen.add(key)
                # DirEntry's Windows fast stat can omit nlink; lstat the actual
                # path through the same checked chain used by file operations.
                info = _check_chain(path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    size += info.st_size
                else:
                    raise PermissionError('managed Drift directory contains an unsupported file')
    return size, count


class DriftFiles:
    def __init__(self, app, workspace_getter, *, limits=FileLimits()):
        self.app, self.workspace_getter, self.limits = app, workspace_getter, limits
        with app._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_drift_allocations (
                role_id TEXT PRIMARY KEY,root TEXT NOT NULL,root_identity TEXT NOT NULL,
                committed_bytes INTEGER NOT NULL,committed_items INTEGER NOT NULL,
                reserved_bytes INTEGER NOT NULL,reserved_items INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_drift_write_journal (
                journal_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,path TEXT NOT NULL,temporary TEXT NOT NULL,
                root_identity TEXT NOT NULL,digest TEXT NOT NULL,bytes INTEGER NOT NULL,status TEXT NOT NULL)''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(autonomy_drift_write_journal)')}
            if 'temp_identity' not in columns:
                db.execute("ALTER TABLE autonomy_drift_write_journal ADD COLUMN temp_identity TEXT NOT NULL DEFAULT ''")
            if 'operation_id' not in columns:
                db.execute("ALTER TABLE autonomy_drift_write_journal ADD COLUMN operation_id TEXT NOT NULL DEFAULT ''")

    def policy(self, role_id, *, host_projection=False):
        @contextmanager
        def policy(path, data, context):
            root = self.workspace_getter(role_id) / 'drift'
            try:
                relative = path.relative_to(root)
            except ValueError:
                yield None
                return
            acquired = False
            try:
                while not acquired:
                    if context.signal.is_cancelled():
                        raise ValueError('managed Drift write cancelled')
                    acquired = _ALLOCATION_GATE.acquire(timeout=.05)
                if len(data) > self.limits.file_bytes:
                    raise ValueError('managed Drift file exceeds 8 MiB')
                _check_chain(root.parent)
                if not root.exists():
                    root.mkdir()
                identity = str(_directory_identity(root))
                parents = {}
                parent = root
                parents[parent] = _directory_identity(parent)
                new_directories = 0
                for part in relative.parts[:-1]:
                    parent = parent / part
                    if parent.exists():
                        parents[parent] = _directory_identity(parent)
                    else:
                        new_directories += 1
                old_identity = _identity(path) if path.exists() else None
                size, items = _scan(root)
                meme_size, meme_items = self.app.memes.storage.extra(role_id) if hasattr(self.app, 'memes') else (0, 0)
                temporary = path.parent / ('.meido-' + uuid.uuid4().hex + '.tmp')
                journal_id = uuid.uuid4().hex
                head_bytes = 0 if host_projection else self.limits.headroom_bytes
                head_items = 0 if host_projection else self.limits.headroom_items
                # Old target and temporary new copy coexist until atomic replace.
                if (size + meme_size + len(data) + head_bytes > self.limits.role_bytes
                        or items + meme_items + new_directories + 1 + head_items > self.limits.role_items):
                    self._settle(role_id, root, identity, size, items)
                    raise ValueError('managed Drift allocation quota exhausted before write')
                with self.app._transaction() as db:
                    if db.execute("SELECT 1 FROM autonomy_drift_write_journal WHERE role_id=? AND status='pending'", (role_id,)).fetchone():
                        raise ValueError('managed Drift write requires journal reconciliation')
                    db.execute('''INSERT INTO autonomy_drift_allocations VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(role_id) DO UPDATE SET root=excluded.root,root_identity=excluded.root_identity,
                        committed_bytes=excluded.committed_bytes,committed_items=excluded.committed_items,
                        reserved_bytes=excluded.reserved_bytes,reserved_items=excluded.reserved_items''',
                        (role_id, str(root), identity, size, items, len(data) + self.limits.headroom_bytes,
                         new_directories + 1 + self.limits.headroom_items))
                    db.execute('''INSERT INTO autonomy_drift_write_journal
                        (journal_id,role_id,path,temporary,root_identity,digest,bytes,status,operation_id) VALUES(?,?,?,?,?,?,?,'pending',?)''',
                        (journal_id, role_id, str(path), str(temporary), identity, hashlib.sha256(data).hexdigest(), len(data), context.run_id))
                    retention.check_capacity(db, role_id)
                write = _Write(self, role_id, root, path, temporary, parents, old_identity, journal_id,
                    hashlib.sha256(data).hexdigest(), len(data))
                try:
                    yield write
                finally:
                    # This is factual reconciliation only, including failure/cancel.
                    if str(_directory_identity(root)) == identity and not temporary.exists():
                        size, items = _scan(root)
                        self._settle(role_id, root, identity, size, items, journal_id,
                            'committed' if write.replaced else 'discarded')
            finally:
                if acquired:
                    _ALLOCATION_GATE.release()
        return policy

    def pending(self, role_id, connection=None):
        if connection is not None:
            return connection.execute("SELECT 1 FROM autonomy_drift_write_journal WHERE role_id=? AND status='pending'", (role_id,)).fetchone() is not None
        with self.app._transaction() as db:
            return db.execute("SELECT 1 FROM autonomy_drift_write_journal WHERE role_id=? AND status='pending'", (role_id,)).fetchone() is not None

    def admit(self, role_id):
        with _ALLOCATION_GATE:
            workspace = self.workspace_getter(role_id)
            parent_identity = _directory_identity(workspace.parent)
            workspace.mkdir(exist_ok=True)
            _directory_identity(workspace)
            if _directory_identity(workspace.parent) != parent_identity:
                raise PermissionError('role workspace parent changed')
            root = workspace / 'drift'
            root.mkdir(exist_ok=True)
            self.reconcile(role_id)
            size, items = _scan(root)
            if hasattr(self.app, 'memes'):
                self.app.memes.storage.refresh_all()
            meme_size, meme_items = self.app.memes.storage.extra(role_id) if hasattr(self.app, 'memes') else (0, 0)
            if size + meme_size + self.limits.headroom_bytes > self.limits.role_bytes or items + meme_items + self.limits.headroom_items > self.limits.role_items:
                from .meme_storage import MemeQuotaError
                raise MemeQuotaError('managed Drift and meme combined allocation quota exhausted')
            with self.app._transaction() as db:
                retention.check_capacity(db, role_id)
            return _directory_identity(root)

    def project(self, role_id, filename, content, *, expected_root_identity):
        from ..agent_runtime.file_tools import build_file_tools
        from ..agent_runtime.provider import CancellationToken
        from ..agent_runtime.tools import ToolContext
        root = self.workspace_getter(role_id) / 'drift'
        signal = CancellationToken()
        context = ToolContext(role_id, 'drift-projection:' + role_id, 'drift-projection', signal)
        tool = build_file_tools(role_id, root, is_owner=False,
            authorized=lambda current: (current.role_id, current.session_key, current.run_id) ==
                (context.role_id, context.session_key, context.run_id) and self.app.role_getter(role_id) is not None
                and str(_directory_identity(root)) == expected_root_identity,
            max_file_bytes=self.limits.file_bytes, write_policy=self.policy(role_id, host_projection=True))[2]
        result = asyncio.run(tool.execute({'path': 'state/' + filename, 'content': content}, context))
        if result.is_error:
            raise ValueError('durable Drift progress projection remains pending: ' + result.content)

    def _settle(self, role_id, root, identity, size, items, journal_id=None, status=None):
        with self.app._transaction() as db:
            db.execute('''INSERT INTO autonomy_drift_allocations VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(role_id) DO UPDATE SET root_identity=excluded.root_identity,
                committed_bytes=excluded.committed_bytes,committed_items=excluded.committed_items,
                reserved_bytes=excluded.reserved_bytes,reserved_items=excluded.reserved_items''',
                (role_id, str(root), identity, size, items, self.limits.headroom_bytes, self.limits.headroom_items))
            if journal_id is not None:
                db.execute('UPDATE autonomy_drift_write_journal SET status=? WHERE journal_id=?', (status, journal_id))

    def reconcile(self, role_id):
        """A stopped owner may remove its exact orphan temp; it never replays writes."""
        with _ALLOCATION_GATE:
            root = self.workspace_getter(role_id) / 'drift'
            if not root.exists():
                return
            identity = str(_directory_identity(root))
            with self.app._transaction() as db:
                rows = db.execute("SELECT * FROM autonomy_drift_write_journal WHERE role_id=? AND status='pending'", (role_id,)).fetchall()
            for row in rows:
                if row['root_identity'] != identity:
                    raise PermissionError('cannot reconcile a replaced Drift root')
                temporary, target = Path(row['temporary']), Path(row['path'])
                if not temporary.is_relative_to(root) or not target.is_relative_to(root):
                    raise PermissionError('invalid managed journal identity')
                if temporary.exists():
                    if not row['temp_identity'] or str(_identity(temporary)[:2]) != row['temp_identity']:
                        raise PermissionError('orphan temporary identity no longer belongs to this journal')
                    temporary.unlink()
                status = 'discarded'
                if target.exists() and hashlib.sha256(_read(target, self.limits.file_bytes)).hexdigest() == row['digest']:
                    status = 'committed'
                size, items = _scan(root)
                self._settle(role_id, root, identity, size, items, row['journal_id'], status)
            size, items = _scan(root)
            self._settle(role_id, root, identity, size, items)


class _Write:
    def __init__(self, owner, role_id, root, path, temporary, parents, old_identity, journal_id, digest, size):
        self.owner, self.role_id, self.root, self.path, self.temporary = owner, role_id, root, path, temporary
        self.parents, self.old_identity, self.journal_id = parents, old_identity, journal_id
        self.replaced = False
        self.digest, self.size = digest, size
        self.temp_identity = None

    def opened(self, stream):
        info = os.fstat(stream.fileno())
        self.temp_identity = (info.st_dev, info.st_ino)
        if _identity(self.temporary)[:2] != self.temp_identity:
            raise PermissionError('managed temporary changed while opening')
        with self.owner.app._transaction() as db:
            db.execute('UPDATE autonomy_drift_write_journal SET temp_identity=? WHERE journal_id=?',
                (str(self.temp_identity), self.journal_id))

    def cleanup(self):
        if not self.temporary.exists():
            return
        for parent, identity in self.parents.items():
            if _directory_identity(parent) != identity:
                raise PermissionError('cannot remove a temporary through a changed parent')
        if self.temp_identity is None or _identity(self.temporary)[:2] != self.temp_identity:
            raise PermissionError('cannot remove a foreign same-path temporary')
        self.temporary.unlink()

    def _contents(self, path):
        _identity(path)
        value = _read(path, self.owner.limits.file_bytes)
        if len(value) != self.size or hashlib.sha256(value).hexdigest() != self.digest:
            raise PermissionError('managed Drift publication bytes changed')

    def check(self):
        for path, identity in self.parents.items():
            if _directory_identity(path) != identity:
                raise PermissionError('managed Drift parent directory changed')
        parent = self.root
        for part in self.path.relative_to(self.root).parts[:-1]:
            parent = parent / part
            self.parents.setdefault(parent, _directory_identity(parent))
        if (_identity(self.path) if self.path.exists() else None) != self.old_identity:
            raise PermissionError('managed Drift target changed before replace')
        if self.temporary.exists():
            self._contents(self.temporary)

    def committed(self):
        for path, identity in self.parents.items():
            if _directory_identity(path) != identity:
                raise PermissionError('managed Drift parent changed after publication')
        self._contents(self.path)
        self.replaced = True

    def receipt(self):
        return {'sha256': self.digest, 'identity': list(_identity(self.path))}
