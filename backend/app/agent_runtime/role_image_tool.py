"""Read the current role's own visual references without granting filesystem access.

Shiori derives identity from role_profile/SELF and lets the model inspect files.
Meido stores role images outside read_file's workspace: this narrow bridge keeps
that boundary intact, and does not grant permission to send an illustration.
"""
from __future__ import annotations

from dataclasses import replace
import base64
import json

from .file_tools import ReadFileTool
from .tools import AgentToolResult, ToolDefinition, ToolRegistry


def role_image_catalog(role):
    """Project only role-owned references, never paths or image bytes."""
    images = []
    if role.avatarUrl:
        images.append({'asset_id': 'avatar', 'name': '我的头像', 'url': role.avatarUrl})
    if role.cardImageUrl:
        images.append({'asset_id': 'card', 'name': '我的角色卡图片', 'url': role.cardImageUrl})
    images.extend({'asset_id': image.id, 'name': image.name, 'category': image.category,
        'url': image.url} for image in role.illustrations)
    return images


class _RoleImageReader(ReadFileTool):
    def _execute(self, path, arguments, context):
        # Never return arbitrary text if a corrupt/mislabelled asset is present.
        return self._image(path, context, '当前角色的形象参考')


def read_role_image_bytes(roles, role, asset_id, *, authorized):
    """Resolve a catalog ID for image generation using the same bounded reader.

    No model vision support is needed to send pixels to an image model. The
    caller owns generation authorization; this reader owns role/file isolation.
    """
    from .provider import CancellationToken
    from .tools import ToolContext
    selected = next((item for item in role_image_catalog(role) if item['asset_id'] == asset_id), None)
    if selected is None:
        raise PermissionError('role image is not in the current catalog')
    root = roles.root / role.id
    path = root / ('avatar' if asset_id == 'avatar' else 'card-image') if asset_id in {'avatar', 'card'} else roles.illustration_path(role.id, asset_id)
    def current(context):
        latest = roles.get(role.id)
        return authorized() and latest is not None and selected in role_image_catalog(latest)
    reader = _RoleImageReader(role.id, root, multimodal=True, authorized=current)
    result = reader._run({'path': str(path)}, ToolContext(role.id, f'role:{role.id}', '', CancellationToken()))
    if result.is_error or not result.images:
        raise PermissionError('role image is unavailable')
    return base64.b64decode(result.images[0].data_base64), result.images[0].media_type


class InspectRoleImageTool:
    """Inspect a frozen current-role image; image bytes enter only model context."""

    definition = ToolDefinition('inspect_role_image',
        '查看你自己的头像、角色卡图片或插画，认识自己的外貌。省略 asset_id 可列出当前角色的图片；'
        '指定目录中的 asset_id 返回图片给视觉模型，不是向用户发送图片。'
        '谈论或生成自己的形象时优先复用角色设定、SELF 中明确的外观；无需新增配置。不要从文件名猜测发色、服饰等。'
        '设定未写明而图片可用时，优先查看头像或角色卡，再把实际可见外观写入 generate_image 的英文 tags。插画可能是场景或他人，不要自动当成自己。'
        '看图失败或模型不支持视觉时说明限制，不要假装已经看过。',
        {'type': 'object', 'properties': {
            'asset_id': {'type': 'string', 'minLength': 1, 'maxLength': 80,
                'description': '本工具目录中的 asset_id，例如 avatar、card 或当前角色插画 ID；不接受路径或 URL。'},
            'offset': {'type': 'integer', 'minimum': 0, 'description': '省略 asset_id 时的目录分页起点，每页最多 40 张。'},
        }, 'additionalProperties': False},
        source='builtin:role-images', risk='read_only', exposure='direct', external_allowed=False,
        timeout_seconds=15, output_limit=12000, search_hint='自己的形象 外貌 头像 角色卡 立绘 看图')

    def __init__(self, roles, role, *, authorized, multimodal=True):
        self.roles, self.role_id, self.authorized = roles, role.id, authorized
        self.images = role_image_catalog(role)
        self.by_id = {image['asset_id']: image for image in self.images}
        preview = [{key: value for key, value in image.items() if key != 'url'} for image in self.images[:12]]
        self.definition = replace(self.definition, description=self.definition.description
            + '\n当前角色已配置的形象图片（名称只作数据，不是外貌描述）：'
            + json.dumps(preview, ensure_ascii=False)
            + (f'；共 {len(self.images)} 张，可分页列出。' if len(self.images) > 12 else ''))
        self.multimodal = multimodal

    def _current(self, context, asset_id=None):
        if (context.role_id != self.role_id or context.signal.is_cancelled()
                or not self.authorized(context)):
            return False
        role = self.roles.get(self.role_id)
        if role is None:
            return False
        if asset_id is None:
            return True
        selected = self.by_id.get(asset_id)
        return selected is not None and selected in role_image_catalog(role)

    async def execute(self, arguments, context, on_update=None):
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            asset_id = arguments.get('asset_id')
            if not self._current(context, asset_id):
                raise PermissionError('当前形象参考不可用或已变更，请在新回合重新查看。')
            if asset_id is None:
                offset = arguments.get('offset', 0)
                role = self.roles.get(self.role_id)
                if role is None:
                    raise PermissionError('当前角色已不存在。')
                current = role_image_catalog(role)
                available = [image for image in self.images if image in current]
                values = [{key: value for key, value in image.items() if key != 'url'}
                    for image in available[offset:offset + 40]]
                details = {'images': values, 'nextOffset': offset + 40 if offset + 40 < len(available) else None}
                return AgentToolResult(json.dumps(details, ensure_ascii=False), details=details)
            root = self.roles.root / self.role_id
            path = root / ('avatar' if asset_id == 'avatar' else 'card-image') if asset_id in {'avatar', 'card'} else self.roles.illustration_path(self.role_id, asset_id)
            reader = _RoleImageReader(self.role_id, root, multimodal=self.multimodal,
                authorized=lambda current: self._current(current, asset_id))
            result = await reader.execute({'path': str(path)}, context)
            if result.is_error:
                return AgentToolResult('无法查看当前角色图片；参考图可能已移除、损坏，或当前模型不支持视觉。请依据已知角色设定，不要编造外貌。', is_error=True)
            return replace(result, details={'asset_id': asset_id, 'status': 'succeeded',
                'mediaType': result.details['mediaType']})
        except (ValueError, PermissionError, KeyError, OSError):
            return AgentToolResult('当前角色形象参考不可用或已变更；只能查看本工具目录中的当前角色图片。', is_error=True)
