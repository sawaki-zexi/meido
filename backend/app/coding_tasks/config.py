from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .git_repository import repository_identity
from .owned_process import OwnedProcess
from .native_build import verify_bundle

CODEX_EXEC_VERSION = '0.159.3'
CODEX_EXEC_TEMPLATE = 'codex-exec-0.159.3-workspace-write-never-network-off-v1'


def codex_artifact(profile):
    if profile['kind'] != 'codex' or profile['executor'] != 'codex_exec' or profile['version'] != CODEX_EXEC_VERSION:
        raise ValueError('Codex exec requires the fixed supported Codex CLI version')
    return {key: profile[key] for key in ('executor', 'executable', 'sha256', 'version')} | {'permissionTemplate': CODEX_EXEC_TEMPLATE}


class CodingRoleConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    repoGrantIds: list[str] = Field(default_factory=list)
    profileIds: list[str] = Field(default_factory=list)
    defaultHerdrProfileId: str | None = None
    defaultCodexProfileId: str | None = None
    maxAttemptsPerDay: int = Field(default=5, ge=1, le=100)


class RepositoryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str | None = None
    root: str
    allowedRoleIds: list[str] = Field(default_factory=list)
    baseRef: str = 'main'
    remote: str = 'origin'
    validation: list[dict] = Field(default_factory=list)
    localCommit: bool = False
    fetch: bool = False
    push: bool = False
    draftPr: bool = False


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    kind: Literal['codex', 'claude']
    executor: Literal['herdr', 'codex_exec'] = 'herdr'
    executable: str
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    version: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    authDirectory: str
    allowedRoleIds: list[str] = Field(default_factory=list)
    configurationManifest: list[dict] = Field(default_factory=list)


def executable_identity(value: str, expected: str) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError('CLI executable must be a fixed absolute file')
    path = path.resolve(strict=True)
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError('CLI executable exceeds verification budget')
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != expected:
        raise ValueError('CLI executable SHA-256 differs from the approved build')
    return str(path)


