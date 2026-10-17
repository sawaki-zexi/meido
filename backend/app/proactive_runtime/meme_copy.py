"""One frozen upload becomes real files; recovery may only inspect these facts."""
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any
import uuid

from PIL import Image

from ..agent_runtime.file_tools import _file_lock, build_file_tools
from .drift_attachments import _load
from .drift_workspace import _identity
from .source_authoring import _directory_identity
from .source_validation import _read
from .meme_library import _json


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class _CopyFile:
    target: Path
    stage: Path
    content: bytes
    sha256: str


@dataclass(frozen=True)
class _CopyPlan:
    operation_id: str
    category: str
    source_id: str
    source_identity: tuple
    source_digest: str
    source_public_json: str
    expected_json: str
    planned_json: str
    image: _CopyFile
    metadata: _CopyFile
    manifest: _CopyFile


def execute(library, turn, arguments, context):
    identifier, category = arguments['attachment_id'], arguments['category']
    library.authorize(turn, context, 'meme_copy_image', category)
    grant = turn.config.driftMeme
    if (not grant.copyEnabled or identifier not in turn.meme_scope['sources']
            or not turn.app.tool_permitted(turn.origin, 'read_file') or not turn.app.tool_permitted(turn.origin, 'write_file')):
        raise PermissionError('meme copy lacks explicit source read, library write or purpose permission')
    expected_source = turn.meme_scope['sources'][identifier]
    actual_source, raw = _load(turn.app, turn.origin.role_id, identifier)
    if actual_source != expected_source:
        raise PermissionError('meme upload changed after this run froze it')
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError('meme copy source exceeds 40 million pixels')
        extension = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp', 'GIF': 'gif'}.get(image.format)
        image.verify()
    if extension is None or len(raw) > 10 * 1024 * 1024:
        raise ValueError('meme copy requires a supported image at most 10 MiB')
    role = turn.origin.role_id
    root = library.root(role)
    manifest = root / 'manifest.json'
    with turn.app._transaction() as db:
        observed = db.execute('SELECT fact,epoch FROM autonomy_meme_observations WHERE role_id=? AND run_id=? AND path=?',
            (role, context.run_id, 'manifest.json')).fetchone()
        count = db.execute("SELECT COUNT(*) FROM autonomy_meme_operations WHERE role_id=? AND run_id=? AND json_valid(planned) AND json_extract(planned,'$.kind')='copy'", (role, context.run_id)).fetchone()[0]
    if observed is None or observed[1] != grant.managedEpoch:
        raise ValueError('needs_read: inspect the actual manifest before copying')
    expected = json.loads(observed[0])
    if library._fact(role, manifest) != expected:
        raise PermissionError('conflict: the library changed after its original read')
    if count >= 4:
        raise ValueError('meme copy count exhausted for this original run')
    manifest_raw = _read(manifest, 4 * 1024 * 1024)
    document = json.loads(manifest_raw)
    if category not in document.get('categories', {}):
        raise PermissionError('meme copy requires an actual category in the observed manifest')
    operation = uuid.uuid4().hex
    target = root / category / (operation + '.' + extension)
    file_guard = build_file_tools(role, root)[0]
    file_guard._path(str(target))
    if target.exists():
        raise PermissionError('meme copy host target already exists')
    private = root.parent / '.meido-memes'
    metadata = private / 'metadata' / (operation + '.json')
    image_stage = private / 'staging' / (operation + '-image.tmp')
    metadata_stage = private / 'staging' / (operation + '-metadata.tmp')
    manifest_stage = private / 'staging' / (operation + '-manifest.tmp')
    metadata_raw = _json({'operationId': operation, 'sourcePublicId': identifier, 'sourceSha256': _hash(raw),
        'path': target.relative_to(root).as_posix(), 'sha256': _hash(raw), 'bytes': len(raw),
        'roleId': role, 'managedEpoch': grant.managedEpoch}).encode('utf-8')
    planned = {'kind': 'copy', 'sourcePublicId': identifier, 'sourceSha256': _hash(raw),
        'sourceIdentity': expected_source.identity, 'sourceMetadata': expected_source.public,
        'provenance': library.provenance(role, context, grant, turn),
        'manifest': str(manifest), 'manifestSha256': _hash(manifest_raw), 'manifestContent': manifest_raw.decode('utf-8'),
        'image': str(target), 'imageSha256': _hash(raw), 'imageBytes': len(raw),
        'metadata': str(metadata), 'metadataSha256': _hash(metadata_raw), 'metadataContent': metadata_raw.decode('utf-8'),
        'stages': [str(image_stage), str(metadata_stage), str(manifest_stage)]}
    directories = (private, private / 'staging', private / 'metadata', target.parent)
    byte_peak = len(raw) + len(metadata_raw) + len(manifest_raw) + len(_json(planned).encode('utf-8')) + len(_json(expected).encode('utf-8'))
    plan = _CopyPlan(operation_id=operation, category=category, source_id=identifier,
        source_identity=expected_source.identity, source_digest=expected_source.digest,
        source_public_json=_json(expected_source.public), expected_json=_json(expected), planned_json=_json(planned),
        image=_CopyFile(target=target, stage=image_stage, content=raw, sha256=_hash(raw)),
        metadata=_CopyFile(target=metadata, stage=metadata_stage, content=metadata_raw, sha256=_hash(metadata_raw)),
        manifest=_CopyFile(target=manifest, stage=manifest_stage, content=manifest_raw, sha256=_hash(manifest_raw)))
    with library.storage.claim(role, operation, byte_peak, 3 + sum(not path.exists() for path in directories), context):
        return _publish(library, turn, context, plan)


