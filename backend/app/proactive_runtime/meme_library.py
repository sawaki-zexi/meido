"""A current-role library seam; raw images live only in the owned next request."""
import base64
from _thread import LockType
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
from threading import Lock
import uuid
from weakref import WeakValueDictionary

from PIL import Image, ImageOps

from ..agent_runtime.file_tools import build_file_tools, _file_lock
from ..agent_runtime.tools import AgentToolResult
from ..agent_runtime.types import ImageContent
from .source_authoring import _directory_identity
from .source_validation import _read, _unique_object
from .drift_workspace import _identity
from .meme_profile import is_profile


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class MemeLibrary:
    def __init__(self, app):
        self.app = app
        self._recovery_gates: WeakValueDictionary[str, LockType] = WeakValueDictionary()
        self._recovery_gate_lock = Lock()
        with app._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_meme_observations (
                role_id TEXT NOT NULL,run_id TEXT NOT NULL,path TEXT NOT NULL,fact TEXT NOT NULL,
                epoch TEXT NOT NULL,PRIMARY KEY(role_id,run_id,path))''')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_meme_operations (
                operation_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,run_id TEXT NOT NULL,call_id TEXT NOT NULL,
                epoch TEXT NOT NULL,path TEXT NOT NULL,expected TEXT NOT NULL,digest TEXT NOT NULL,
                planned TEXT NOT NULL,temporary TEXT NOT NULL,status TEXT NOT NULL,actual TEXT NOT NULL DEFAULT '{}')''')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_meme_roots (
                role_id TEXT PRIMARY KEY,epoch TEXT NOT NULL,identity TEXT NOT NULL)''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(autonomy_meme_roots)')}
            if 'revision' not in columns:
                db.execute('ALTER TABLE autonomy_meme_roots ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_meme_model_requests (
                request_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,run_id TEXT NOT NULL,usage TEXT NOT NULL)''')
        from .meme_storage import MemeStorage
        self.storage = MemeStorage(self)

    def adopt(self, db, role_id, grant):
        root = self.root(role_id)
        _directory_identity(root.parent)
        root.mkdir(exist_ok=True)
        identity = _json(self._root_identity(role_id))
        previous = db.execute('SELECT epoch,identity,revision FROM autonomy_meme_roots WHERE role_id=?', (role_id,)).fetchone()
        if previous is not None and previous[0] == grant.managedEpoch and previous[1] != identity:
            raise ValueError('meme library root changed; disable and explicitly adopt the current library again')
        revision = previous[2] if previous is not None and previous[0] == grant.managedEpoch else 0
        db.execute('INSERT OR REPLACE INTO autonomy_meme_roots VALUES(?,?,?,?)', (role_id, grant.managedEpoch, identity, revision))

    def owner_tools(self, tools, role_id, authorized):
        return tuple(_OwnerLibraryTool(self, tool, role_id, authorized) for tool in tools)

    def status(self, role_id):
        self.app._require_role(role_id)
        with self.app._transaction() as db:
            rows = db.execute('SELECT operation_id,run_id,call_id,path,status,actual FROM autonomy_meme_operations WHERE role_id=? ORDER BY rowid DESC LIMIT 100', (role_id,)).fetchall()
            requests = db.execute('SELECT request_id,run_id,usage FROM autonomy_meme_model_requests WHERE role_id=? ORDER BY rowid DESC LIMIT 100', (role_id,)).fetchall()
        operations = []
        for row in rows:
            actual = json.loads(row['actual'])
            path = Path(row['path'])
            relative = '@memes/' + path.relative_to(self.root(role_id)).as_posix() if path.is_relative_to(self.root(role_id)) else 'unconfirmed'
            operations.append({'operationId': row['operation_id'], 'runId': row['run_id'], 'callId': row['call_id'],
                'path': relative, 'status': row['status'], 'physicalPhase': actual.get('physicalPhase', 'reserved')})
        model_requests = []
        for row in requests:
            usage = json.loads(row['usage'])
            model_requests.append({'requestId': row['request_id'], 'runId': row['run_id'],
                **{name: usage[name] for name in ('protocol', 'bodyBytes', 'imageOccurrences', 'imageBase64Bytes',
                    'imageInputEstimate', 'wireNonImageBytes', 'envelopeBytes', 'bodySha256')}})
        return {'enabled': self._grant(role_id).enabled, 'operations': operations, 'modelRequests': model_requests}

    def reconcile(self, role_id):
        from .meme_recovery import reconcile
        # Recovery owners for one role share one original factual projection.
        # This gate is never an owner file/publication lock or the application gate.
        # Waiting owners retain the same gate; unused role entries disappear.
        with self._recovery_gate_lock:
            gate = self._recovery_gates.get(role_id)
            if gate is None:
                gate = Lock()
                self._recovery_gates[role_id] = gate
        with gate:
            reconcile(self, role_id)

    def root(self, role_id):
        return self.app.drift_skills.workspace(role_id) / 'memes'

    def provenance(self, role_id, context, grant, turn=None):
        value = {'principal': 'autonomy' if turn is not None else 'owner', 'roleId': role_id,
            'sessionKey': context.session_key, 'runId': context.run_id, 'callId': context.tool_call_id,
            'capabilitySnapshotId': context.capability_snapshot_id, 'purpose': grant.model_dump(mode='json')}
        if turn is not None:
            value.update({'origin': turn.origin.public(), 'configRevision': turn.config.revision,
                'builtinBundleSha256': turn.bundle_hash, 'modelId': turn.config.driftModelConfigurationId,
                'modelDigests': self.app.frozen_bindings(turn.origin).get('models', {})})
        return value

    def _grant(self, role_id):
        return self.app.drift_skills.configuration(role_id).driftMeme

    def _fact(self, role_id, path):
        root = self.root(role_id)
        chain = self._root_identity(role_id)
        with self.app._transaction() as db:
            current = db.execute('SELECT revision FROM autonomy_meme_roots WHERE role_id=?', (role_id,)).fetchone()
        version = current[0] if current else 0
        if not path.exists():
            return {'root': chain, 'libraryRevision': version, 'file': None}
        if path.is_dir():
            from .drift_workspace import _scan
            size, items = _scan(path)
            return {'root': chain, 'libraryRevision': version, 'directory': list(_directory_identity(path)), 'size': size, 'items': items}
        raw = _read(path, 10 * 1024 * 1024)
        return {'root': chain, 'libraryRevision': version, 'file': list(_identity(path)), 'sha256': hashlib.sha256(raw).hexdigest()}

    def _fast_fact(self, role_id, path):
        chain = self._root_identity(role_id)
        with self.app._transaction() as db:
            current = db.execute('SELECT revision FROM autonomy_meme_roots WHERE role_id=?', (role_id,)).fetchone()
        return {'root': chain, 'libraryRevision': current[0] if current else 0,
            'file': list(_identity(path)) if path.exists() else None}

    def _matches(self, role_id, path, expected):
        if 'file' not in expected:
            return False
        return self._fast_fact(role_id, path) == {name: expected[name] for name in ('root', 'libraryRevision', 'file')}

    def advance_revision(self, db, role_id, grant, expected):
        if db.execute('UPDATE autonomy_meme_roots SET revision=revision+1 WHERE role_id=? AND epoch=? AND revision=?',
                (role_id, grant.managedEpoch, expected)).rowcount != 1:
            raise PermissionError('conflict: managed library revision changed during publication')

    def _root_identity(self, role_id):
        root = self.root(role_id)
        return [[str(parent), list(_directory_identity(parent))] for parent in (root.parent.parent, root.parent, root)]

    def freeze(self, turn):
        grant = turn.config.driftMeme
        if not grant.enabled:
            return None
        root = self.root(turn.origin.role_id)
        _directory_identity(root.parent)
        root.mkdir(exist_ok=True)
        with self.app._transaction() as db:
            adopted = db.execute('SELECT epoch,identity FROM autonomy_meme_roots WHERE role_id=?', (turn.origin.role_id,)).fetchone()
        if adopted is None or tuple(adopted) != (grant.managedEpoch, _json(self._root_identity(turn.origin.role_id))):
            raise PermissionError('meme library does not match the explicit original adoption')
        from .drift_attachments import bind
        return {'grant': grant, 'root': self._root_identity(turn.origin.role_id),
            'sources': {identifier: bind(self.app, turn.origin.role_id, identifier) for identifier in grant.sourceAttachmentIds}}

    def validate_scope(self, turn, context):
        frozen = turn.meme_scope
        if (frozen is None or not turn.file_authorized(context)
                or self._grant(turn.origin.role_id) != frozen['grant']
                or self._root_identity(turn.origin.role_id) != frozen['root']):
            raise PermissionError('meme purpose, original library identity or builtin activation changed')
        if is_profile(turn.skill):
            from .meme_profile import BUILTIN_ROOT
            try:
                digest = self.app.drift_skills._bundle(turn.origin.role_id, turn.skill, BUILTIN_ROOT)[3]
            except (ValueError, OSError):
                raise PermissionError('original full builtin meme instruction bundle changed') from None
            if digest != turn.bundle_hash:
                raise PermissionError('original full builtin meme instruction bundle changed')

    def authorize(self, turn, context, name, relative):
        self.validate_scope(turn, context)
        if not is_profile(turn.skill):
            raise PermissionError('shared meme access requires the actual activated fixed builtin profile')
        frozen = turn.meme_scope
        grant = frozen['grant']
        if name in {'write_file', 'edit_file'} and not grant.editEnabled:
            raise PermissionError('meme category edit purpose is disabled')
        if name == 'meme_copy_image' and not grant.copyEnabled:
            raise PermissionError('meme copy purpose is disabled')
        if not grant.readEnabled:
            raise PermissionError('meme library read purpose is disabled')
        if name == 'meme_copy_image' and relative not in grant.allowedCategories:
            raise PermissionError('meme copy category is outside the exact original finite purpose')
        parts = PurePosixPath(relative).parts
        if parts and parts[0] != 'manifest.json' and parts[0] not in grant.allowedCategories:
            raise PermissionError('meme category is outside the original finite purpose')

    async def execute(self, turn, name, arguments, context):
        raw = arguments['path']
        relative = raw.removeprefix('@memes').removeprefix('/')
        if raw not in {'@memes', '@memes/'} and not raw.startswith('@memes/'):
            raise PermissionError('meme library requires its fixed address space')
        if relative and (PurePosixPath(relative).is_absolute() or any(part in {'', '.', '..'} for part in relative.split('/')) or ':' in relative or '\\' in relative):
            raise PermissionError('meme library requires a canonical current-role path')
        self.authorize(turn, context, name, relative)
        path = self.root(turn.origin.role_id) / relative
        before = await self.app._owned(self._fact, turn.origin.role_id, path)
        tools = {tool.definition.name: tool for tool in build_file_tools(turn.origin.role_id, self.root(turn.origin.role_id),
            is_owner=False, multimodal=True, max_file_bytes=10 * 1024 * 1024,
            authorized=lambda item: self._authorized(turn, item, name, relative),
            write_policy=self.policy(turn.origin.role_id, turn=turn))}
        if name in {'write_file', 'edit_file'} and relative != 'manifest.json':
            raise PermissionError('meme purpose writes category manifest; image copies use the narrow copy tool')
        result = await tools[name].execute({**arguments, 'path': relative or '.'}, context)
        if result.is_error:
            return {'content': result.content, 'details': result.details, 'isError': True}
        self.authorize(turn, context, name, relative)
        after = await self.app._owned(self._fact, turn.origin.role_id, path)
        if name in {'read_file', 'list_dir'}:
            if before != after:
                raise PermissionError('meme library changed during its actual observation')
            with self.app._transaction() as db:
                db.execute('INSERT OR REPLACE INTO autonomy_meme_observations VALUES(?,?,?,?,?)',
                    (turn.origin.role_id, context.run_id, relative, _json(after), turn.config.driftMeme.managedEpoch))
        if result.images:
            turn.pending_images = await self.app._owned(self._visual_copies, result.images)
            self.authorize(turn, context, name, relative)
        details = {key: value for key, value in result.details.items() if key != 'publication'}
        details['libraryVersion'] = hashlib.sha256(_json(after).encode()).hexdigest()
        return {'content': result.content, 'details': details, 'isError': False}

    def _authorized(self, turn, context, name, relative):
        try:
            self.authorize(turn, context, name, relative)
            return True
        except (PermissionError, ValueError, OSError):
            return False

    @staticmethod
    def _visual_copies(images):
        result = []
        for image in images:
            raw = base64.b64decode(image.data_base64, validate=True)
            if len(raw) > 10 * 1024 * 1024:
                raise ValueError('meme source image exceeds 10 MiB')
            with Image.open(io.BytesIO(raw)) as opened:
                if opened.width * opened.height > 40_000_000:
                    raise ValueError('meme source image exceeds 40 million pixels')
                normalized = ImageOps.exif_transpose(opened)
                normalized.thumbnail((1568, 1568))
                canvas = Image.new('RGB', normalized.size, 'white')
                canvas.paste(normalized.convert('RGB'), mask=normalized.getchannel('A') if 'A' in normalized.getbands() else None)
                output = io.BytesIO()
                canvas.save(output, 'PNG', optimize=True)
                raw, media = output.getvalue(), 'image/png'
                if len(raw) > 768 * 1024:
                    output = io.BytesIO()
                    canvas.save(output, 'JPEG', quality=80, optimize=True)
                    raw, media = output.getvalue(), 'image/jpeg'
                if len(raw) > 768 * 1024 or canvas.width * canvas.height > 2_458_624:
                    raise ValueError('meme visual copy exceeds its actual byte or pixel budget')
            encoded = base64.b64encode(raw).decode('ascii')
            if len(encoded) > 1024 * 1024:
                raise ValueError('meme visual copy exceeds 1 MiB base64')
            result.append(ImageContent(media, encoded, 'high'))
        return tuple(result)

    def policy(self, role_id, *, turn=None, admission_grant=None):
        owner = self
        admitted = admission_grant if admission_grant is not None else turn.config.driftMeme if turn is not None else self._grant(role_id)
        @contextmanager
        def policy(path, data, context):
            grant = owner._grant(role_id)
            if grant != admitted:
                raise PermissionError('meme_permission_changed: original managed admission was sealed; retry this owner operation with current permission')
            if not grant.enabled:
                if turn is not None:
                    raise PermissionError('meme purpose was disabled')
                yield None
                return
            relative = path.relative_to(owner.root(role_id)).as_posix()
            if turn is not None and relative != 'manifest.json':
                raise PermissionError('meme category policy only accepts the manifest')
            with owner.app._transaction() as db:
                observed = db.execute('SELECT fact,epoch FROM autonomy_meme_observations WHERE role_id=? AND run_id=? AND path=?',
                    (role_id, context.run_id, relative)).fetchone()
            expected = json.loads(observed[0]) if observed else None
            if path.exists() and (expected is None or observed[1] != grant.managedEpoch):
                raise ValueError('needs_read: read this library file in the current run before editing')
            if expected is None:
                expected = owner._fact(role_id, path)
            if turn is not None:
                desired = json.loads(data, object_pairs_hook=_unique_object)
                if not isinstance(desired, dict) or desired.get('version') != 1 or not isinstance(desired.get('categories'), dict):
                    raise ValueError('meme manifest must contain version 1 and categories')
                previous = json.loads(_read(path, 4 * 1024 * 1024), object_pairs_hook=_unique_object) if path.exists() else {'categories': {}}
                for category in set(previous['categories']) | set(desired['categories']):
                    if category not in grant.allowedCategories and previous['categories'].get(category) != desired['categories'].get(category):
                        raise PermissionError('meme manifest cannot change an ungranted category')
                for category, entry in desired['categories'].items():
                    if (not isinstance(entry, dict) or set(entry) != {'desc', 'aliases', 'enabled'} or not isinstance(entry['desc'], str)
                            or len(entry['desc']) > 2000 or not isinstance(entry['aliases'], list) or len(entry['aliases']) > 32
                            or any(not isinstance(alias, str) or len(alias) > 128 for alias in entry['aliases']) or type(entry['enabled']) is not bool):
                        raise ValueError('meme category requires finite description, aliases and enabled flag')
            operation = uuid.uuid4().hex
            private = owner.root(role_id).parent / '.meido-memes/staging'
            temporary = private / (operation + '.tmp')
            digest = hashlib.sha256(data).hexdigest()
            planned = _json({'kind': 'write', 'content': data.decode('utf-8'),
                'provenance': owner.provenance(role_id, context, grant, turn)})
            new_directories = sum(not parent.exists() for parent in (private.parent, private))
            peak = len(data) + len(planned.encode('utf-8')) + len(_json(expected).encode('utf-8'))
            with owner.storage.claim(role_id, operation, peak, 1 + new_directories, context):
                private.mkdir(parents=True, exist_ok=True)
                with owner.app._transaction() as db:
                    db.execute('INSERT INTO autonomy_meme_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (operation, role_id, context.run_id, context.tool_call_id, grant.managedEpoch, str(path), _json(expected), digest,
                         planned, str(temporary), 'reserved', '{}'))
                    from . import retention
                    retention.check_capacity(db, role_id)
                write = _MemeWrite(owner, turn, grant, role_id, path, temporary, context, expected, operation, digest)
                yield write
        setattr(policy, 'short_publication', admitted.enabled)
        return policy


class _MemeWrite:
    def __init__(self, owner, turn, grant, role_id, path, temporary, context, expected, operation, digest):
        self.owner, self.turn, self.grant, self.role_id = owner, turn, grant, role_id
        self.path, self.temporary, self.context, self.expected = path, temporary, context, expected
        self.operation, self.digest, self.temp_identity = operation, digest, None
        self.prepared_identity = None
        self.parents: list[tuple[str, list[int]]] = []

    def opened(self, stream):
        self.temp_identity = _identity(self.temporary)[:2]
        self.parents = [(str(path), list(_directory_identity(path))) for path in (self.temporary.parent.parent, self.temporary.parent)]
        with self.owner.app._transaction() as db:
            db.execute('UPDATE autonomy_meme_operations SET actual=? WHERE operation_id=?',
                (_json({'physicalPhase': 'reserved', 'staging': {str(self.temporary): list(self.temp_identity)}, 'privateParents': self.parents}), self.operation))

    def check(self):
        if self.context.signal.is_cancelled() or self.owner._grant(self.role_id) != self.grant:
            raise PermissionError('meme publication generation was sealed')
        if self.turn is not None:
            self.owner.authorize(self.turn, self.context, 'write_file', 'manifest.json')
        matches = (self.owner._fact(self.role_id, self.path) == self.expected if self.prepared_identity is None
            else self.owner._matches(self.role_id, self.path, self.expected))
        if not matches:
            with self.owner.app._transaction() as db:
                db.execute("UPDATE autonomy_meme_operations SET status='conflict' WHERE operation_id=?", (self.operation,))
            raise PermissionError('conflict: meme file changed after its original observation')
        if self.prepared_identity is not None and _identity(self.temporary) != self.prepared_identity:
            raise PermissionError('meme prepared staging identity changed')

    def prepared(self):
        # Long content verification belongs to the original owned worker and
        # precedes the publication lock. The final CAS below checks identity.
        if self.owner._fact(self.role_id, self.path) != self.expected:
            raise PermissionError('conflict: meme file changed during private preparation')
        identity = _identity(self.temporary)
        if identity[:2] != self.temp_identity or hashlib.sha256(_read(self.temporary, 4 * 1024 * 1024)).hexdigest() != self.digest:
            raise PermissionError('meme staging content changed')
        self.prepared_identity = identity
        self.check()
        with self.owner.app._transaction() as db:
            db.execute("UPDATE autonomy_meme_operations SET status='files_ready',actual=json_set(actual,'$.physicalPhase','files_ready') WHERE operation_id=?", (self.operation,))

    @contextmanager
    def publication(self):
        with _file_lock(self.path, self.context, wait_seconds=.25), self.owner.app._gate:
            self.check()
            yield

    def committed(self):
        if self.prepared_identity is None:
            raise PermissionError('meme publication has no confirmed prepared staging identity')
        fact = self.owner._fast_fact(self.role_id, self.path)
        if fact.get('file') != list(self.prepared_identity):
            raise PermissionError('meme published bytes are unconfirmed')
        fact['sha256'] = self.digest
        with self.owner.app._transaction() as db:
            self.owner.advance_revision(db, self.role_id, self.grant, self.expected['libraryRevision'])
            fact['publishedLibraryRevision'] = self.expected['libraryRevision'] + 1
            phase = 'manifest_published' if self.path.name == 'manifest.json' else 'files_published'
            original = json.loads(db.execute('SELECT actual FROM autonomy_meme_operations WHERE operation_id=?', (self.operation,)).fetchone()[0])
            db.execute('UPDATE autonomy_meme_operations SET status=?,actual=? WHERE operation_id=?',
                (phase, _json({**original, 'physicalPhase': phase, 'file': fact}), self.operation))
        with self.owner.app._transaction() as db:
            db.execute("UPDATE autonomy_meme_operations SET status='recorded' WHERE operation_id=?", (self.operation,))

    def cleanup(self):
        if self.temporary.exists():
            if any(list(_directory_identity(Path(path))) != identity for path, identity in self.parents):
                raise PermissionError('meme staging parent identity is unconfirmed')
            if self.temp_identity is None or _identity(self.temporary)[:2] != self.temp_identity:
                raise PermissionError('meme temporary identity is unconfirmed')
            self.temporary.unlink()

    def receipt(self):
        return {'sha256': self.digest, 'operationId': self.operation, 'stage': 'recorded'}


class _OwnerLibraryTool:
    """Ordinary file authority is preserved; adopted libraries add shared CAS."""
    def __init__(self, owner, tool, role_id, authorized):
        self.owner, self.tool, self.role_id, self.authorized = owner, tool, role_id, authorized
        self.definition = tool.definition

    def with_write_policy(self, policy):
        return _OwnerLibraryTool(self.owner, self.tool.with_write_policy(policy), self.role_id, self.authorized)

    async def execute(self, arguments, context, on_update=None):
        raw = arguments.get('path')
        root = self.owner.root(self.role_id)
        target = Path(os.path.abspath(self.tool.workspace / raw)) if isinstance(raw, str) else root.parent
        if not target.is_relative_to(root):
            return await self.tool.execute(arguments, context, on_update)
        grant = self.owner._grant(self.role_id)
        if not grant.enabled:
            return await self.tool.execute(arguments, context, on_update)
        if not self.authorized(context):
            return AgentToolResult('Managed library access denied: original owner turn is unavailable.', {'status': 'denied'}, True)
        name = self.definition.name
        before = await self.owner.app._owned(self.owner._fact, self.role_id, target)
        tool = self.tool.with_write_policy(self.owner.policy(self.role_id, admission_grant=grant))
        result = await tool.execute(arguments, context, on_update)
        if name in {'read_file', 'list_dir'} and not result.is_error:
            after = await self.owner.app._owned(self.owner._fact, self.role_id, target)
            if before != after or self.owner._grant(self.role_id) != grant:
                return AgentToolResult('Library changed during read; read again before editing.', {'status': 'conflict'}, True)
            with self.owner.app._transaction() as db:
                db.execute('INSERT OR REPLACE INTO autonomy_meme_observations VALUES(?,?,?,?,?)',
                    (self.role_id, context.run_id, target.relative_to(root).as_posix(), _json(after), grant.managedEpoch))
        return result
