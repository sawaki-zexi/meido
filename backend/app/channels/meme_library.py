"""Ordinary owner chat access to the existing role meme library.

This adapter supplies no authority of its own. Its caller selects a registered
current-input attachment and rechecks the original owner turn. Publication shares
FileTools' locks and the adopted MemeLibrary's allocation/revision protocol.
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid

from PIL import Image

from ..agent_runtime.file_tools import build_file_tools, _file_lock
from ..chat_attachments import resolve_attachment
from ..proactive_runtime.drift_workspace import _identity
from ..proactive_runtime.source_authoring import _directory_identity
from ..proactive_runtime.source_validation import _read, _unique_object

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_ANIMATION_FRAMES = 200
MAX_ANIMATION_PIXELS = 80_000_000
MAX_SCAN = 512
FORMATS = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'),
           'WEBP': ('webp', 'image/webp'), 'GIF': ('gif', 'image/gif')}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _image(raw):
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('meme image must be at most 10 MiB')
    with Image.open(io.BytesIO(raw)) as image:
        format = image.format
        if format not in FORMATS or image.width * image.height > MAX_PIXELS:
            raise ValueError('unsupported meme format or image dimensions')
        pixels = 0
        for frame in range(MAX_ANIMATION_FRAMES + 1):
            try:
                image.seek(frame)
            except EOFError:
                break
            if frame == MAX_ANIMATION_FRAMES:
                raise ValueError('meme animation exceeds 200 frames')
            pixels += image.width * image.height
            if pixels > MAX_ANIMATION_PIXELS:
                raise ValueError('meme animation exceeds decoded pixel budget')
            image.load()
    return FORMATS[format]


def _category(value):
    if (not isinstance(value, str) or not value or len(value) > 80
            or value in {'.', '..'} or value.startswith('.')
            or any(char in value for char in '/\\:\0') or value.rstrip(' .') != value):
        raise ValueError('category must be a single ordinary directory name')
    return value


@dataclass(frozen=True)
class MemeCandidate:
    id: str
    role_id: str
    category: str
    description: str
    aliases: tuple[str, ...]
    path: Path
    identity: tuple
    root_identity: tuple
    sha256: str
    media_type: str

    def public(self):
        return {'id': self.id, 'category': self.category, 'description': self.description,
                'aliases': list(self.aliases)}


class ChatMemeLibrary:
    def __init__(self, library, attachment_root):
        self.library = library
        self._attachment_root = attachment_root

    def attachment_root(self, role_id):
        value = self._attachment_root(role_id) if callable(self._attachment_root) else self._attachment_root
        return Path(value)

    def _path(self, role_id, path):
        return build_file_tools(role_id, self.library.root(role_id))[0]._path(str(path))

    def _manifest(self, role_id):
        path = self._path(role_id, self.library.root(role_id) / 'manifest.json')
        if not path.exists():
            return {'version': 1, 'categories': {}}, None
        raw = _read(path, 4 * 1024 * 1024)
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('categories'), dict):
            raise ValueError('invalid existing meme manifest')
        return value, raw

    def _root_identity(self, role_id):
        root = self.library.root(role_id)
        return tuple((str(path), _directory_identity(path)) for path in (root.parent.parent, root.parent, root))

    def _items(self, role_id, *, enabled_only=True):
        root = self.library.root(role_id)
        if not root.exists():
            return []
        document, _ = self._manifest(role_id)
        count = 0
        for category, entry in document['categories'].items():
            _category(category)
            if (not isinstance(entry, dict) or (enabled_only and entry.get('enabled') is not True)):
                continue
            directory = self._path(role_id, root / category)
            if not directory.is_dir():
                continue
            _directory_identity(directory)
            with os.scandir(directory) as entries:
                for item in entries:
                    count += 1
                    if count > MAX_SCAN:
                        raise ValueError('meme library exceeds bounded candidate scan')
                    try:
                        path = self._path(role_id, Path(item.path))
                        if path.suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.gif'} or not path.is_file():
                            continue
                        raw = _read(path, MAX_IMAGE_BYTES)
                        _, media = _image(raw)
                        yield category, entry, path, raw, media
                    except Exception:
                        # One invalid entry must not make other genuine images invisible.
                        continue

    def candidates(self, role_id, query='', context=None, *, limit=12):
        del context
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError('invalid finite meme query')
        result = []
        for category, entry, path, raw, media in self._items(role_id):
            digest = _hash(raw)
            desc = entry.get('desc', '')
            aliases = entry.get('aliases', [])
            aliases = tuple(value for value in aliases if isinstance(value, str)) if isinstance(aliases, list) else ()
            private = self.library.root(role_id).parent / '.meido-memes' / 'chat' / (digest + '.json')
            if private.exists():
                try:
                    metadata = json.loads(_read(private, 16 * 1024))
                    if isinstance(metadata, dict) and metadata.get('roleId') == role_id and metadata.get('sha256') == digest:
                        desc = metadata.get('description') or desc
                except (OSError, ValueError, PermissionError):
                    pass
            if not isinstance(desc, str):
                desc = ''
            identity = _identity(path)
            identifier = _hash(_json([role_id, str(path), identity, digest]))[:32]
            candidate = MemeCandidate(identifier, role_id, category, desc[:2000], aliases[:32],
                path, identity, self._root_identity(role_id), digest, media)
            searchable = ' '.join((category, desc, *aliases)).casefold()
            score = sum(token in searchable for token in query.casefold().split())
            if not query.strip() or score:
                result.append((score, path.name, candidate))
        result.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in result[:max(0, min(limit, MAX_SCAN))])

    def valid(self, role_id, candidate):
        try:
            if not isinstance(candidate, MemeCandidate) or candidate.role_id != role_id:
                return False
            root = self.library.root(role_id)
            if candidate.path.parent != root / candidate.category:
                return False
            self._path(role_id, candidate.path)
            document, _ = self._manifest(role_id)
            entry = document['categories'].get(candidate.category)
            return bool(isinstance(entry, dict) and entry.get('enabled') is True
                and self._root_identity(role_id) == candidate.root_identity
                and _identity(candidate.path) == candidate.identity
                and _hash(_read(candidate.path, MAX_IMAGE_BYTES)) == candidate.sha256)
        except Exception:
            return False

    def _source(self, role_id, attachment):
        identifier = str(attachment.get('id', ''))
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise PermissionError('invalid source attachment identity')
        directory = self.attachment_root(role_id) / 'attachments'
        metadata = json.loads(_read(directory / (identifier + '.json'), 16 * 1024))
        source_path = build_file_tools(role_id, directory)[0]._path(str(metadata.get('path', '')))
        item = resolve_attachment(self.attachment_root(role_id), identifier)
        if item is not None and str(source_path) != item.get('path'):
            raise PermissionError('attachment path identity changed')
        if item is None or any(item.get(key) != attachment.get(key) for key in ('id','path','mediaType','size','kind')):
            raise PermissionError('meme source is not the original registered role attachment')
        if item.get('kind') != 'image':
            raise ValueError('meme source must be an image')
        raw = _read(Path(item['path']), MAX_IMAGE_BYTES)
        identity = _identity(Path(item['path']))
        if ('_memeIdentity' in attachment and (tuple(attachment['_memeIdentity']) != identity
                or attachment.get('_memeSha256') != _hash(raw))):
            raise PermissionError('attachment changed after the original input was frozen')
        extension, media = _image(raw)
        if item.get('mediaType') != media or item.get('size') != len(raw):
            raise ValueError('meme attachment metadata does not match the actual original')
        return raw, extension, identity

    def freeze_attachment(self, role_id, attachment):
        raw, _, identity = self._source(role_id, attachment)
        return {**attachment, '_memeIdentity': identity, '_memeSha256': _hash(raw)}

    def save(self, role_id, attachment, category, description, context, authorized):
        def check():
            if context.role_id != role_id or context.signal.is_cancelled() or not authorized():
                raise PermissionError('original owner meme source permission was revoked')
        check()
        category = _category(category)
        if not isinstance(description, str) or not description.strip() or len(description) > 2000:
            raise ValueError('meme description must be nonempty and at most 2000 characters')
        raw, extension, source_identity = self._source(role_id, attachment)
        digest = _hash(raw)
        root = self.library.root(role_id)
        root.parent.mkdir(parents=True, exist_ok=True)
        _directory_identity(root.parent)
        root.mkdir(exist_ok=True)
        root_identity = self._root_identity(role_id)
        manifest = self._path(role_id, root / 'manifest.json')
        document, original_manifest = self._manifest(role_id)
        manifest_identity = _identity(manifest) if manifest.exists() else None
        grant = self.library._grant(role_id)
        expected = self.library._fact(role_id, manifest) if grant.enabled else None
        # Dedupe spans categories, including disabled ones. Saving is not permission
        # to change a prior category's enabled flag or owner's explanation.
        existing = next((item for item in self._items(role_id, enabled_only=False) if _hash(item[3]) == digest), None)
        target = existing[2] if existing else self._path(role_id, root / category / (digest + '.' + extension))
        target_identity = _identity(target) if target.exists() else None
        actual_category = existing[0] if existing else category
        metadata = root.parent / '.meido-memes' / 'chat' / (digest + '.json')
        metadata_raw = _json({'roleId':role_id,'sha256':digest,'description':description,
            'sourceAttachmentId':attachment['id'],'sourceSessionKey':context.session_key,
            'category':actual_category})
        desired = json.loads(json.dumps(document))
        if not existing and category not in desired['categories']:
            desired['categories'][category] = {'desc':description,'aliases':[],'enabled':True}
        files = {}
        if not target.exists():
            files[target] = raw
        if not metadata.exists():
            files[metadata] = metadata_raw
        if desired != document or original_manifest is None:
            files[manifest] = _json(desired)
        if not files:
            check()
            self._source(role_id,attachment)
            if (_identity(Path(attachment['path'])) != source_identity
                    or (_identity(target) if target.exists() else None) != target_identity):
                raise PermissionError('original meme source or duplicate was replaced')
            return {'status':'recorded','deduplicated':True,'sha256':digest,'category':actual_category,'bytes':len(raw)}
        publication = _OwnerPublication(self.library, role_id, context, files, kind='owner_chat_copy')
        claim = (self.library.storage.claim(role_id, publication.operation,
            sum(len(data) for data in files.values()) + 32*1024, len(files)+5, context)
            if grant.enabled else nullcontext())
        try:
            with claim:
                publication.prepare(check)
                # Verify expensive source bytes before the short publication lock.
                now, _, identity = self._source(role_id, attachment)
                if identity != source_identity or _hash(now) != digest:
                    raise PermissionError('original attachment changed during preparation')
                with ExitStack() as stack:
                    for path in sorted((manifest,target,metadata), key=lambda item: os.path.normcase(str(item))):
                        stack.enter_context(_file_lock(path, context, wait_seconds=.25))
                    if grant.enabled:
                        stack.enter_context(self.library.app._gate)
                    check()
                    if self.library._grant(role_id) != grant or self._root_identity(role_id) != root_identity:
                        raise PermissionError('meme library permission or root changed')
                    current, current_raw = self._manifest(role_id)
                    if (current_raw != original_manifest
                            or (_identity(manifest) if manifest.exists() else None) != manifest_identity
                            or grant.enabled and not self.library._matches(role_id,manifest,expected)):
                        raise PermissionError('conflict: meme manifest changed during preparation')
                    if _identity(Path(attachment['path'])) != source_identity:
                        raise PermissionError('original attachment changed before publication')
                    if ((_identity(target) if target.exists() else None) != target_identity
                            or target.exists() and _hash(_read(target,MAX_IMAGE_BYTES)) != digest):
                        raise PermissionError('meme target was replaced')
                    if existing is None and any(_hash(item[3]) == digest
                            for item in self._items(role_id, enabled_only=False)):
                        raise PermissionError('conflict: identical meme was saved concurrently; retry to reuse it')
                    target.parent.mkdir(exist_ok=True)
                    self._path(role_id,target)
                    metadata.parent.mkdir(exist_ok=True)
                    _directory_identity(metadata.parent)
                    for path in files:
                        check()
                        if self.library._grant(role_id) != grant or self._root_identity(role_id) != root_identity:
                            raise PermissionError('meme publication permission or root changed')
                        publication.publish(path)
                    with self.library.app._transaction() as db:
                        if grant.enabled and files:
                            self.library.advance_revision(db,role_id,grant,expected['libraryRevision'])
                        publication.finish(db)
        finally:
            publication.close()
        return {'status':'recorded','deduplicated':existing is not None,'sha256':digest,
                'category':actual_category,'bytes':len(raw)}

    def reference(self, role_id, candidate, context, *, principal='owner'):
        # Host-only provenance, never a model argument or an authority grant.
        if principal not in {'owner', 'natural', 'reaction'}:
            raise ValueError('invalid meme reference principal')
        if context.role_id != role_id or context.signal.is_cancelled() or not self.valid(role_id,candidate):
            raise PermissionError('meme candidate is no longer valid for this role')
        raw = _read(candidate.path, MAX_IMAGE_BYTES)
        if _hash(raw) != candidate.sha256:
            raise PermissionError('meme bytes changed before reference preparation')
        extension, media = _image(raw)
        identifier = _hash(('chat-meme:' + role_id + ':' + candidate.sha256).encode())[:32]
        directory = self.attachment_root(role_id) / 'attachments'
        directory.mkdir(parents=True,exist_ok=True)
        directory_identity = _directory_identity(directory)
        path = directory / (identifier + '-meme.' + extension)
        metadata_path = directory / (identifier + '.json')
        value = {'id':identifier,'name':'meme.'+extension,'mediaType':media,'size':len(raw),
                 'kind':'image','path':str(path.absolute()),'text':''}
        def check():
            if context.signal.is_cancelled() or not self.valid(role_id,candidate):
                raise PermissionError('meme changed before sending reference publication')
            if _directory_identity(directory) != directory_identity:
                raise PermissionError('sending reference directory changed')
        files = {target:data for target,data in ((path,raw),(metadata_path,_json(value))) if not target.exists()}
        if not files:
            with _file_lock(metadata_path,context,wait_seconds=.25):
                check()
                if _read(path,MAX_IMAGE_BYTES)!=raw or json.loads(_read(metadata_path,16*1024))!=value:
                    raise PermissionError('existing meme sending reference was replaced')
                return value,raw
        publication = _OwnerPublication(self.library,role_id,context,files,
            kind={'owner': 'owner_chat_reference', 'natural': 'natural_chat_reference',
                  'reaction': 'role_reaction_reference'}[principal],
            principal=principal)
        grant=self.library._grant(role_id)
        claim=(self.library.storage.claim(role_id,publication.operation,
            sum(len(data) for data in files.values())+32*1024,len(files)+3,context)
            if grant.enabled else nullcontext())
        try:
            with claim:
                publication.prepare(check)
                with _file_lock(metadata_path,context,wait_seconds=.25):
                    check()
                    for destination,data in ((path,raw),(metadata_path,_json(value))):
                        if destination.exists():
                            existing = _read(destination, MAX_IMAGE_BYTES if destination == path else 16*1024)
                            matches = existing == data if destination == path else json.loads(existing) == value
                            if not matches:
                                raise PermissionError('existing meme sending reference was replaced')
                        else:
                            check()
                            publication.publish(destination)
                    with self.library.app._transaction() as db:
                        publication.finish(db)
        finally:
            publication.close()
        return value,raw


class _OwnerPublication:
    """Durable ownership of private stages, never authority to replay an edit."""
    def __init__(self, library, role_id, context, files, *, kind, principal='owner'):
        self.library, self.role_id, self.context = library, role_id, context
        self.operation, self.files = uuid.uuid4().hex, files
        self.private = library.root(role_id).parent / '.meido-memes'
        for directory in (self.private,self.private/'staging'):
            directory.mkdir(exist_ok=True)
            _directory_identity(directory)
        self.expected = library._fact(role_id,library.root(role_id)/'manifest.json')
        self.grant = library._grant(role_id)
        self.prepared = {}
        self.complete = False
        self.plan = {'kind':kind,'principal':principal,'files':[
            {'target':str(path),'stage':str(self.private/'staging'/(self.operation+'-'+str(index)+'.tmp')),
             'sha256':_hash(data),'expectedIdentity':list(_identity(path)) if path.exists() else None}
            for index,(path,data) in enumerate(files.items())]}
        self.actual = {'physicalPhase':'reserved','staging':{},'published':[],
            'privateParents':[[str(path),list(_directory_identity(path))]
                for path in (self.private,self.private/'staging')]}
        # Persist the recovery owner BEFORE claiming quota or creating a stage.
        # Provenance describes this host publication; it never confers a tool grant.
        with library.app._transaction() as db:
            db.execute('INSERT INTO autonomy_meme_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.operation,role_id,context.run_id,context.tool_call_id,
                 getattr(self.grant,'managedEpoch',''),str(library.root(role_id)/'manifest.json'),
                 _json(self.expected).decode(),'',_json(self.plan).decode(),'',
                 'reserved',_json(self.actual).decode()))
            from ..proactive_runtime import retention
            retention.check_capacity(db,role_id)

    def record(self,status):
        with self.library.app._transaction() as db:
            db.execute('UPDATE autonomy_meme_operations SET status=?,actual=? WHERE operation_id=?',
                (status,_json(self.actual).decode(),self.operation))

    def prepare(self,check):
        for item in self.plan['files']:
            check()
            stage=Path(item['stage'])
            with stage.open('xb') as output:
                info=os.fstat(output.fileno())
                self.actual['staging'][str(stage)]=[info.st_dev,info.st_ino]
                self.record('reserved')  # durable identity before the first byte
                data=self.files[Path(item['target'])]
                for offset in range(0,len(data),65536):
                    check()
                    output.write(data[offset:offset+65536])
                output.flush()
                os.fsync(output.fileno())
            identity=_identity(stage)
            if list(identity[:2]) != self.actual['staging'][str(stage)] or _hash(_read(stage,MAX_IMAGE_BYTES)) != item['sha256']:
                raise PermissionError('owner meme staging identity changed')
            self.prepared[str(stage)]=identity
        self.actual['physicalPhase']='files_ready'
        self.record('files_ready')

    def publish(self,path):
        item=next((item for item in self.plan['files'] if item['target']==str(path)),None)
        if item is None:
            raise PermissionError('reference disappeared after original observation')
        stage=Path(item['stage'])
        if (_identity(stage) != self.prepared[str(stage)]
                or (list(_identity(path)) if path.exists() else None) != item['expectedIdentity']):
            raise PermissionError('conflict: owner meme destination or stage changed')
        for name,identity in self.actual['privateParents']:
            if list(_directory_identity(Path(name))) != identity:
                raise PermissionError('owner meme staging directory changed')
        os.replace(stage,path)
        self.actual['published'].append(str(path))
        self.actual['physicalPhase']='files_published'
        self.record('files_published')

    def finish(self,db):
        # A competing reference creator may already have published equivalent
        # final bytes. Retire its now-unused owned stages BEFORE terminal status,
        # so an abrupt exit after this transaction cannot strand ignored files.
        from ..proactive_runtime.meme_recovery import _cleanup
        _cleanup(self.private,self.actual)
        self.actual.update(physicalPhase='completed',outcome='complete')
        db.execute('UPDATE autonomy_meme_operations SET status=?,actual=? WHERE operation_id=?',
            ('recorded',_json(self.actual).decode(),self.operation))
        self.complete=True

    def close(self):
        from ..proactive_runtime.meme_recovery import _cleanup, reconcile_owner
        if self.complete:
            _cleanup(self.private,self.actual)
            return
        with self.library.app._transaction() as db:
            row=db.execute('SELECT * FROM autonomy_meme_operations WHERE operation_id=?',(self.operation,)).fetchone()
        reconcile_owner(self.library,row)