class CodingConfigStore:
    def __init__(self, path: Path, *, native_supported=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.native_supported = native_supported or (lambda: sys.platform == 'win32')
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute('CREATE TABLE IF NOT EXISTS coding_configuration (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0)')
        self._db.execute('INSERT OR IGNORE INTO coding_configuration(id) VALUES(1)')
        self._db.execute('CREATE TABLE IF NOT EXISTS coding_repositories (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self._db.execute('CREATE TABLE IF NOT EXISTS coding_profiles (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self._db.execute('CREATE TABLE IF NOT EXISTS coding_native_build (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        self._db.execute('CREATE TABLE IF NOT EXISTS coding_role_generations(role_id TEXT PRIMARY KEY,generation INTEGER NOT NULL)')
        self._db.execute('CREATE TABLE IF NOT EXISTS coding_delegate_generations(role_id TEXT PRIMARY KEY,generation INTEGER NOT NULL)')
        self._db.commit()

    def role_generation(self, role_id: str) -> int:
        with self._lock:
            row = self._db.execute('SELECT generation FROM coding_role_generations WHERE role_id=?', (role_id,)).fetchone()
            return int(row[0]) if row else 0

    def revoke_role(self, role_id: str):
        with self._lock, self._db:
            self._db.execute('INSERT INTO coding_role_generations VALUES(?,1) ON CONFLICT(role_id) DO UPDATE SET generation=generation+1', (role_id,))

    def delegation_generation(self, role_id: str) -> int:
        with self._lock:
            row = self._db.execute('SELECT generation FROM coding_delegate_generations WHERE role_id=?', (role_id,)).fetchone()
            return int(row[0]) if row else 0

    def revoke_delegation(self, role_id: str):
        with self._lock, self._db:
            self._db.execute('INSERT INTO coding_delegate_generations VALUES(?,1) ON CONFLICT(role_id) DO UPDATE SET generation=generation+1', (role_id,))

    def public(self) -> dict:
        with self._lock:
            return {'enabled': bool(self._db.execute('SELECT enabled FROM coding_configuration WHERE id=1').fetchone()[0]),
                    'repositories': [json.loads(row[0]) for row in self._db.execute('SELECT body FROM coding_repositories ORDER BY id')],
                    'profiles': [json.loads(row[0]) for row in self._db.execute('SELECT body FROM coding_profiles ORDER BY id')],
                    'nativeBuild': self.native_build()}

    def native_build(self) -> dict | None:
        with self._lock:
            row = self._db.execute('SELECT body FROM coding_native_build WHERE id=1').fetchone()
            return json.loads(row[0]) if row else None

    def set_native_build(self, executable: str, sha256: str) -> dict:
        path = executable_identity(executable, sha256)
        verified_bundle = verify_bundle(Path(path))
        environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP') if key in os.environ}
        process = OwnedProcess([path, '--meido-owned-capabilities'], Path(path).parent, environment, output_bytes=65536)
        response = process.communicate(timeout=10)
        try:
            capabilities = json.loads(response.stdout)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError('owned Herdr capability response is invalid') from error
        if response.returncode or capabilities.get('protocol') != 1 or capabilities.get('version') != '0.9.4-meido.1' or capabilities.get('upstreamCommit') != '7e26bad94044eba205033111a0ce8033ede7e24f':
            raise ValueError('a fixed Meido owned Herdr build is required')
        required = {'explicit-session', 'foreground-only', 'no-registry-env', 'no-shell-profile',
                    'fixed-agent-template', 'no-restore-update-plugin', 'bounded-session-hook', 'host-owned-job'}
        if not required.issubset(capabilities.get('features', [])):
            raise ValueError('owned Herdr capabilities are incomplete')
        value = {'executable': path, 'sha256': sha256, 'protocol': 1, 'version': capabilities['version'], **verified_bundle}
        with self._lock, self._db:
            self._db.execute('INSERT INTO coding_native_build(id,body) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body', (json.dumps(value),))
        return value

    def set_enabled(self, enabled: bool) -> None:
        with self._lock, self._db:
            self._db.execute('UPDATE coding_configuration SET enabled=? WHERE id=1', (int(enabled),))

    def save_repository(self, data: dict) -> dict:
        setting = RepositoryInput.model_validate(data)
        identity = repository_identity(setting.root)
        if not setting.baseRef or setting.baseRef.startswith('-') or any(c.isspace() for c in setting.baseRef):
            raise ValueError('invalid Git base ref')
        value = setting.model_dump(exclude={'id'}) | {'id': setting.id or 'repo_' + uuid.uuid4().hex,
                                                     'root': identity['root'], 'commonDir': identity['commonDir']}
        if any(value.get(key) for key in ('localCommit', 'fetch', 'push', 'draftPr')):
            raise ValueError('commit and publication are not available in this local-task ticket')
        for command in value['validation']:
            if (set(command) - {'id', 'argv', 'cwd'} or not isinstance(command.get('id'), str)
                    or not command['id'] or len(command['id']) > 100
                    or not isinstance(command.get('argv'), list) or not command['argv']
                    or not all(isinstance(item, str) and '\x00' not in item for item in command['argv'])
                    or not Path(command['argv'][0]).is_absolute()
                    or not Path(command['argv'][0]).is_file()
                    or Path(command.get('cwd', '.')).is_absolute() or '..' in Path(command.get('cwd', '.')).parts):
                raise ValueError('validation requires fixed IDs, executable and relative working directory')
        if len({item['id'] for item in value['validation']}) != len(value['validation']):
            raise ValueError('validation IDs must be unique')
        if value['validation'] and len(value['validation']) > 20:
            raise ValueError('validation command budget exceeded')
        value['rootIdentity'] = [Path(identity['root']).stat().st_dev, Path(identity['root']).stat().st_ino]
        value['commonIdentity'] = [Path(identity['commonDir']).stat().st_dev, Path(identity['commonDir']).stat().st_ino]
        value['revision'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        with self._lock, self._db:
            self._db.execute('INSERT INTO coding_repositories(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body', (value['id'], json.dumps(value)))
        return value

    def save_profile(self, data: dict) -> dict:
        setting = ProfileInput.model_validate(data)
        if setting.executor == 'codex_exec':
            codex_artifact(setting.model_dump())
        executable = executable_identity(setting.executable, setting.sha256)
        auth = Path(setting.authDirectory)
        if not auth.is_absolute() or not auth.is_dir() or auth.is_symlink():
            raise ValueError('CLI authentication must use an explicitly configured existing directory')
        value = setting.model_dump(exclude={'id'}) | {'id': setting.id or 'profile_' + uuid.uuid4().hex,
                                                      'executable': executable, 'authDirectory': str(auth.resolve(strict=True))}
        if value['configurationManifest']:
            raise ValueError('project CLI extensions are not supported by the fixed profile')
        credential = auth / ('auth.json' if value['kind'] == 'codex' else '.credentials.json')
        value['authenticationRevision'] = (hashlib.sha256(credential.read_bytes()).hexdigest()
            if credential.is_file() and not credential.is_symlink() and credential.stat().st_size <= 256*1024 else None)
        value['revision'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        value['verification'] = {'status': 'unverified', 'reason': '原生权限、扩展配置及精确 session 尚未核验'}
        with self._lock, self._db:
            self._db.execute('INSERT INTO coding_profiles(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body', (value['id'], json.dumps(value)))
        return value

    def repository(self, identifier: str) -> dict:
        with self._lock:
            row = self._db.execute('SELECT body FROM coding_repositories WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError('coding repository grant not found')
            return json.loads(row[0])

    def profile(self, identifier: str) -> dict:
        with self._lock:
            row = self._db.execute('SELECT body FROM coding_profiles WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError('coding CLI profile not found')
            return json.loads(row[0])

    def record_verification(self, identifier: str, *, revision: str, native_session: str,
                            resumed_session: str, manifest_hash: str) -> dict:
        """Host probe boundary; never accepted as model/tool arguments."""
        if not native_session or native_session != resumed_session or not manifest_hash:
            raise ValueError('native profile probe did not prove exact session continuation')
        with self._lock, self._db:
            value = self.profile(identifier)
            if value['revision'] != revision:
                raise ValueError('CLI profile changed while its native probe was running')
            value['verification'] = {'status': 'verified', 'revision': revision, 'manifestHash': manifest_hash,
                                     'nativeSession': native_session}
            self._db.execute('UPDATE coding_profiles SET body=? WHERE id=?', (json.dumps(value), identifier))
        return value

    def resolve(self, role_id: str, role: CodingRoleConfig, repository: str, *, kind: str | None = None,
                executor: str = 'herdr') -> tuple[dict, dict, dict]:
        setting = self.public()
        if not setting['enabled'] or not role.enabled:
            raise PermissionError('coding is disabled')
        if executor == 'codex_exec' and not self.native_supported():
            raise PermissionError('Codex exec native execution is only supported on Windows')
        grants = [item for item in setting['repositories'] if item['id'] in role.repoGrantIds
                  and role_id in item['allowedRoleIds'] and repository in {item['id'], item['root']}]
        if len(grants) != 1:
            raise PermissionError('repository is not granted to this role')
        profiles = [item for item in setting['profiles'] if item['id'] in role.profileIds
                    and role_id in item['allowedRoleIds'] and item['executor'] == executor
                    and (kind is None or item['kind'] == kind)]
        default = role.defaultHerdrProfileId if executor == 'herdr' else role.defaultCodexProfileId
        if kind is None:
            profiles = [item for item in profiles if item['id'] == default]
        elif default and any(item['id'] == default for item in profiles):
            profiles = [item for item in profiles if item['id'] == default]
        if len(profiles) != 1:
            raise PermissionError('CLI profile is unavailable or ambiguous')
        profile = profiles[0]
        if profile['verification']['status'] != 'verified' or profile['verification'].get('revision') != profile['revision']:
            raise PermissionError('CLI permissions, extensions and native session have not passed verification')
        build = setting['nativeBuild'] if executor == 'herdr' else codex_artifact(profile)
        if build is None:
            raise PermissionError('owned Herdr native build is not configured')
        executable_identity(profile['executable'], profile['sha256'])
        credential = Path(profile['authDirectory']) / ('auth.json' if profile['kind'] == 'codex' else '.credentials.json')
        if (not credential.is_file() or credential.is_symlink() or credential.stat().st_size > 256*1024
                or hashlib.sha256(credential.read_bytes()).hexdigest() != profile.get('authenticationRevision')):
            raise PermissionError('dedicated CLI authentication changed; save and qualify the profile again')
        if executor == 'herdr':
            executable_identity(build['executable'], build['sha256'])
            if verify_bundle(Path(build['executable'])) != {key: build[key] for key in ('manifestSha256', 'patchSha256')}:
                raise PermissionError('owned build manifest changed')
        identity = repository_identity(grants[0]['root'])
        if identity['commonDir'] != grants[0]['commonDir']:
            raise PermissionError('registered repository identity changed')
        for field, path in (('rootIdentity', identity['root']), ('commonIdentity', identity['commonDir'])):
            if grants[0][field] != [Path(path).stat().st_dev, Path(path).stat().st_ino]:
                raise PermissionError('registered repository was replaced')
        return grants[0], profile, build

    def still_authorized(self, role_id: str, role: CodingRoleConfig, record: dict) -> bool:
        setting = self.public()
        if not setting['enabled'] or not role.enabled:
            return False
        repository, profile = record['repository'], record['profile']
        granted_repo = any(item['id'] == repository['id'] and item['revision'] == repository['revision']
                           and role_id in item['allowedRoleIds'] for item in setting['repositories'])
        granted_profile = any(item['id'] == profile['id'] and item['revision'] == profile['revision']
                              and role_id in item['allowedRoleIds'] and item['verification']['status'] == 'verified'
                              for item in setting['profiles'])
        return bool(granted_repo and granted_profile and repository['id'] in role.repoGrantIds
                    and profile['id'] in role.profileIds
                    and (setting['nativeBuild'] if record['executor'] == 'herdr' else codex_artifact(profile)) == record.get('nativeBuild'))

    def availability(self, role_id: str, role: CodingRoleConfig, *, executor='herdr') -> dict:
        if not self.public()['enabled'] or not role.enabled:
            return {'status': 'disabled', 'reason': '编程委托尚未启用'}
        if not self.native_supported():
            return {'status': 'unavailable', 'reason': '编程委托首版仅支持 Windows 普通权限桌面宿主'}
        if not role.repoGrantIds:
            return {'status': 'unavailable', 'reason': '尚未配置可信仓库'}
        try:
            self.resolve(role_id, role, role.repoGrantIds[0], executor=executor)
            return {'status': 'available', 'reason': '固定 Codex exec、仓库及原生权限核验已通过' if executor == 'codex_exec'
                    else '固定原生宿主、仓库及专用 CLI profile 已核验'}
        except (ValueError, KeyError, PermissionError, OSError) as error:
            return {'status': 'unavailable', 'reason': str(error)[:300]}

    def close(self) -> None:
        self._db.close()
