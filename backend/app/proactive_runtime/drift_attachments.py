"""Freeze role-owned uploaded attachment references, never caller paths or bytes."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from ..chat_attachments import public_attachment
from .source_authoring import _directory_identity
from .source_validation import _read, _unique_object
from .drift_workspace import _identity


@dataclass(frozen=True)
class Attachment:
    public: dict
    identity: tuple
    digest: str


def _load(app, role_id, identifier):
    if not isinstance(identifier, str) or re.fullmatch(r'[a-f0-9]{32}', identifier) is None:
        raise PermissionError('invalid Drift attachment identifier')
    root = app.drift_skills.workspace(role_id) / 'attachments'
    root_identity = _directory_identity(root)
    metadata_path = root / (identifier + '.json')
    metadata_identity = _identity(metadata_path)
    value = json.loads(_read(metadata_path, 64 * 1024), object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or value.get('id') != identifier or not isinstance(value.get('path'), str):
        raise PermissionError('Drift attachment has no actual current-role ownership')
    path = Path(value['path'])
    if not path.is_absolute() or path.parent != root or not path.name.startswith(identifier + '-'):
        raise PermissionError('Drift attachment path is outside the host current-role upload')
    identity = _identity(path)
    raw = _read(path, 10 * 1024 * 1024)
    public = public_attachment(value)
    if public.get('size') != len(raw) or set(public) != {'id', 'name', 'mediaType', 'size', 'kind'}:
        raise ValueError('Drift attachment metadata does not match its actual file')
    if (_directory_identity(root), _identity(metadata_path), _identity(path)) != (root_identity, metadata_identity, identity):
        raise PermissionError('Drift attachment identity changed while freezing')
    return Attachment(public, (root_identity, metadata_identity, identity), hashlib.sha256(raw).hexdigest()), raw


def bind(app, role_id, identifier):
    return _load(app, role_id, identifier)[0]


def selected(turn, identifiers):
    result = []
    for identifier in identifiers:
        expected = turn.attachments.get(identifier)
        if expected is None or bind(turn.app, turn.origin.role_id, identifier) != expected:
            raise PermissionError('Drift attachment was not frozen or changed after admission')
        result.append(expected.public)
    return tuple(result)


def frozen_bytes(turn, identifiers):
    """Carry only the exact original run's verified bytes across adapter awaits."""
    if len(identifiers) > 4 or len(set(identifiers)) != len(identifiers):
        raise PermissionError('invalid original Drift attachment collection')
    result = {}
    for identifier in identifiers:
        expected = turn.attachments.get(identifier)
        actual, raw = _load(turn.app, turn.origin.role_id, identifier)
        if expected is None or actual != expected:
            raise PermissionError('Drift attachment bytes no longer match the original run')
        result[identifier] = raw
    return result
