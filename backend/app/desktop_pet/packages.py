"""Bounded Shiori codex-sprite package admission; archives never supply code.

Adapted from Shiori-Agent 4f266ae desktop_pet/backend/package_{archive,images}.py.
Copyright (c) 2026 YinFengWindy. MIT; see third_party/shiori-agent/LICENSE.
"""
from dataclasses import dataclass
import io
import json
from pathlib import PurePosixPath
import re
import stat
import zipfile

from PIL import Image


MAX_ZIP_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 128
FORMAT = 'codex-sprite@1'
ACTION_STATES = frozenset({'idle', 'running-right', 'running-left', 'waving', 'jumping'})
USED_CELLS = (6, 8, 8, 4, 5, 8, 6, 6, 6)


@dataclass(frozen=True, slots=True)
class PackageData:
    package_id: str
    display_name: str
    description: str
    actions: tuple[tuple[str, str], ...]
    sprite: bytes
    preview: bytes | None
    preview_media_type: str | None

    def manifest(self) -> dict[str, object]:
        return {'id': self.package_id, 'format': FORMAT, 'displayName': self.display_name,
                'description': self.description, 'actions': dict(self.actions), 'spritesheetPath': 'spritesheet.webp'}


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or len(value) > 512 or any(char in value for char in ('\\', ':', '\x00'))
            or path.is_absolute() or '..' in path.parts or path == PurePosixPath('.')):
        raise ValueError('桌宠包路径不安全')
    return path.as_posix()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('桌宠 manifest 包含重复字段')
        result[key] = value
    return result


def _read(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, limit: int) -> bytes:
    if entry.file_size > limit:
        raise ValueError('桌宠包文件超过容量限制')
    with archive.open(entry) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('桌宠包文件超过容量限制')
    return data


def _image(data: bytes, *, atlas: bool) -> str:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if atlas:
                if image.format != 'WEBP' or image.size != (1536, 1872):
                    raise ValueError('桌宠精灵图必须是 1536 × 1872 WebP')
            elif image.format not in {'PNG', 'WEBP'} or not all(64 <= edge <= 2048 for edge in image.size):
                raise ValueError('桌宠预览图须为64–2048像素的 PNG/WebP')
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('桌宠包图片须为单帧 atlas 或预览图')
            image.load()
            media_type = 'image/webp' if image.format == 'WEBP' else 'image/png'
            if atlas:
                alpha = image.convert('RGBA').getchannel('A')
                for row, used in enumerate(USED_CELLS):
                    for column in range(8):
                        occupied = alpha.crop((column * 192, row * 208,
                            (column + 1) * 192, (row + 1) * 208)).getbbox() is not None
                        if occupied != (column < used):
                            raise ValueError('精灵图必需帧须有内容，未使用单元须透明')
            return media_type
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError('桌宠包图片无效') from error


def decode_package(raw: bytes) -> PackageData:
    if not raw or len(raw) > MAX_ZIP_BYTES:
        raise ValueError('桌宠 ZIP 最大32MiB')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise ValueError('桌宠 ZIP 成员或展开容量超额')
            files: dict[str, zipfile.ZipInfo] = {}
            for entry in entries:
                if entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError('桌宠包不接受加密或链接成员')
                name = _relative(entry.filename.rstrip('/'))
                if entry.is_dir():
                    continue
                if name in files:
                    raise ValueError('桌宠包包含重复路径')
                files[name] = entry
            prefix = ''
            if 'pet.json' not in files:
                roots = {PurePosixPath(name).parts[0] for name in files if len(PurePosixPath(name).parts) > 1}
                if len(roots) != 1:
                    raise ValueError('桌宠包缺少 pet.json')
                prefix = next(iter(roots)) + '/'
            manifest_entry = files.get(prefix + 'pet.json')
            if manifest_entry is None:
                raise ValueError('桌宠包缺少 pet.json')
            value = json.loads(_read(archive, manifest_entry, 65536).decode('utf-8'), object_pairs_hook=_unique)
            if not isinstance(value, dict):
                raise ValueError('桌宠 manifest 必须为对象')
            if value.get('format', FORMAT) != FORMAT:
                raise ValueError('桌宠包格式须为 codex-sprite@1')
            fields = {}
            for name, limit in (('id', 128), ('displayName', 256), ('description', 4096), ('spritesheetPath', 512)):
                item = value.get(name)
                if not isinstance(item, str) or not item.strip() or len(item) > limit:
                    raise ValueError('桌宠 manifest 字段缺失或超长')
                try:
                    item.encode('utf-8')
                except UnicodeEncodeError as error:
                    raise ValueError('桌宠 manifest 字符无效') from error
                fields[name] = item.strip()
            package_id = fields['id']
            if _relative(package_id) != package_id or '/' in package_id:
                raise ValueError('桌宠包 id 不安全')
            raw_actions = value.get('actions', {})
            if raw_actions is None:
                raw_actions = {}
            if not isinstance(raw_actions, dict) or len(raw_actions) > 32:
                raise ValueError('桌宠包动作必须为至多32项映射')
            actions = []
            for name, state in raw_actions.items():
                if (not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name)
                        or not isinstance(state, str) or state not in ACTION_STATES):
                    raise ValueError('桌宠包动作名称或状态无效')
                actions.append((name, state))
            sprite_entry = files.get(prefix + _relative(fields['spritesheetPath']))
            if sprite_entry is None:
                raise ValueError('桌宠包缺少精灵图')
            sprite = _read(archive, sprite_entry, MAX_ZIP_BYTES)
            _image(sprite, atlas=True)
            preview = None
            preview_media_type = None
            if value.get('previewPath') is not None:
                preview_path = value['previewPath']
                if not isinstance(preview_path, str):
                    raise ValueError('桌宠预览图路径无效')
                preview_entry = files.get(prefix + _relative(preview_path))
                if preview_entry is None:
                    raise ValueError('桌宠包缺少预览图')
                preview = _read(archive, preview_entry, MAX_ZIP_BYTES)
                preview_media_type = _image(preview, atlas=False)
            return PackageData(package_id, fields['displayName'], fields['description'],
                               tuple(sorted(actions)), sprite, preview, preview_media_type)
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, NotImplementedError, RuntimeError) as error:
        raise ValueError('桌宠 ZIP 或 manifest 无效') from error
