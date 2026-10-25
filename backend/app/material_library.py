"""Owner-facing view of the existing role meme library, not a model capability.

All IDs are host-derived observations, never filesystem addresses. Imports use
ordinary owner publication/recovery and the existing managed quota/revision seam.
Removal moves only the library original to private recovery storage; attachment
copies already referenced by chat history remain untouched.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace
from urllib.parse import quote
import uuid

from PIL import Image

from .agent_runtime.file_tools import _FileFailure, _file_lock
from .agent_runtime.provider import CancellationToken
from .channels.meme_library import (
    MAX_IMAGE_BYTES, MAX_SCAN, _OwnerPublication, _category, _hash, _image, _json,
)
from .proactive_runtime.drift_workspace import _identity
from .proactive_runtime.source_authoring import _directory_identity
from .proactive_runtime.source_validation import _read


class MaterialConflict(PermissionError):
    pass


class MaterialGone(LookupError):
    pass


class InvalidMaterial(ValueError):
    pass


class MaterialParameters(ValueError):
    pass


@dataclass(frozen=True)
class _Entry:
    path: Path
    identity: tuple
    digest: str
    public: dict


@dataclass(frozen=True)
class _Snapshot:
    entries: tuple[_Entry, ...]
    categories: tuple[dict, ...]
    revision: str
    stamp: tuple
    manifest: dict
    grant: object
    fact: dict


class OwnerMaterialLibrary:
    def __init__(self, chat_library, role_getter):
        self.chat = chat_library
        self.library = chat_library.library
        self.role_getter = role_getter
        # Shared with short-lived router adapters, bounded and strictly advisory:
        # every image lookup rechecks role, root, path, original identity and hash.
        with self.library.app._gate:
            if not hasattr(self.library, '_owner_material_observations'):
                self.library._owner_material_observations = OrderedDict()
        self._observations = self.library._owner_material_observations
        with self.library._recovery_gate_lock:
            if not hasattr(self.library, '_owner_material_runs'):
                self.library._owner_material_runs = set()

    @contextmanager
    def _owned(self, context):
        # Register BEFORE any durable row can become visible to recovery. The
        # unique host ID is never reused or adopted after process restart.
        key = (context.role_id, context.run_id)
        with self.library._recovery_gate_lock:
            self.library._owner_material_runs.add(key)
        try:
            yield
        finally:
            with self.library._recovery_gate_lock:
                self.library._owner_material_runs.discard(key)

    def _check_role(self, role_id):
        if self.role_getter(role_id) is None:
            raise KeyError(role_id)

    def _root(self, role_id):
        self._check_role(role_id)
        root = self.library.root(role_id)
        _directory_identity(root.parent)
        root.mkdir(exist_ok=True)
        _directory_identity(root)
        return root

    def _context(self, role_id):
        return SimpleNamespace(role_id=role_id, session_key='owner:material-library',
            run_id=uuid.uuid4().hex, tool_call_id='owner-material-library', signal=CancellationToken())

    def _stamp(self, role_id):
        """Cheap bounded identity projection; no image decoding inside path locks."""
        root = self._root(role_id)
        records = [('root', self.chat._root_identity(role_id))]
        pending = [(root, 'memes', 0)]
        private = root.parent / '.meido-memes'
        for name in ('chat', 'metadata'):
            path = private / name
            if path.exists() or path.is_symlink():
                _directory_identity(path)
                pending.append((path, name, 1))
        count = 0
        while pending:
            directory, label, depth = pending.pop()
            _directory_identity(directory)
            records.append((label, _directory_identity(directory)))
            with os.scandir(directory) as children:
                for child in children:
                    count += 1
                    if count > MAX_SCAN * 3:
                        raise ValueError('material library exceeds bounded identity scan')
                    path = Path(child.path)
                    info = path.lstat()  # Never follow linked entries, even inside this role.
                    name = label + '/' + child.name
                    records.append((name, info.st_mode, info.st_dev, info.st_ino,
                                    info.st_size, info.st_mtime_ns, info.st_nlink))
                    if stat.S_ISDIR(info.st_mode) and depth == 0:
                        pending.append((path, name, depth + 1))
        return tuple(sorted(records, key=lambda item: item[0]))

    def _metadata(self, role_id, path, digest):
        private = self.library.root(role_id).parent / '.meido-memes'
        for kind, metadata in [('chat', private / 'chat' / (digest + '.json')),
                               ('drift', private / 'metadata' / (path.stem + '.json'))]:
            if not metadata.exists():
                continue
            try:
                value = json.loads(_read(metadata, 16 * 1024))
                if not isinstance(value, dict) or value.get('roleId') != role_id or value.get('sha256') != digest:
                    continue
                if kind == 'drift' and value.get('path') != path.relative_to(self.library.root(role_id)).as_posix():
                    continue
                return ('owner_import' if kind == 'chat' and value.get('source') == 'owner_import' else kind), value
            except (OSError, ValueError, PermissionError):
                continue
        return 'legacy', {}

    def _snapshot(self, role_id):
        root = self._root(role_id)
        before = self._stamp(role_id)
        grant = self.library._grant(role_id)
        fact = self.library._fact(role_id, root / 'manifest.json')
        if grant.enabled:
            with self.library.app._transaction() as db:
                adopted = db.execute('SELECT epoch,identity FROM autonomy_meme_roots WHERE role_id=?', (role_id,)).fetchone()
            if (adopted is None or adopted[0] != grant.managedEpoch
                    or json.loads(adopted[1]) != fact['root']):
                raise PermissionError('managed original library root changed')
        document, _ = self.chat._manifest(role_id)
        entries = []
        for category, category_data, path, raw, media in self.chat._items(role_id, enabled_only=False):
            identity = _identity(path)
            digest = _hash(raw)
            source, metadata = self._metadata(role_id, path, digest)
            description = metadata.get('description') or category_data.get('desc', '')
            if not isinstance(description, str):
                description = ''
            source_aliases = category_data.get('aliases', [])
            aliases = [value[:80] for value in source_aliases if isinstance(value, str)][:32] if isinstance(source_aliases, list) else []
            identifier = _hash(_json([role_id, str(path), identity, digest]))[:32]
            with Image.open(io.BytesIO(raw)) as image:
                width, height, format = image.width, image.height, image.format
                animated = getattr(image, 'n_frames', 1) > 1
            name = metadata.get('name')
            if not isinstance(name, str):
                name = path.name
            name = name.replace('\\', '/').rsplit('/', 1)[-1][:160]
            public = {'id': identifier, 'name': name, 'category': category,
                'description': description[:2000], 'aliases': aliases, 'source': source,
                'enabled': category_data.get('enabled') is True, 'format': format,
                'mediaType': media, 'size': len(raw), 'width': width, 'height': height,
                'animated': animated,
                'imageUrl': f'/api/roles/{quote(role_id, safe="")}/materials/{identifier}/image'}
            entries.append(_Entry(path, identity, digest, public))
        entries.sort(key=lambda item: (item.public['category'], item.public['name'], item.public['id']))
        categories = []
        for name, entry in document['categories'].items():
            _category(name)
            if not isinstance(entry, dict):
                continue
            desc = entry.get('desc', '')
            categories.append({'name': name, 'description': desc[:2000] if isinstance(desc, str) else '',
                'enabled': entry.get('enabled') is True,
                'count': sum(item.public['category'] == name for item in entries)})
        categories.sort(key=lambda item: item['name'])
        if before != self._stamp(role_id) or not self.library._matches(role_id, root / 'manifest.json', fact):
            raise MaterialConflict('library changed during observation')
        revision = _hash(_json([role_id, before, fact, [entry.public for entry in entries]]))
        root_identity = self.chat._root_identity(role_id)
        with self.library.app._gate:
            for entry in entries:
                key = (role_id, entry.public['id'])
                self._observations[key] = (entry, root_identity)
                self._observations.move_to_end(key)
            while len(self._observations) > 2048:
                self._observations.popitem(last=False)
        return _Snapshot(tuple(entries), tuple(categories), revision, before, document, grant, fact)

    def list(self, role_id, query='', category=''):
        if len(query) > 2000 or len(category) > 80:
            raise MaterialParameters('invalid finite query')
        snapshot = self._snapshot(role_id)
        tokens = query.casefold().split()
        entries = [item.public for item in snapshot.entries if (not category or item.public['category'] == category)
            and all(token in ' '.join((item.public['name'], item.public['category'], item.public['description'],
                *item.public['aliases'])).casefold() for token in tokens)]
        return {'materials': entries, 'categories': list(snapshot.categories), 'revision': snapshot.revision,
                'storageLabel': '当前角色工作区 / workspace/memes'}

    def _entry(self, snapshot, identifier):
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise MaterialGone(identifier)
        item = next((item for item in snapshot.entries if item.public['id'] == identifier), None)
        if item is None:
            raise MaterialGone(identifier)
        return item

    def _current(self, role_id, snapshot):
        self._check_role(role_id)
        if (self.library._grant(role_id) != snapshot.grant or self._stamp(role_id) != snapshot.stamp
                or not self.library._matches(role_id, self.library.root(role_id) / 'manifest.json', snapshot.fact)):
            raise MaterialConflict('material library changed; refresh before changing it')

    def _original(self, role_id, entry):
        self.chat._path(role_id, entry.path)
        if _identity(entry.path) != entry.identity:
            raise MaterialGone(entry.public['id'])
        raw = _read(entry.path, MAX_IMAGE_BYTES)
        if _identity(entry.path) != entry.identity or _hash(raw) != entry.digest:
            raise MaterialGone(entry.public['id'])
        return raw

    def _locks(self, role_id, paths, context):
        stack = ExitStack()
        try:
            for path in sorted(set(paths), key=lambda value: os.path.normcase(str(value))):
                stack.enter_context(_file_lock(path, context, wait_seconds=.25))
            return stack
        except BaseException:
            stack.close()
            raise

    def image(self, role_id, identifier):
        root = self._root(role_id)
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise MaterialGone(identifier)
        with self.library.app._gate:
            observed = self._observations.get((role_id, identifier))
        if observed is None:
            raise MaterialGone(identifier)
        entry, root_identity = observed
        with self._locks(role_id, (entry.path, root / 'manifest.json'), self._context(role_id)):
            self._check_role(role_id)
            if self.chat._root_identity(role_id) != root_identity:
                raise MaterialGone(identifier)
            document, _ = self.chat._manifest(role_id)
            if (entry.path.parent != root / entry.public['category']
                    or entry.public['category'] not in document['categories']):
                raise MaterialGone(identifier)
            try:
                raw = self._original(role_id, entry)
            except (OSError, ValueError, PermissionError, _FileFailure):
                raise MaterialGone(identifier) from None
            # Shared FileTools path errors are deliberately not public paths.
        return raw, entry.public

    def import_image(self, role_id, raw, filename, media_type, category, description, expected_revision):
        context = self._context(role_id)
        with self._owned(context):
            return self._import_image(role_id, raw, filename, media_type, category, description, expected_revision, context)

    def _import_image(self, role_id, raw, filename, media_type, category, description, expected_revision, context):
        root = self._root(role_id)
        try:
            category = _category(category)
        except ValueError:
            raise MaterialParameters('invalid category') from None
        if not isinstance(description, str) or not description.strip() or len(description) > 2000:
            raise MaterialParameters('description must be nonempty and at most 2000 characters')
        try:
            extension, media = _image(raw)
        except (OSError, ValueError, Image.DecompressionBombError):
            raise InvalidMaterial('invalid image') from None
        if media_type.split(';', 1)[0].strip().lower() not in {'', 'application/octet-stream', media}:
            raise InvalidMaterial('claimed media type does not match image')
        snapshot = self._snapshot(role_id)
        if snapshot.revision != expected_revision:
            raise MaterialConflict('stale observation')
        digest = _hash(raw)
        existing = next((item for item in snapshot.entries if item.digest == digest), None)
        manifest = root / 'manifest.json'
        if existing:
            with self._locks(role_id, (manifest, existing.path), context):
                self._current(role_id, snapshot)
                self._original(role_id, existing)
            return {'material': existing.public, 'deduplicated': True, 'revision': snapshot.revision}
        target = self.chat._path(role_id, root / category / (digest + '.' + extension))
        reused_identity = None
        if target.exists():
            # A prior interrupted owner import may have published the original
            # before its new category manifest. A fresh explicit upload may
            # adopt only those exact bytes, never overwrite or replay intent.
            reused_identity = _identity(target)
            if (_hash(_read(target, MAX_IMAGE_BYTES)) != digest
                    or _identity(target) != reused_identity):
                raise MaterialConflict('unindexed destination contains different bytes')
        metadata = root.parent / '.meido-memes' / 'chat' / (digest + '.json')
        desired = json.loads(json.dumps(snapshot.manifest))
        if category not in desired['categories']:
            desired['categories'][category] = {'desc': description, 'aliases': [], 'enabled': True}
        name = filename.replace('\\', '/').rsplit('/', 1)[-1].strip()[:160] or ('image.' + extension)
        files = {} if reused_identity is not None else {target: raw}
        if not metadata.exists():
            files[metadata] = _json({'roleId': role_id, 'sha256': digest, 'description': description,
                'category': category, 'source': 'owner_import', 'name': name})
        if desired != snapshot.manifest or not manifest.exists():
            files[manifest] = _json(desired)
        publication = _OwnerPublication(self.library, role_id, context, files, kind='owner_chat_copy')
        claim = (self.library.storage.claim(role_id, publication.operation,
            sum(len(data) for data in files.values()) + 32 * 1024, len(files) + 5, context)
            if snapshot.grant.enabled else nullcontext())
        try:
            with claim:
                publication.prepare(lambda: self._check_role(role_id))
                with self._locks(role_id, (manifest, target, metadata), context):
                    with self.library.app._gate:
                        self._current(role_id, snapshot)
                        if reused_identity is None:
                            if target.exists():
                                raise MaterialConflict('destination changed')
                        elif (_identity(target) != reused_identity
                                or _hash(_read(target, MAX_IMAGE_BYTES)) != digest):
                            raise MaterialConflict('unindexed original changed before explicit reuse')
                        target.parent.mkdir(exist_ok=True)
                        self.chat._path(role_id, target)
                        metadata.parent.mkdir(exist_ok=True)
                        _directory_identity(metadata.parent)
                        for path in files:
                            self._check_role(role_id)
                            publication.publish(path)
                        with self.library.app._transaction() as db:
                            if snapshot.grant.enabled:
                                self.library.advance_revision(db, role_id, snapshot.grant, snapshot.fact['libraryRevision'])
                            publication.finish(db)
        finally:
            publication.close()
        latest = self._snapshot(role_id)
        material = next((item.public for item in latest.entries if item.path == target and item.digest == digest), None)
        if material is None:
            raise MaterialConflict('published material changed before confirmation')
        return {'material': material, 'deduplicated': reused_identity is not None, 'revision': latest.revision}

    def remove(self, role_id, identifier, expected_revision):
        context = self._context(role_id)
        with self._owned(context):
            return self._remove(role_id, identifier, expected_revision, context)

    def _remove(self, role_id, identifier, expected_revision, context):
        snapshot = self._snapshot(role_id)
        if snapshot.revision != expected_revision:
            raise MaterialConflict('stale observation')
        entry = self._entry(snapshot, identifier)
        root = self.library.root(role_id)
        operation = uuid.uuid4().hex
        private = root.parent / '.meido-memes'
        trash = private / 'trash' / operation
        target = trash / ('original.' + entry.path.suffix.lstrip('.').lower())
        receipt = trash / 'receipt.json'
        planned = {'kind': 'owner_ui_remove', 'principal': 'owner', 'source': str(entry.path),
            'target': str(target), 'identity': list(entry.identity), 'sha256': entry.digest,
            'trashIdentity': None, 'receipt': str(receipt)}
        actual = {'physicalPhase': 'reserved'}
        receipt_bytes = _json({'version': 1, 'roleId': role_id,
            'originalRelativePath': entry.path.relative_to(root).as_posix(),
            'sha256': entry.digest, 'material': entry.public})
        with self.library.app._transaction() as db:
            db.execute('INSERT INTO autonomy_meme_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (operation, role_id, context.run_id, context.tool_call_id,
                 getattr(snapshot.grant, 'managedEpoch', ''), str(entry.path), _json(snapshot.fact).decode(), '',
                 _json(planned).decode(), '', 'reserved', _json(actual).decode()))
            from .proactive_runtime import retention
            retention.check_capacity(db, role_id)
        claim = (self.library.storage.claim(role_id, operation, len(receipt_bytes) + 16 * 1024, 5, context)
                 if snapshot.grant.enabled else nullcontext())
        try:
            with claim:
                for path in (private, private / 'trash', trash):
                    path.mkdir(exist_ok=True)
                    _directory_identity(path)
                planned['trashIdentity'] = list(_directory_identity(trash))
                with self.library.app._transaction() as db:
                    db.execute('UPDATE autonomy_meme_operations SET planned=? WHERE operation_id=?',
                        (_json(planned).decode(), operation))
                # Persist recovery instructions before moving original bytes.
                with receipt.open('xb') as output:
                    output.write(receipt_bytes)
                    output.flush()
                    os.fsync(output.fileno())
                with self._locks(role_id, (root / 'manifest.json', entry.path, target), context):
                    with self.library.app._gate:
                        self._current(role_id, snapshot)
                        self._original(role_id, entry)
                        if _directory_identity(trash) != tuple(planned['trashIdentity']) or target.exists():
                            raise MaterialConflict('private recovery destination changed')
                        os.replace(entry.path, target)
                        actual['physicalPhase'] = 'files_published'
                        with self.library.app._transaction() as db:
                            if snapshot.grant.enabled:
                                self.library.advance_revision(db, role_id, snapshot.grant, snapshot.fact['libraryRevision'])
                            db.execute('UPDATE autonomy_meme_operations SET status=?,actual=? WHERE operation_id=?',
                                ('recorded', _json(actual).decode(), operation))
        except BaseException:
            with self.library.app._transaction() as db:
                row = db.execute('SELECT * FROM autonomy_meme_operations WHERE operation_id=?', (operation,)).fetchone()
            reconcile_removal(self.library, row)
            raise
        self.library.storage.refresh(role_id)
        return {'removed': True, 'recoverable': True, 'revision': self._snapshot(role_id).revision}


def reconcile_removal(library, row):
    """Observe a single original move; never retry deletion or restore a file."""
    planned, expected, actual = (json.loads(row[key]) for key in ('planned', 'expected', 'actual'))
    root = library.root(row['role_id'])
    target, source = Path(planned['target']), Path(planned['source'])
    status = 'conflict'
    try:
        if (library._root_identity(row['role_id']) != expected['root']
                or not source.is_relative_to(root) or len(source.relative_to(root).parts) != 2
                or target.parent != root.parent / '.meido-memes' / 'trash' / row['operation_id']
                or planned['trashIdentity'] is not None and list(_directory_identity(target.parent)) != planned['trashIdentity']):
            raise PermissionError('original removal scope changed')
        if target.exists():
            if list(_identity(target)) != planned['identity'] or _hash(_read(target, MAX_IMAGE_BYTES)) != planned['sha256']:
                raise PermissionError('recovery copy identity changed')
            with library.app._transaction() as db:
                db.execute('UPDATE autonomy_meme_roots SET revision=revision+1 WHERE role_id=? AND epoch=? AND revision=?',
                    (row['role_id'], row['epoch'], expected['libraryRevision']))
            actual.update(physicalPhase='files_published', outcome='retained_in_trash')
        elif source.exists() and list(_identity(source)) == planned['identity']:
            actual['outcome'] = 'not_removed'
        else:
            raise PermissionError('original removal fact unavailable')
        status = 'reconciled'
    except (OSError, ValueError, PermissionError, KeyError):
        actual['outcome'] = 'unconfirmed'
    with library.app._transaction() as db:
        db.execute('UPDATE autonomy_meme_operations SET status=?,actual=? WHERE operation_id=?',
            (status, _json(actual).decode(), row['operation_id']))
    library.storage.reconciled(row['role_id'], row['operation_id'], status)
