"""Plugin-owned packages and binding revisions; RoleStore owns role identity."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from threading import RLock, Lock
import uuid
import time

from .packages import decode_package


ROLE_BYTES = 256 * 1024 * 1024
GLOBAL_BYTES = 1024 * 1024 * 1024
STATE_BYTES = 64 * 1024 * 1024


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


class PetStore:
    def __init__(self, roles, root: Path):
        self.roles = roles
        self.root = root.parent.resolve() / root.name
        self._gate = RLock()
        self._imports = Lock()
        self.root.mkdir(exist_ok=True)
        self._checked(self.root)

    def _checked(self, path: Path) -> Path:
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(self.root):
            raise ValueError('桌宠资源目录已改变')
        current = self.root
        for part in ((), *[(name,) for name in path.relative_to(self.root).parts]):
            if part:
                current = current / part[0]
            if current.exists() or current.is_symlink():
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('桌宠资源不接受链接或重解析目录')
        return path

    def _directory(self, path: Path) -> Path:
        self._checked(path)
        path.mkdir(parents=True, exist_ok=True)
        self._checked(path)
        return path

    def _read_state(self):
        path = self._checked(self.root / 'state.json')
        if not path.exists():
            return {'version': 1, 'activeRoleId': None, 'roles': {}}
        with path.open('rb') as stream:
            raw = stream.read(STATE_BYTES + 1)
        if len(raw) > STATE_BYTES:
            raise ValueError('桌宠状态容量超额')
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('roles'), dict):
            raise ValueError('桌宠状态文件无效')
        return data

    def _save(self, state):
        raw = _json(state)
        if len(raw) > STATE_BYTES:
            raise ValueError('桌宠状态容量超额')
        temporary = self._checked(self.root / ('.state-' + uuid.uuid4().hex))
        try:
            with temporary.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._checked(self.root / 'state.json')
            os.replace(temporary, self.root / 'state.json')
        finally:
            temporary.unlink(missing_ok=True)

    def _role(self, state, role_id):
        if not isinstance(role_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', role_id) or self.roles.get(role_id) is None:
            raise KeyError('角色不存在')
        return state['roles'].setdefault(role_id, {'revision': 0, 'generation': '', 'enabled': False,
            'selectedPackageId': None, 'packages': [], 'claims': []})

    @staticmethod
    def _changed(role):
        role['revision'] += 1
        role['generation'] = uuid.uuid4().hex

    def _usage(self, path: Path) -> int:
        if not path.exists():
            return 0
        total = 0
        for directory, directories, files in os.walk(self._checked(path), followlinks=False):
            for name in (*directories, *files):
                target = self._checked(Path(directory) / name)
                info = target.lstat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                elif not stat.S_ISDIR(info.st_mode):
                    raise ValueError('桌宠资源包含非常规文件')
        return total

    @staticmethod
    def _public_package(package):
        return {key: deepcopy(package[key]) for key in ('id', 'format', 'displayName', 'description', 'actions')}

    def status(self, role_id):
        with self._gate:
            state = self._read_state()
            role = self._role(state, role_id)
            return {'roleId': role_id, 'revision': role['revision'], 'generation': role['generation'],
                'enabled': role['enabled'], 'selectedPackageId': role['selectedPackageId'],
                'packages': [self._public_package(package) for package in role['packages']],
                'activeRoleId': state['activeRoleId']}

    def binding(self, role_id):
        with self._gate:
            state = self._read_state()
            role = self._role(state, role_id)
            package = next((item for item in role['packages'] if item['id'] == role['selectedPackageId']), None)
            return deepcopy(role), deepcopy(package), state['activeRoleId']

    def active_role(self):
        with self._gate:
            role_id = self._read_state()['activeRoleId']
            return role_id if role_id is not None and self.roles.get(role_id) is not None else None

    def native_binding(self, role_id):
        role, package, active = self.binding(role_id)
        if active != role_id or not role['enabled'] or package is None:
            return None
        self.asset(role_id, package['id'], 'sprite')
        if any(name.startswith('preview.') for name in package['hashes']):
            self.asset(role_id, package['id'], 'preview')
        directory = self._checked(self.root / 'packages' / role_id / package['key'])
        return {'roleId': role_id, 'packageId': package['id'], 'generation': role['generation'],
            'displayName': package['displayName'], 'actions': deepcopy(package['actions']),
            'spritePath': str(directory / 'spritesheet.webp'), 'spriteSha256': package['hashes']['spritesheet.webp']}

    def import_package(self, role_id: str, raw: bytes):
        with self._imports:
            package = decode_package(raw)
            with self._gate:
                state = self._read_state()
                role = self._role(state, role_id)
                if len(role['packages']) >= 8 or any(item['id'] == package.package_id for item in role['packages']):
                    raise ValueError('每角色最多8个包，重复ID不能覆盖已有包')
                key = uuid.uuid4().hex
                assets = {'spritesheet.webp': package.sprite, 'pet.json': _json(package.manifest())}
                if package.preview is not None:
                    assets['preview.webp' if package.preview_media_type == 'image/webp' else 'preview.png'] = package.preview
                metadata = {**package.manifest(), 'key': key,
                    'hashes': {name: hashlib.sha256(content).hexdigest() for name, content in assets.items()}}
                role['packages'].append(metadata)
                role['revision'] += 1
                amount = sum(map(len, assets.values()))
                if (self._usage(self.root / 'packages') + amount + len(_json(state)) > GLOBAL_BYTES
                        or self._usage(self.root / 'packages' / role_id) + amount + len(_json(role)) > ROLE_BYTES):
                    raise ValueError('桌宠包存储配额不足')
                parent = self._directory(self.root / 'packages' / role_id)
                staging = parent / ('.import-' + key)
                destination = parent / key
                staging.mkdir()
                published = False
                try:
                    for name, content in assets.items():
                        (staging / name).write_bytes(content)
                    self._checked(destination)
                    os.replace(staging, destination)
                    published = True
                    self._save(state)
                except BaseException:
                    target = destination if published else staging
                    self._checked(target)
                    if target.parent != parent or target == self.root:
                        raise ValueError('桌宠暂存归属无法确认')
                    shutil.rmtree(target)
                    raise
                return self.status(role_id)

    def configure(self, role_id: str, *, expected_revision: int, package_id: str | None, enabled: bool):
        with self._gate:
            state = self._read_state()
            role = self._role(state, role_id)
            if role['revision'] != expected_revision:
                raise ValueError('桌宠配置已改变，请重新加载')
            if package_id is not None and not any(item['id'] == package_id for item in role['packages']):
                raise ValueError('桌宠包不属于该角色')
            if enabled and package_id is None:
                raise ValueError('请先选择桌宠包')
            if enabled:
                for other_id, other in state['roles'].items():
                    if other_id != role_id and other['enabled']:
                        other['enabled'] = False
                        self._changed(other)
                state['activeRoleId'] = role_id
            elif state['activeRoleId'] == role_id:
                state['activeRoleId'] = None
            role.update({'selectedPackageId': package_id, 'enabled': enabled})
            self._changed(role)
            self._save(state)
            return self.status(role_id)

    def asset(self, role_id: str, package_id: str, asset: str):
        with self._gate:
            role = self._role(self._read_state(), role_id)
            package = next((item for item in role['packages'] if item['id'] == package_id), None)
            if package is None:
                raise KeyError('桌宠包不存在')
            names = {'sprite': 'spritesheet.webp'}
            names['preview'] = next((name for name in package['hashes'] if name.startswith('preview.')), 'spritesheet.webp')
            if asset not in names or not re.fullmatch(r'[a-f0-9]{32}', package['key']):
                raise ValueError('桌宠素材无效')
            name = names[asset]
            path = self._checked(self.root / 'packages' / role_id / package['key'] / name)
            if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('桌宠素材已改变')
            raw = path.read_bytes()
            if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != package['hashes'][name]:
                raise ValueError('桌宠素材已改变')
            return raw, 'image/png' if name.endswith('.png') else 'image/webp'

    def claim(self, role_id, generation, run_id):
        with self._gate:
            state = self._read_state()
            role = self._role(state, role_id)
            if not role['enabled'] or state['activeRoleId'] != role_id or role['generation'] != generation:
                raise ValueError('桌宠绑定已改变或许可已关闭')
            now = time.time()
            if run_id in role['claims']:
                raise ValueError('每个真实回合最多一次桌宠动作')
            if now - role.get('lastActionAt', 0) < 3:
                raise ValueError('桌宠动作间隔至少3秒')
            role['claims'] = [*role['claims'][-4095:], run_id]
            role['lastActionAt'] = now
            self._save(state)

    def remove_package(self, role_id, package_id, expected_revision):
        with self._gate:
            state = self._read_state()
            role = self._role(state, role_id)
            if role['revision'] != expected_revision:
                raise ValueError('桌宠配置已改变，请重新加载')
            package = next((item for item in role['packages'] if item['id'] == package_id), None)
            if package is None:
                raise KeyError('桌宠包不存在')
            if package_id == role['selectedPackageId'] and role['enabled']:
                raise ValueError('请先停用并解除选择，再删除桌宠包')
            role['packages'].remove(package)
            if role['selectedPackageId'] == package_id:
                role['selectedPackageId'] = None
                self._changed(role)
            else:
                role['revision'] += 1
            self._save(state)
            directory = self._checked(self.root / 'packages' / role_id / package['key'])
            if directory.parent != self.root / 'packages' / role_id or not re.fullmatch(r'[a-f0-9]{32}', package['key']):
                raise ValueError('桌宠包目录归属不明')
            shutil.rmtree(directory)

    def revoke(self, role_id):
        with self._gate:
            state = self._read_state()
            role = self._role(state, role_id)
            role['enabled'] = False
            self._changed(role)
            if state['activeRoleId'] == role_id:
                state['activeRoleId'] = None
            self._save(state)

    def purge(self, role_id):
        with self._gate:
            state = self._read_state()
            if state['activeRoleId'] == role_id:
                raise ValueError('桌宠绑定尚未撤销')
            # Role IDs were admitted by RoleStore; never accept arbitrary paths.
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', role_id):
                raise ValueError('角色身份无效')
            directory = self._checked(self.root / 'packages' / role_id)
            if directory.exists():
                self._usage(directory)
                shutil.rmtree(directory)
            state['roles'].pop(role_id, None)
            self._save(state)
