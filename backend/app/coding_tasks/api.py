"""Local owner settings and task inspection; never a model launch surface."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .application import public_task
from .config import ProfileInput, RepositoryInput


class EnabledInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool


class BuildInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    executable: str
    sha256: str


class ProbeInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: str
    roleId: str
    buildSha256: str
    approveNativeTurns: int
    requestId: str


async def bounded_body(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 128 * 1024:
            raise HTTPException(413, 'Coding 配置超过大小限制')
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError:
        raise HTTPException(422, 'Coding 配置必须为 JSON 对象') from None


def coding_router(application, roles) -> APIRouter:
    router = APIRouter(prefix='/api/coding')

    @router.get('/configuration')
    async def configuration():
        return application().config.public()

    @router.put('/configuration')
    async def enabled(request: Request):
        try:
            data = EnabledInput.model_validate(await bounded_body(request))
            current = application()
            current.config.set_enabled(data.enabled)
            if not data.enabled:
                for role in roles().list():
                    await current.invalidate_role(role.id)
            return current.config.public()
        except ValueError:
            raise HTTPException(422, 'Coding 开关配置无效') from None

    async def save(request, kind):
        try:
            data = (RepositoryInput if kind == 'repository' else ProfileInput).model_validate(await bounded_body(request))
            if any(roles().get(role_id) is None for role_id in data.allowedRoleIds):
                raise ValueError('role does not exist')
            current = application()
            method = current.config.save_repository if kind == 'repository' else current.config.save_profile
            value = await asyncio.to_thread(method, data.model_dump())
            # Revision changes revoke current attempts before this response.
            for role in roles().list():
                await current.invalidate_role(role.id)
            return value
        except (ValueError, OSError, KeyError):
            raise HTTPException(422, '固定路径、哈希、角色许可或配置无效；未读取桌面认证') from None

    @router.post('/repositories')
    async def repository(request: Request):
        return await save(request, 'repository')

    @router.post('/profiles')
    async def profile(request: Request):
        return await save(request, 'profile')

    @router.put('/native-build')
    async def native_build(request: Request):
        try:
            data = BuildInput.model_validate(await bounded_body(request))
            current = application()
            value = await asyncio.to_thread(current.config.set_native_build, data.executable, data.sha256)
            for role in roles().list():
                await current.invalidate_role(role.id)
            return value
        except (ValueError, OSError):
            raise HTTPException(422, 'owned Herdr 构建或发行资源核验失败') from None

    @router.get('/roles/{role_id}/tasks')
    async def tasks(role_id: str):
        role = roles().get(role_id)
        if role is None:
            raise HTTPException(404, '角色不存在')
        current = application()
        await current.tick()
        records = current.ledger.role_records(role_id)
        # This is the trusted local owner UI. Model tools keep the narrower
        # original-session scope and cannot use this API to inherit sources.
        return {'tasks': [public_task(record) for record in reversed(records)],
                'availability': current.config.availability(role_id, role.agentConfig.coding)}

    @router.get('/probes')
    async def probes():
        current = application()
        await asyncio.to_thread(current.probes.tick)
        return {'probes': current.probes.public()}

    @router.post('/profiles/{profile_id}/probe')
    async def start_probe(profile_id: str, request: Request):
        try:
            data = ProbeInput.model_validate(await bounded_body(request))
            if roles().get(data.roleId) is None:
                raise ValueError('probe role does not exist')
            return await asyncio.to_thread(application().probes.start, profile_id, revision=data.revision,
                role_id=data.roleId, build_sha256=data.buildSha256,
                approve_native_turns=data.approveNativeTurns, request_id=data.requestId)
        except (ValueError, KeyError, PermissionError, OSError):
            raise HTTPException(422, '核验未启动：需要匹配的固定 CLI 版本、角色、宿主修订及首次和继续两个原生任务回合的明确许可') from None

    @router.post('/roles/{role_id}/tasks/{task_id}/cancel')
    async def cancel(role_id: str, task_id: str):
        if roles().get(role_id) is None:
            raise HTTPException(404, '角色不存在')
        current = application()
        records = [item for item in current.ledger.role_records(role_id) if item['id'] == task_id]
        if not records:
            raise HTTPException(404, 'Coding 任务不存在')
        try:
            return await asyncio.to_thread(current.cancel_owned_task, role_id, task_id)
        except (KeyError, PermissionError):
            raise HTTPException(409, '原来源许可已失效；任务正在撤销或已停止') from None

    return router
