"""Verify the fixed local build and its pinned Windows distribution resources."""
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise ValueError('fixed native resource is missing or redirected')
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def verify_bundle(executable: Path) -> dict:
    references = Path(__file__).resolve().parents[3] / 'third_party/herdr-owned'
    pinned = json.loads((references / 'manifest.json').read_text(encoding='utf-8'))
    lock = json.loads((references / 'conpty.lock.json').read_text(encoding='utf-8'))
    manifest_path = executable.parent / 'meido-owned-build.json'
    if not manifest_path.is_file() or manifest_path.is_symlink() or manifest_path.stat().st_size > 65536:
        raise ValueError('owned build manifest is missing or invalid')
    value = json.loads(manifest_path.read_text(encoding='utf-8'))
    if any(value.get(key) != pinned[key] for key in ('commit', 'version', 'protocol', 'patchSha256', 'rust', 'zig', 'target', 'acceptedBuildSha256', 'buildMode', 'msvc', 'sdk')):
        raise ValueError('owned build source, patch or toolchain does not match the fixed manifest')
    required = {item['destination']: item['sha256'] for item in lock['bundles']['x86_64']['files'] + lock['notices']}
    required.update(pinned['notices'])
    required[executable.name] = pinned['acceptedBuildSha256']
    for path in executable.parent.rglob('*'):
        if path.is_file() and path.suffix.lower() in {'.exe', '.dll', '.com', '.bat', '.cmd', '.ps1'} and path.relative_to(executable.parent).as_posix() not in required:
            raise ValueError('unexpected executable in the fixed native bundle')
    for relative, expected in required.items():
        path = executable.parent / relative
        if not expected or digest(path) != expected or value['files'].get(relative) != expected:
            raise ValueError('owned build resource hash mismatch')
    return {'manifestSha256': digest(manifest_path), 'patchSha256': pinned['patchSha256']}
