"""Bounded CCv3 exports from explicit role fields and current image slots."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import stat
import warnings
import zipfile

from PIL import Image, ImageOps, PngImagePlugin, UnidentifiedImageError

from . import role_card_import as limits


MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MEDIA = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp', 'GIF': 'image/gif'}
_FIELDS = ['名称', '简介', '角色设定', '性格', '行为规则', '回复约束', '称呼']
_SLOTS = (('avatar', '头像', 'avatarUrl', 'avatarMediaType'),
          ('card-image', '卡片图', 'cardImageUrl', 'cardImageMediaType'))


class RoleCardExportError(ValueError):
    pass


@dataclass(frozen=True)
class RoleCardExport:
    content: bytes
    filename: str
    media_type: str
    summary: dict


def _card(role):
    profile = role.profile
    return {'spec': 'chara_card_v3', 'spec_version': '3.0', 'data': {
        'name': role.name, 'description': profile.profile, 'personality': profile.personality,
        'system_prompt': profile.behaviorRules, 'post_history_instructions': profile.responseConstraints,
        'extensions': {'meido': {'description': role.description, 'nickname': profile.nickname}},
        'tags': [], 'assets': [], 'creator': '', 'character_version': '', 'mes_example': '', 'scenario': '',
        'creator_notes': '', 'first_mes': '', 'alternate_greetings': [], 'group_only_greetings': [],
    }}


def _json(card):
    data = json.dumps(card, ensure_ascii=False, indent=2).encode('utf-8')
    if len(data) > limits.MAX_JSON_BYTES:
        raise RoleCardExportError('角色设定超过角色卡大小限制，请精简后再导出')
    return data


def _plain_stat(path: Path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise RoleCardExportError('角色图片路径不安全，请重新保存图片或改用 JSON')
    if not directory and info.st_nlink != 1:
        raise RoleCardExportError('角色图片不能使用文件链接，请重新保存图片或改用 JSON')
    return info


def _read_image(root: Path, role_id: str, slot: str) -> bytes:
    """Check the opened file identity before reading any bounded image bytes."""
    if not role_id or role_id in {'.', '..'} or any(char in role_id for char in '/\\:'):
        raise RoleCardExportError('角色图片路径无效')
    directory = root / role_id
    path = directory / slot
    try:
        _plain_stat(root, directory=True)
        _plain_stat(directory, directory=True)
        before = _plain_stat(path)
        if root.resolve() != root or directory.resolve().parent != root or path.resolve().parent != directory:
            raise RoleCardExportError('角色图片路径不安全，请重新保存图片或改用 JSON')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as source:
            opened = os.fstat(source.fileno())
            if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
                raise RoleCardExportError('角色图片已改变，请重试导出')
            if opened.st_size > MAX_IMAGE_BYTES:
                raise RoleCardExportError('角色图片超过 10 MB，请缩小图片或改用 JSON')
            raw = source.read(MAX_IMAGE_BYTES + 1)
            after = _plain_stat(path)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise RoleCardExportError('角色图片已改变，请重试导出')
        if path.resolve().parent != directory or len(raw) > MAX_IMAGE_BYTES:
            raise RoleCardExportError('角色图片已改变或超过大小限制，请重试')
        return raw
    except OSError:
        raise RoleCardExportError('角色图片无法读取，请重新保存图片或改用 JSON') from None


def _clean_image(raw: bytes, declared_type: str) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in _MEDIA or declared_type != _MEDIA[source.format]:
                    raise RoleCardExportError('角色图片类型不匹配，请重新保存图片或改用 JSON')
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise RoleCardExportError('角色图片像素过大，请缩小图片或改用 JSON')
                source.seek(0)
                with ImageOps.exif_transpose(source) as oriented, oriented.convert('RGBA') as converted:
                    # A fresh image drops EXIF and old character-card chunks;
                    # even convert/copy can otherwise retain source metadata.
                    with Image.frombytes('RGBA', converted.size, converted.tobytes()) as clean:
                        output = io.BytesIO()
                        clean.save(output, format='PNG')
                        result = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        if isinstance(error, RoleCardExportError):
            raise
        raise RoleCardExportError('角色图片无效或像素过大，请重新保存图片或改用 JSON') from None
    if len(result) > limits.MAX_ASSET_PREVIEW_BYTES:
        raise RoleCardExportError('转换后的图片超过 10 MB，请缩小图片或改用 JSON')
    return result


def _source_token(role):
    return _card(role), tuple(getattr(role, key) for _, _, url, media in _SLOTS for key in (url, media))


def export_role_card(store, role_id: str, format: str) -> RoleCardExport:
    if format not in {'json', 'png', 'charx'}:
        raise RoleCardExportError('请选择 JSON、PNG 或 CHARX 格式')
    role = store.get(role_id)
    if role is None:
        raise KeyError(role_id)
    token = _source_token(role)
    card = _card(role)
    available = [item for item in _SLOTS if getattr(role, item[2])]
    summary = {'format': format, 'fields': list(_FIELDS), 'includedAssets': [], 'omittedAssets': [],
               'notes': ['简介和用户称呼保存在 Meido 扩展中；其他客户端可能忽略这两项。',
                         '不含模型配置、权限、会话、记忆、工作区或裁剪前原图。']}
    images = {}
    selected = available if format == 'charx' else available[-1:] if format == 'png' else []
    for slot, label, _, media in selected:
        images[slot] = _clean_image(_read_image(store.root, role.id, slot), getattr(role, media))
        summary['includedAssets'].append(label)
    summary['omittedAssets'] = [label for slot, label, _, _ in available if slot not in images]
    if images:
        summary['notes'].append('图片以首帧 PNG 导出，原图附加信息已移除。')
    if format == 'json':
        summary['notes'].append('JSON 仅包含设定，不包含图片。')
        content, media_type = _json(card), 'application/json'
    elif format == 'png':
        cover = next(iter(images.values()), None)
        if cover is None:
            output = io.BytesIO()
            Image.new('RGBA', (1, 1), (0, 0, 0, 0)).save(output, format='PNG')
            cover = output.getvalue()
            summary['notes'].append('没有头像或卡片图，使用透明占位封面。')
        summary['notes'].append('PNG 只有一个封面；重新导入时可选择将封面用作头像。')
        card['data']['assets'] = [{'type': 'icon', 'uri': 'ccdefault:', 'name': 'main', 'ext': 'png'}]
        encoded = base64.b64encode(_json(card))
        if len(encoded) > limits.MAX_METADATA_BYTES:
            raise RoleCardExportError('角色设定超过 PNG 元数据限制，请精简设定或改用 JSON')
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('ccv3', encoded.decode('ascii'))
        output = io.BytesIO()
        with Image.open(io.BytesIO(cover)) as image:
            image.save(output, format='PNG', pnginfo=metadata)
        content, media_type = output.getvalue(), 'image/png'
        if len(content) > limits.MAX_ASSET_PREVIEW_BYTES:
            raise RoleCardExportError('PNG 角色卡超过预览素材限制，请缩小图片或改用 CHARX')
    else:
        assets = []
        members = {}
        for slot, data in images.items():
            name = f'assets/icon/images/{slot}.png'
            assets.append({'type': 'icon', 'uri': f'embeded://{name}',
                           'name': 'main' if slot == 'avatar' else 'card-image', 'ext': 'png'})
            members[name] = data
        if sum(map(len, members.values())) > limits.MAX_ASSET_PREVIEW_TOTAL_BYTES:
            raise RoleCardExportError('图片总大小超过导入预览限制，请缩小图片后导出')
        if assets:
            card['data']['assets'] = assets
            summary['notes'].append('CHARX 图片可在导入预览中分别选为头像或卡片图。')
        members['card.json'] = _json(card)
        if (len(members) > limits.MAX_CHARX_MEMBERS
                or any(len(data) > limits.MAX_CHARX_MEMBER_BYTES for data in members.values())
                or sum(map(len, members.values())) > limits.MAX_CHARX_UNCOMPRESSED_BYTES):
            raise RoleCardExportError('CHARX 内容超过导入限制，请精简设定或图片')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        content, media_type = output.getvalue(), 'application/zip'
    if len(content) > limits.MAX_SOURCE_BYTES:
        raise RoleCardExportError('导出文件超过角色卡导入大小限制，请精简后重试')
    current = store.get(role_id)
    if current is None or _source_token(current) != token:
        raise RoleCardExportError('角色资料已改变，请重试导出')
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', role.name).strip(' .')[:80] or 'role-card'
    return RoleCardExport(content, f'{name}.{format}', media_type, summary)
