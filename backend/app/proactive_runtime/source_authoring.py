"""Read a role-owned author draft without trusting, executing or registering it."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

from ..mcp_autonomy import declaration_hash
from .config import SourceGrant
from .context import _check_chain
from .source_validation import AUTHOR_PROFILE, MAX_FILES, MAX_ARTIFACT_BYTES, MAX_MANIFEST_BYTES, _read, _unique_object


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 240 or ':' in value or '\\' in value
            or PurePosixPath(value).is_absolute() or any(part in {'', '.', '..'} for part in value.split('/'))):
        raise ValueError('a relative artifact path is required')
    return value


def _directory_identity(path):
    info = _check_chain(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('artifact root is not a directory')
    return info.st_dev, info.st_ino


def preview_artifact(workspace, manifest_name, *, role_id, mcp_store=None, server_id=None):
    workspace = Path(workspace)
    workspace_identity = _directory_identity(workspace)
    path = workspace.joinpath(*_relative(manifest_name).split('/'))
    if not path.resolve().is_relative_to(workspace.resolve()):
        raise PermissionError('artifact is outside the current role workspace')
    artifact_identity = _directory_identity(path.parent)
    raw = _read(path, MAX_MANIFEST_BYTES)
    manifest = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(manifest, dict) or set(manifest) != {'profile', 'version', 'entrypoint', 'files'}
            or manifest['profile'] != AUTHOR_PROFILE or type(manifest['version']) is not int or manifest['version'] != 2
            or not isinstance(manifest['files'], dict) or not 1 <= len(manifest['files']) <= MAX_FILES):
        raise ValueError('an author-v2 manifest draft is required')
    entry = _relative(manifest['entrypoint'])
    if entry not in manifest['files']:
        raise ValueError('the entrypoint must be declared')
    files, contents, seen, total = [], {}, set(), 0
    for name in manifest['files']:
        _relative(name)
        if name.casefold() in seen:
            raise ValueError('artifact paths must be unique')
        seen.add(name.casefold())
        content = _read(path.parent.joinpath(*name.split('/')), MAX_ARTIFACT_BYTES - total)
        total += len(content)
        contents[name] = content
        files.append({'path': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    prepared = {**manifest, 'files': {item['path']: item['sha256'] for item in files}}
    result = {'manifestPath': str(path), 'manifestSha256': hashlib.sha256(raw).hexdigest(),
              'manifestReady': manifest == prepared, 'preparedManifest': json.dumps(prepared, ensure_ascii=False, indent=2) + '\n',
              'files': files, 'sourceGrants': [], 'configurationStatus': 'select_saved_server'}
    if server_id is not None:
        server = mcp_store.get(server_id) if mcp_store is not None else None
        if server is None or role_id not in server.role_ids:
            raise PermissionError('select an existing MCP server granted to this role')
        actual = path.parent.joinpath(*entry.split('/')).resolve()
        if len([arg for arg in server.command[1:] if Path(arg).is_absolute() and Path(arg).resolve() == actual]) != 1:
            raise PermissionError('the selected MCP command has another entrypoint')
        draft = json.loads(contents['source-draft.json'], object_pairs_hook=_unique_object)
        catalog = json.loads(contents['catalog.json'], object_pairs_hook=_unique_object)
        if (not isinstance(draft, dict) or set(draft) != {'serverId', 'eventDomain', 'secretRefs', 'sources'}
                or draft['serverId'] != server_id or not isinstance(draft['secretRefs'], list)
                or len(draft['secretRefs']) > 64
                or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', name) for name in draft['secretRefs'])
                or not isinstance(draft['sources'], list) or not 1 <= len(draft['sources']) <= 8
                or not isinstance(catalog, list) or len(catalog) > 128):
            raise ValueError('invalid source configuration draft')
        declarations = {tool['name']: tool for tool in catalog}
        grants = []
        for item in draft['sources']:
            if (not isinstance(item, dict) or not {'sourceId', 'channel', 'getTool'} <= set(item)
                    or set(item) - {'sourceId', 'channel', 'getTool', 'pollTool', 'ackTool'}):
                raise ValueError('invalid source operation draft')
            value = {**item, 'serverId': server_id, 'configRevision': server.revision, 'grantRevision': server.grant_id,
                     'eventDomain': draft['eventDomain'], 'enabled': False}
            for prefix in ('get', 'poll', 'ack'):
                name = item.get(prefix + 'Tool')
                if name is not None:
                    value[prefix + 'DefinitionHash'] = declaration_hash(declarations[name])
            value['ackContract'] = 'absolute-idempotent' if item.get('ackTool') else 'none'
            grants.append(SourceGrant.model_validate(value).model_dump(mode='json'))
        if len({item['sourceId'] for item in grants}) != len(grants):
            raise ValueError('duplicate source draft identity')
        result.update(sourceGrants=grants, configurationStatus='draft_only', requiredCredentialKeys=draft['secretRefs'])
    if _read(path, MAX_MANIFEST_BYTES) != raw:
        raise ValueError('manifest changed while previewing')
    for name, content in contents.items():
        if _read(path.parent.joinpath(*name.split('/')), len(content)) != content:
            raise ValueError('artifact changed while previewing')
    if (_directory_identity(workspace) != workspace_identity
            or _directory_identity(path.parent) != artifact_identity):
        raise ValueError('selected artifact directory changed while previewing')
    return result
