"""Trusted local owner actions; no model tool accepts these request identities."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from ..channels.consolidation import finish_io
from ..task_runtime import TaskOrigin
from .context import GenerationContext


async def _body(request: Request):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 64 * 1024:
            raise HTTPException(413, '生图设置或请求超过大小限制')
    try:
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, RecursionError):
        raise HTTPException(422, '生图请求必须为有效 JSON 对象') from None


def novelai_router(application):
    router = APIRouter()
    def current():
        return application() if callable(application) else application

    @router.get('/api/services/novelai')
    async def settings(application=Depends(current)):
        return application.settings.public()

    @router.put('/api/services/novelai')
    async def save_settings(request: Request, application=Depends(current)):
        data = await _body(request)
        revision = data.pop('expectedRevision', None)
        try:
            result = application.settings.save(data, expected_revision=revision)
            await application.invalidate()
            return result
        except ValueError:
            raise HTTPException(422, 'NovelAI 设置或版本无效，请刷新后检查有限值和模型白名单') from None

    @router.put('/api/services/novelai/token')
    async def set_token(request: Request, application=Depends(current)):
        data = await _body(request)
        if set(data) != {'token'}:
            raise HTTPException(422, '请使用明确的独立凭据设置操作')
        try:
            result = await finish_io(asyncio.to_thread(application.settings.set_token, data['token']))
            return result
        except ValueError:
            raise HTTPException(422, 'NovelAI 凭据格式无效') from None
        except Exception:
            raise HTTPException(503, '独立凭据未保存或清理未完成，请检查设置状态后重试') from None
        finally:
            await application.invalidate()

    @router.delete('/api/services/novelai/token')
    async def clear_token(application=Depends(current)):
        try:
            return await finish_io(asyncio.to_thread(application.settings.clear_token))
        except Exception:
            raise HTTPException(503, '独立凭据清理尚未完成，请重试') from None
        finally:
            await application.invalidate()

    @router.get('/api/roles/{role_id}/generations')
    async def history(role_id: str, limit: int = 20, cursor: str | None = None, application=Depends(current)):
        try:
            return application.history(role_id, limit=limit, cursor=cursor)
        except KeyError:
            raise HTTPException(404, '角色不存在') from None
        except ValueError:
            raise HTTPException(422, '历史分页参数无效') from None

    @router.get('/api/roles/{role_id}/generations/{generation_id}')
    async def generation(role_id: str, generation_id: str, application=Depends(current)):
        try:
            return {'generation': application.owner_generation(role_id, generation_id)}
        except KeyError:
            raise HTTPException(404, '角色归属的生成记录不存在') from None

    @router.post('/api/roles/{role_id}/generations')
    async def generate(role_id: str, request: Request, application=Depends(current)):
        data = await _body(request)
        if (set(data) != {'requestId', 'expectedRevision', 'parameters'}
                or not isinstance(data['requestId'], str) or not re.fullmatch(r'[0-9a-f]{32}|[0-9a-f-]{36}', data['requestId'])
                or type(data['expectedRevision']) is not int or not isinstance(data['parameters'], dict)):
            raise HTTPException(422, '生成操作需要有效的请求 ID、设置版本与严格参数')
        if application.roles.get(role_id) is None:
            raise HTTPException(404, '角色不存在')
        # This route is the actual explicit owner action. The tool path has no
        # means to select uiAction/requestId or promote a previous human run.
        session = application.sessions.open_role_session(role_id)
        identifier = data['parameters'].get('base_attachment_id')
        source = GenerationContext(TaskOrigin(role_id, session.sessionKey, source_thread_id=f'desktop:{role_id}'),
            'ui:' + data['requestId'], 'ui:' + data['requestId'],
            frozenset([identifier]) if isinstance(identifier, str) else frozenset(), ui_action=True)
        operation = asyncio.create_task(application.submit(source, data['parameters'], expected_revision=data['expectedRevision']))

        async def watch_disconnect():
            while not operation.done():
                await asyncio.sleep(.05)
                if await request.is_disconnected():
                    try:
                        application.seal_request(source)
                    finally:
                        operation.cancel()
                    return
        watcher = asyncio.create_task(watch_disconnect())
        try:
            result = await operation
            if await request.is_disconnected():
                application.seal_request(source)
                raise asyncio.CancelledError()
            if result['status'] == 'succeeded':
                await application.attach_ui_completion(source, result['attemptId'])
            return {'generation': application.owner_generation(role_id, result['generationId'])}
        except asyncio.CancelledError:
            application.seal_request(source)
            raise
        except PermissionError:
            raise HTTPException(403, '当前来源或有限生图许可不可用，请检查设置与归属历史') from None
        except ValueError:
            raise HTTPException(422, '生图参数、请求版本或额度无效；同一输入不会自动再次消费') from None
        except (sqlite3.Error, OSError):
            raise HTTPException(503, '生图本地步骤尚未完成，请保留请求 ID 并查看历史，不要自动新建消费') from None
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if not operation.done():
                application.seal_request(source)
                operation.cancel()
                async def join_operation():
                    await asyncio.gather(operation, return_exceptions=True)
                await finish_io(join_operation())

    @router.post('/api/roles/{role_id}/generations/{generation_id}/cancel')
    async def cancel(role_id: str, generation_id: str, application=Depends(current)):
        try:
            return {'generation': await application.cancel(role_id, generation_id)}
        except KeyError:
            raise HTTPException(404, '角色归属的生成记录不存在') from None

    return router
