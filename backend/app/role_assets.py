"""Local-owner role illustration and chat-background actions.

Behavior reference: Shiori d3acaed1 core/roles/store.py and assets.py.
No arbitrary file paths or another role's attachment grants are accepted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .chat_attachments import resolve_attachment
from .models import RoleResponse


class AssetChanges(BaseModel):
    model_config = ConfigDict(extra='forbid')
    categories: list[str] | None = None
    bindings: dict[str, str] | None = None
    categorySend: dict[str, StrictBool] | None = None
    moodBindings: dict[str, str] | None = None
    defaultMood: str | None = Field(default=None, min_length=1, max_length=40)


class BackgroundSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    illustrationId: str | None


class AttachmentImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attachmentId: str = Field(pattern=r'^[0-9a-f]{32}$')
    setBackground: StrictBool = False
    category: str | None = None


def role_assets_router(get_store: Callable):
    router = APIRouter()

    def action(callback):
        try:
            return RoleResponse(role=callback())
        except KeyError:
            raise HTTPException(404, '角色或插画不存在') from None
        except (ValueError, PermissionError) as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(503, '插画操作尚未确认，请刷新角色素材后核对，不要自动重复提交') from None

    @router.get('/api/roles/{role_id}/illustrations', response_model=RoleResponse)
    def list_illustrations(role_id: str):
        role = get_store().get(role_id)
        if role is None:
            raise HTTPException(404, '角色不存在')
        return RoleResponse(role=role)

    @router.post('/api/roles/{role_id}/illustrations', response_model=RoleResponse)
    async def upload_illustration(role_id: str, request: Request, name: str = '插画', category: str | None = None):
        store = get_store()
        if store.get(role_id) is None:
            raise HTTPException(404, '角色不存在')
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > 10 * 1024 * 1024:
                raise HTTPException(413, '插画不能超过 10 MiB')
            content.extend(chunk)
        media_type = request.headers.get('content-type', '').split(';', 1)[0].lower().strip()
        return action(lambda: store.add_illustration(role_id, bytes(content), media_type, name, category=category))

    @router.post('/api/roles/{role_id}/illustrations/from-attachment', response_model=RoleResponse)
    def import_attachment(role_id: str, body: AttachmentImport):
        store = get_store()
        def apply():
            workspace = store.workspace_path(role_id)
            image = resolve_attachment(workspace, body.attachmentId)
            if image is None or image.get('kind') != 'image' or not isinstance(image.get('path'), str):
                raise KeyError('当前角色图片不存在')
            path = Path(image['path'])
            if not path.resolve().is_relative_to(workspace.resolve()):
                raise PermissionError('当前角色图片路径无效')
            with path.open('rb') as handle:
                content = handle.read(10 * 1024 * 1024 + 1)
            return store.add_illustration(role_id, content, image['mediaType'], image['name'],
                category=body.category, set_background=body.setBackground)
        return action(apply)

    @router.patch('/api/roles/{role_id}/illustrations', response_model=RoleResponse)
    def update_illustrations(role_id: str, body: AssetChanges):
        return action(lambda: get_store().update_illustrations(role_id, categories=body.categories, bindings=body.bindings,
            category_send=body.categorySend, mood_bindings=body.moodBindings, default_mood=body.defaultMood))

    @router.put('/api/roles/{role_id}/chat-background', response_model=RoleResponse)
    def select_background(role_id: str, body: BackgroundSelection):
        return action(lambda: get_store().set_chat_background(role_id, body.illustrationId))

    @router.put('/api/roles/{role_id}/avatar-from-illustration', response_model=RoleResponse)
    def select_avatar(role_id: str, body: BackgroundSelection):
        store = get_store()
        def apply():
            role = store.get(role_id)
            item = next((item for item in role.illustrations if item.id == body.illustrationId), None) if role else None
            if item is None:
                raise KeyError('角色插画不存在')
            with store.illustration_path(role_id, item.id).open('rb') as handle:
                content = handle.read(10 * 1024 * 1024 + 1)
            from .novelai_generation.media import validate_image
            validate_image(content, max_pixels=16 * 1024 * 1024)
            if len(content) > 10 * 1024 * 1024:
                raise ValueError('插画不能超过 10 MiB')
            return store.set_avatar(role_id, content, item.mediaType)
        return action(apply)

    @router.get('/api/roles/{role_id}/illustrations/{identifier}')
    def get_illustration(role_id: str, identifier: str):
        store = get_store()
        role = store.get(role_id)
        item = next((item for item in role.illustrations if item.id == identifier), None) if role else None
        if item is None:
            raise HTTPException(404, '角色插画不存在')
        try:
            content = store.illustration_path(role_id, identifier).read_bytes()
        except (KeyError, ValueError, OSError):
            raise HTTPException(404, '角色插画文件不存在') from None
        return Response(content, media_type=item.mediaType, headers={'Cache-Control': 'private, max-age=3600'})

    @router.delete('/api/roles/{role_id}/illustrations/{identifier}', response_model=RoleResponse)
    def remove_illustration(role_id: str, identifier: str):
        return action(lambda: get_store().remove_illustration(role_id, identifier))

    return router
