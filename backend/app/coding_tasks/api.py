"""Local owner settings and task inspection; never a model launch surface."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Annotated, Literal, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from .errors import CodingFailure
from .tools import _owned_thread

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


class CommitReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['commit']
    message: str = Field(min_length=1, max_length=8000)
    authorName: str = Field(min_length=1, max_length=100)
    authorEmail: str = Field(min_length=3, max_length=254)


class PublicationReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['publish']
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=32768)
    previousOperationId: str | None = Field(default=None, min_length=1, max_length=100)


class OwnerApprovalInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    approved: Literal[True]
    requestId: str = Field(min_length=1, max_length=100)

    @field_validator('approved', mode='before')
    @classmethod
    def explicit_boolean(cls, value):
        if value is not True:
            raise ValueError('literal owner approval is required')
        return value


class PublicationCredentialInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    token: str = Field(max_length=4096)


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

    def existing_role(role_id):
        if roles().get(role_id) is None:
            raise HTTPException(404, '角色不存在')

    async def owner_call(function, *args, **kwargs):
        try:
            return await _owned_thread(function, *args, **kwargs)
        except KeyError:
            raise HTTPException(404, '编程任务、审阅或操作不存在') from None
        except PermissionError:
            raise HTTPException(403, '原来源、操作许可或明确的凭据修订不可用') from None
        except CodingFailure:
            raise HTTPException(409, '宿主成果证据不完整或已失效；未提交或自动重新运行 CLI') from None
        except ValueError:
            raise HTTPException(409, '成果、预算或授权已变化，请重新审阅；未自动重试') from None
        except TimeoutError:
            raise HTTPException(409, '原任务的提交预算已结束；成果保留，需要主人明确发起新的继续请求，可能再次消耗 CLI 配额') from None
        except (sqlite3.Error, RuntimeError, OSError):
            raise HTTPException(503, '操作或资源收尾暂不可确认，请查询原操作') from None

    @router.get('/publication')
    async def publication_settings():
        return application().publication.settings()

    @router.put('/publication/credential')
    async def publication_credential(request: Request):
        try:
            value = PublicationCredentialInput.model_validate(await bounded_body(request))
        except ValueError:
            raise HTTPException(422, '发布凭据请求无效') from None
        return await owner_call(application().publication.set_token, value.token)

    @router.get('/roles/{role_id}/operations')
    async def owner_operations(role_id: str):
        existing_role(role_id)
        current = application()
        await current.tick()
        return {'operations': current.publication.list(role_id)}

    @router.post('/roles/{role_id}/tasks/{task_id}/reviews')
    async def prepare_review(role_id: str, task_id: str, request: Request):
        existing_role(role_id)
        try:
            value: CommitReviewInput | PublicationReviewInput = TypeAdapter(Annotated[Union[CommitReviewInput, PublicationReviewInput],
                Field(discriminator='action')]).validate_python(await bounded_body(request))
        except ValueError:
            raise HTTPException(422, '请提交明确的本地提交或 Draft PR 审阅内容') from None
        arguments = ({'message': value.message, 'author_name': value.authorName, 'author_email': value.authorEmail}
            if isinstance(value, CommitReviewInput) else {'title': value.title, 'body': value.body,
                'previous_operation_id': value.previousOperationId})
        return await owner_call(application().publication.prepare, role_id, task_id, action=value.action, **arguments)

    @router.post('/roles/{role_id}/reviews/{review_id}/approve', status_code=202)
    async def approve_review(role_id: str, review_id: str, request: Request):
        existing_role(role_id)
        try:
            value = OwnerApprovalInput.model_validate(await bounded_body(request))
        except ValueError:
            raise HTTPException(422, '须明确批准本次已审阅的确切成果') from None
        return await owner_call(application().publication.approve, role_id, review_id, request_id=value.requestId)

    @router.post('/roles/{role_id}/operations/{operation_id}/cancel', status_code=202)
    async def cancel_operation(role_id: str, operation_id: str):
        existing_role(role_id)
        return await owner_call(application().publication.cancel, role_id, operation_id)

    @router.post('/roles/{role_id}/operations/{operation_id}/observe')
    async def observe_operation(role_id: str, operation_id: str):
        existing_role(role_id)
        return await owner_call(application().publication.observe, role_id, operation_id)

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
        return {'tasks': [public_task(record) | {'ownerActions': current.publication.task_summary(record)} for record in reversed(records)],
                'availability': current.config.availability(role_id, current.role_config(role_id))}

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
