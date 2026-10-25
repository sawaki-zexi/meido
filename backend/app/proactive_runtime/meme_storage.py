"""Bounded actual filesystem scans plus finite, durable in-flight peak claims."""
from contextlib import contextmanager

from . import retention
from .drift_workspace import _ALLOCATION_GATE, _scan
from .source_authoring import _directory_identity

JOURNAL_HEADROOM = 64 * 1024


class MemeQuotaError(ValueError):
    """A finite public storage cause, never an exception's arbitrary text."""
    code = 'meme_storage_quota_exhausted'


class MemeStorage:
    def __init__(self, library):
        self.library, self.app = library, library.app
        with self.app._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_meme_allocations (
                role_id TEXT PRIMARY KEY,committed_bytes INTEGER NOT NULL,committed_items INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_meme_reservations (
                operation_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,bytes INTEGER NOT NULL,items INTEGER NOT NULL)''')

    def _journal(self, db, role_id):
        size, items = 0, 0
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'autonomy_meme_*'")]
        for table in tables:
            columns = [row[1] for row in db.execute('PRAGMA table_info(' + retention._quote(table) + ')')]
            expression = retention._size_expression(columns)
            row = db.execute('SELECT COALESCE(SUM(' + expression + '),0),COUNT(*) FROM ' + retention._quote(table) + ' t WHERE role_id=?', (role_id,)).fetchone()
            size += row[0]
            items += row[1]
        # SQLite rows are byte charges with the original retention row policy;
        # they are one shared journal responsibility, not invented FS entries.
        return size, int(items > 0)

    def scan(self, role_id):
        root = self.library.root(role_id)
        private = root.parent / '.meido-memes'
        with self.app._transaction() as db:
            adopted = db.execute('SELECT 1 FROM autonomy_meme_roots WHERE role_id=?', (role_id,)).fetchone()
        if adopted is None:
            return 0, 0
        size, items = _scan(private)
        if self.library._grant(role_id).enabled:
            library_bytes, library_items = _scan(root)
            # The roots themselves are directory entries in the managed scope.
            size += library_bytes
            items += library_items + int(root.exists())
        if private.exists():
            items += 1
        return size, items

    def refresh(self, role_id):
        size, items = self.scan(role_id)
        with self.app._transaction() as db:
            db.execute('INSERT OR REPLACE INTO autonomy_meme_allocations VALUES(?,?,?)', (role_id, size, items))
        return size, items

    def refresh_all(self):
        # Unknown existing bytes in another adopted library are real global use.
        with self.app._transaction() as db:
            roles = [row[0] for row in db.execute('SELECT role_id FROM autonomy_roles LIMIT 1025')]
        if len(roles) > 1024:
            raise MemeQuotaError('global managed scope exceeds bounded role scan; allocation quota is unconfirmed')
        for role_id in roles:
            if self.app.role_getter(role_id) is None:
                continue
            self.refresh(role_id)
            root = self.library.root(role_id).parent / 'drift'
            if root.exists():
                size, items = _scan(root)
                with self.app._transaction() as db:
                    db.execute('''INSERT INTO autonomy_drift_allocations VALUES(?,?,?,?,?,0,0)
                        ON CONFLICT(role_id) DO UPDATE SET committed_bytes=excluded.committed_bytes,
                        committed_items=excluded.committed_items''',
                        (role_id, str(root), str(_directory_identity(root)), size, items))

    def extra(self, role_id):
        with self.app._transaction() as db:
            responsibility = db.execute("SELECT 1 FROM autonomy_meme_operations WHERE role_id=? AND status NOT IN ('recorded','not_published','reconciled')", (role_id,)).fetchone()
        if not self.library._grant(role_id).enabled and responsibility is None:
            return 0, 0
        size, items = self.refresh(role_id)
        with self.app._transaction() as db:
            journal_size, journal_items = self._journal(db, role_id)
            reserved = db.execute('SELECT COALESCE(SUM(bytes),0),COALESCE(SUM(items),0) FROM autonomy_meme_reservations WHERE role_id=?', (role_id,)).fetchone()
        return size + journal_size + reserved[0], items + journal_items + reserved[1]

    @contextmanager
    def claim(self, role_id, operation_id, byte_peak, item_peak, context):
        acquired = False
        try:
            while not acquired:
                if context.signal.is_cancelled():
                    raise PermissionError('meme storage reservation was cancelled')
                acquired = _ALLOCATION_GATE.acquire(timeout=.05)
            self.refresh_all()
            size, items = self.scan(role_id)
            drift_size, drift_items = _scan(self.library.root(role_id).parent / 'drift')
            with self.app._transaction() as db:
                journal_size, journal_items = self._journal(db, role_id)
                reserved = db.execute('SELECT COALESCE(SUM(bytes),0),COALESCE(SUM(items),0) FROM autonomy_meme_reservations WHERE role_id=?', (role_id,)).fetchone()
                limits = self.app.drift_files.limits
                if (size + drift_size + journal_size + reserved[0] + byte_peak + JOURNAL_HEADROOM > limits.role_bytes
                        or items + drift_items + journal_items + reserved[1] + item_peak + 2 > limits.role_items):
                    raise MemeQuotaError('managed meme and Drift combined peak allocation quota exhausted before staging')
                db.execute('INSERT INTO autonomy_meme_reservations VALUES(?,?,?,?)',
                    (operation_id, role_id, byte_peak + JOURNAL_HEADROOM, item_peak + 2))
                retention.check_capacity(db, role_id)
        finally:
            if acquired:
                _ALLOCATION_GATE.release()
        try:
            yield
        finally:
            with _ALLOCATION_GATE:
                self.refresh(role_id)
                with self.app._transaction() as db:
                    row = db.execute('SELECT status FROM autonomy_meme_operations WHERE operation_id=?', (operation_id,)).fetchone()
                    if row is None or row[0] in {'recorded', 'not_published', 'reconciled'}:
                        db.execute('DELETE FROM autonomy_meme_reservations WHERE operation_id=?', (operation_id,))

    def reconciled(self, role_id, operation_id, status):
        with _ALLOCATION_GATE:
            self.refresh(role_id)
            if status in {'recorded', 'not_published', 'reconciled'}:
                with self.app._transaction() as db:
                    db.execute('DELETE FROM autonomy_meme_reservations WHERE operation_id=?', (operation_id,))
