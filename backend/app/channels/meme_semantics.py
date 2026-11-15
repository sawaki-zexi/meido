"""One durable meaning record per role-owned original, beside existing chat metadata.

The manifest remains the category authority. This adapter reads old chat/Drift
records without inventing a visual observation, and never grants source access.
Writes are prepared here and published by the existing owner publication journal.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from ..proactive_runtime.source_authoring import _directory_identity
from ..proactive_runtime.source_validation import _read

MAX_RECORD_BYTES = 512 * 1024
MAX_SOURCES = 128
MAX_REVISIONS = 128


def metadata_path(library, role_id, digest):
    return library.root(role_id).parent / '.meido-memes' / 'chat' / (digest + '.json')


def _record(path):
    if not path.exists():
        return None
    _directory_identity(path.parent)
    _directory_identity(path.parent.parent)
    value = json.loads(_read(path, MAX_RECORD_BYTES))
    if not isinstance(value, dict):
        raise ValueError('invalid material meaning record')
    return value


def _legacy(role_id, path, digest, category, value, source):
    description = value.get('description') or category.get('desc', '')
    description = description[:2000] if isinstance(description, str) else ''
    return {'version': 2, 'roleId': role_id, 'sha256': digest,
        'assetId': hashlib.sha256(('material:' + role_id + ':' + digest).encode()).hexdigest()[:32],
        'paths': [], 'name': value.get('name') or path.name, 'description': description,
        'enabled': True, 'revision': 0, 'history': [],
        'basis': {'kind': 'legacy_description' if description else 'unverified'},
        'author': {'kind': 'legacy'}, 'sources': [{'kind': source, 'scope': 'role'}],
        'source': source, 'semanticStatus': 'ready' if description else 'pending'}


def _changed(previous, digest, relative):
    value = deepcopy(previous)
    value['history'] = [*value['history'], historical(previous)]
    value.update(sha256=digest, description='', basis={'kind': 'unverified'},
        semanticStatus='content_changed', revision=previous['revision'] + 1)
    if relative not in value['paths']:
        value['paths'].append(relative)
    return value


def meaning_for(library, role_id, path, digest, category):
    root = library.root(role_id)
    relative = path.relative_to(root).as_posix()
    value = _record(metadata_path(library, role_id, digest))
    if value is not None:
        if value.get('roleId') != role_id or value.get('sha256') != digest:
            raise ValueError('material meaning does not match its role and original')
        if value.get('version') == 2:
            return deepcopy(value)
        return _legacy(role_id, path, digest, category, value,
            'owner_import' if value.get('source') == 'owner_import' else 'chat')
    # A replacement at a known location is not a new legacy image. Retain its
    # asset identity and corrections, but never apply old meaning to new pixels.
    directory = metadata_path(library, role_id, digest).parent
    if directory.exists():
        _directory_identity(directory)
        entries = list(directory.glob('*.json'))
        if len(entries) > 1024:
            raise ValueError('material meaning scan exceeds finite limit')
        matches = []
        for item in entries:
            previous = _record(item)
            if (previous and previous.get('version') == 2 and previous.get('roleId') == role_id
                    and relative in previous.get('paths', [])):
                matches.append(previous)
            elif (previous and previous.get('version') != 2 and previous.get('roleId') == role_id
                    and previous.get('sha256') == path.stem and item.stem == path.stem):
                matches.append(_legacy(role_id, path, previous['sha256'], category, previous,
                    'owner_import' if previous.get('source') == 'owner_import' else 'chat'))
        if matches:
            previous = max(matches, key=lambda item: item['revision'])
            return _changed(previous, digest, relative)
    drift = root.parent / '.meido-memes' / 'metadata' / (path.stem + '.json')
    previous = _record(drift)
    if previous and previous.get('roleId') == role_id and previous.get('path') == relative:
        old_digest = previous.get('sha256')
        if not isinstance(old_digest, str) or len(old_digest) != 64:
            raise ValueError('invalid legacy original digest')
        legacy = _legacy(role_id, path, old_digest, category, previous, 'drift')
        return legacy if old_digest == digest else _changed(legacy, digest, relative)
    return _legacy(role_id, path, digest, category, {}, 'legacy')


def historical(value):
    return {key: deepcopy(value.get(key)) for key in
        ('revision', 'sha256', 'description', 'name', 'enabled', 'basis', 'author')}


def new_meaning(role_id, digest, relative, description, *, name='', basis=None, source=None, author=None):
    basis = basis or {'kind': 'legacy_description' if description else 'unverified'}
    source = source or {'kind': 'chat', 'scope': 'role'}
    return {'version': 2, 'roleId': role_id, 'sha256': digest, 'assetId': uuid.uuid4().hex,
        'paths': [relative], 'name': name, 'description': description, 'enabled': True,
        'revision': 1, 'history': [], 'basis': basis, 'author': author or {'kind': 'legacy'},
        'sources': [source], 'source': source['kind'],
        'semanticStatus': 'ready' if description and basis['kind'] != 'unverified' else 'pending'}


def add_source(value, source, relative):
    result = deepcopy(value)
    if source not in result['sources']:
        if len(result['sources']) >= MAX_SOURCES:
            raise ValueError('material source history is full; existing sources were retained')
        result['sources'].append(deepcopy(source))
    if relative not in result['paths']:
        result['paths'].append(relative)
    if result != value:
        result['revision'] += 1
    return result


def revise(value, changes, *, expected_revision, basis, author):
    if value['revision'] != expected_revision:
        raise PermissionError('conflict: material meaning changed; read it again')
    if 'description' in changes and basis['kind'] == 'model_observation' and value['basis']['kind'] == 'owner_confirmed':
        raise PermissionError('主人确认的说明不能由模型观察覆盖，请主人在素材库中修订')
    result = deepcopy(value)
    if len(result['history']) >= MAX_REVISIONS:
        raise ValueError('material revision history is full; prior corrections were retained')
    result['history'].append(historical(value))
    result.update(changes, revision=value['revision'] + 1, author=author)
    if 'description' in changes:
        result['basis'] = basis if changes['description'] else {'kind': 'unverified'}
        result['semanticStatus'] = 'ready' if changes['description'] and basis['kind'] != 'unverified' else 'pending'
    return result


def encode(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError('material meaning history exceeds storage limit; existing data was retained')
    return raw


def public_meaning(value):
    return {'assetId': value['assetId'], 'semanticRevision': value['revision'],
        'semanticStatus': value['semanticStatus'], 'basis': deepcopy(value['basis']),
        'author': deepcopy(value['author']), 'history': deepcopy(value['history']),
        'sources': [{'kind': item['kind'], 'scope': '当前会话' if item.get('sessionKey') else '当前角色'}
                    for item in value['sources']]}


def source_allowed(value, session_key, validator):
    for source in value['sources']:
        if source.get('scope') == 'role':
            return True
        if source.get('sessionKey') != session_key:
            continue
        authority = source.get('authority')
        if authority is None or validator is not None and validator(authority):
            return True
    return False