def _publish(library, turn, context, plan: _CopyPlan):
    role, grant = turn.origin.role_id, turn.config.driftMeme
    operation, identifier, category = plan.operation_id, plan.source_id, plan.category
    manifest, target, metadata = plan.manifest.target, plan.image.target, plan.metadata.target
    root, private = manifest.parent, metadata.parent.parent
    image_stage, metadata_stage, manifest_stage = plan.image.stage, plan.metadata.stage, plan.manifest.stage
    raw, metadata_raw, manifest_raw = plan.image.content, plan.metadata.content, plan.manifest.content
    expected = json.loads(plan.expected_json)
    file_guard = build_file_tools(role, root)[0]
    for directory in (private, private / 'staging', private / 'metadata'):
        _directory_identity(directory.parent)
        directory.mkdir(exist_ok=True)
        _directory_identity(directory)
    with turn.app._transaction() as db:
        db.execute('INSERT INTO autonomy_meme_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (operation, role, context.run_id, context.tool_call_id, grant.managedEpoch, str(target), plan.expected_json,
             plan.image.sha256, plan.planned_json, str(image_stage), 'reserved', '{}'))
        from . import retention
        retention.check_capacity(db, role)
    owned = {}
    facts: dict[str, Any] = {'physicalPhase': 'reserved', 'staging': {}, 'privateParents':
        [[str(path), list(_directory_identity(path))] for path in (private, private / 'staging', private / 'metadata')]}
    def authorize():
        library.authorize(turn, context, 'meme_copy_image', category)
        if context.signal.is_cancelled() or not turn.config.driftMeme.copyEnabled:
            raise PermissionError('meme copy was cancelled or sealed')
    def stage(path, value):
        authorize()
        with path.open('xb') as stream:
            info = os.fstat(stream.fileno())
            owned[path] = (info.st_dev, info.st_ino)
            facts['staging'][str(path)] = list(owned[path])
            record('reserved')
            for offset in range(0, len(value), 65536):
                authorize()
                stream.write(value[offset:offset + 65536])
            stream.flush()
            os.fsync(stream.fileno())
    def record(status):
        if status in {'reserved', 'files_ready', 'files_published', 'manifest_published'}:
            facts['physicalPhase'] = status
        with turn.app._transaction() as db:
            db.execute('UPDATE autonomy_meme_operations SET status=?,actual=? WHERE operation_id=?',
                (status, _json(facts), operation))
    try:
        stage(image_stage, raw)
        stage(metadata_stage, metadata_raw)
        stage(manifest_stage, manifest_raw)
        prepared = {}
        for file in (plan.image, plan.metadata, plan.manifest):
            path = file.stage
            if _identity(path)[:2] != owned[path] or _hash(_read(path, 10 * 1024 * 1024)) != file.sha256:
                raise PermissionError('meme staging does not match its original prepared bytes')
            prepared[path] = _identity(path)
        record('files_ready')
        # Revalidate the actual original upload before entering short publication locks.
        source = _load(turn.app, role, identifier)[0]
        if (source.identity, source.digest, _json(source.public)) != (plan.source_identity, plan.source_digest, plan.source_public_json):
            raise PermissionError('meme copy source changed before publication')
        if library._fact(role, manifest) != expected:
            raise PermissionError('conflict: original manifest changed during preparation')
        def published(path, stage, digest):
            value = library._fast_fact(role, path)
            if value['file'] != list(prepared[stage]):
                raise PermissionError('meme publication no longer matches its prepared identity')
            return {**value, 'sha256': digest}
        def rename(stage, target, kind):
            os.replace(stage, target)
            # Returning from rename is already a physical fact, even if a later
            # identity check cannot confirm ownership. Never downgrade it to ready.
            facts.setdefault('renameReturned', []).append(kind)
            facts['physicalPhase'] = 'manifest_published' if kind == 'manifest' else 'files_published'
        with ExitStack() as stack:
            for path in sorted((target, metadata, manifest), key=lambda item: os.path.normcase(str(item))):
                stack.enter_context(_file_lock(path, context, wait_seconds=.25))
            stack.enter_context(turn.app._gate)
            authorize()
            if not library._matches(role, manifest, expected) or target.exists() or metadata.exists():
                raise PermissionError('conflict: original library observation no longer matches')
            file_guard._path(str(target))
            target.parent.mkdir(exist_ok=True)
            file_guard._path(str(target))
            rename(image_stage, target, 'image')
            facts['image'] = published(target, image_stage, plan.image.sha256)
            record('files_published')
            authorize()
            rename(metadata_stage, metadata, 'metadata')
            facts['metadata'] = published(metadata, metadata_stage, plan.metadata.sha256)
            record('files_published')
            authorize()
            if not library._matches(role, manifest, expected):
                raise PermissionError('conflict: owner manifest changed after file publication')
            rename(manifest_stage, manifest, 'manifest')
            facts['manifest'] = published(manifest, manifest_stage, plan.manifest.sha256)
            with turn.app._transaction() as db:
                library.advance_revision(db, role, grant, expected['libraryRevision'])
            facts['publishedLibraryRevision'] = expected['libraryRevision'] + 1
            record('manifest_published')
            if (facts['image']['sha256'] != plan.image.sha256 or facts['metadata']['sha256'] != plan.metadata.sha256
                    or facts['manifest']['sha256'] != plan.manifest.sha256):
                raise PermissionError('meme copy publication facts are incomplete')
            record('recorded')
        return {'status': 'recorded', 'operationId': operation, 'sourcePublicId': identifier,
            'path': '@memes/' + target.relative_to(root).as_posix(), 'sha256': plan.image.sha256,
            'bytes': len(raw), 'doesNotCreateSendingIdentity': True}
    except BaseException as error:
        record('conflict' if isinstance(error, PermissionError) else 'cleanup_pending')
        raise
    finally:
        for path, identity in owned.items():
            if path.exists():
                if any(list(_directory_identity(Path(parent))) != identity for parent, identity in facts['privateParents']):
                    raise PermissionError('meme staging cleanup cannot use a replaced private parent')
                if _identity(path)[:2] != identity:
                    raise PermissionError('meme staging cleanup cannot adopt a replaced file')
                path.unlink()
