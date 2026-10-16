"""Trusted local owner actions; no model tool accepts these request identities."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..channels.consolidation import finish_io
from ..task_runtime import TaskOrigin
from ..session_store import AttachmentConflict
from .context import GenerationContext


async def _body(request: Request, *, allow_empty=False):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 64 * 1024:
            raise HTTPException(413, '生图设置或请求超过大小限制')
    try:
        if not content and allow_empty:
            return {}
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, RecursionError):
        raise HTTPException(422, '生图请求必须为有效 JSON 对象') from None


async def _run_owner_action(request, application, source, action):
    """Join foreground cancellation; accepted background work belongs to Runtime."""
    operation = asyncio.create_task(action)
    finished = asyncio.Event()

    def cancel_unaccepted():
        accepted = False
        if operation.done() and not operation.cancelled():
            try:
                accepted = operation.result().get('backgroundAccepted', False)
            except Exception:
                pass  # Failed foreground/handover operations still require seal.
        if not accepted:
            try:
                application.seal_request(source)
            finally:
                operation.cancel()

    async def watch_disconnect():
        while not finished.is_set():
            await asyncio.sleep(.05)
            if await request.is_disconnected():
                cancel_unaccepted()
                return
    watcher = asyncio.create_task(watch_disconnect())
    try:
        result = await operation
        if result['backgroundAccepted']:
            return JSONResponse({'generation': result}, status_code=202)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if await request.is_disconnected():
            cancel_unaccepted()
            raise asyncio.CancelledError()
        application.allow_ui_publication(source, result['attemptId'])
        if result['status'] == 'succeeded':
            if result['deliveryMode'] == 'replacement':
                await application.attach_replacement(source, result['attemptId'])
            elif result['deliveryMode'] == 'source_completion':
                await application.attach_ui_completion(source, result['attemptId'])
            else:
                application.keep_history_only(result['attemptId'])
        return {'generation': application.owner_generation(source.origin.role_id,
            result['generationId'], attempt_id=result['attemptId'])}
    except asyncio.CancelledError:
        cancel_unaccepted()
        raise
    except PermissionError:
        raise HTTPException(403, '当前来源、所选图片或有限生图许可不可用，请检查归属历史') from None
    except AttachmentConflict:
        raise HTTPException(409, '原消息中的图片已改变；尚未新生成。请重新选图或明确选择仅保存历史') from None
    except ValueError:
        raise HTTPException(422, '生图参数、请求或版本冲突；同一输入不会自动再次消费') from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, '本地步骤尚未完成，请保留原请求 ID 查看历史，不要自动新增消费') from None
    finally:
        finished.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if not operation.done():
            async def join_operation():
                await asyncio.gather(operation, return_exceptions=True)
            try:
                cancel_unaccepted()
            finally:
                await finish_io(join_operation())


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

    @router.get('/api/roles/{role_id}/generations/attachment/{attachment_id}')
    async def attachment_generation(role_id: str, attachment_id: str, messageId: str, application=Depends(current)):
        try:
            return {'generation': application.attachment_generation(role_id, attachment_id, messageId)}
        except KeyError:
            raise HTTPException(404, '当前消息中的角色归属图片不存在') from None

    @router.get('/api/roles/{role_id}/generations/{generation_id}')
    async def generation(role_id: str, generation_id: str, attemptId: str | None = None, application=Depends(current)):
        try:
            return {'generation': application.owner_generation(role_id, generation_id, attempt_id=attemptId)}
        except KeyError:
            raise HTTPException(404, '角色归属的生成记录不存在') from None

    @router.post('/api/roles/{role_id}/generations')
    async def generate(role_id: str, request: Request, application=Depends(current)):
        data = await _body(request)
        if (set(data) - {'deliveryMode'} != {'requestId', 'expectedRevision', 'parameters'}
                or data.get('deliveryMode') not in (None, 'history_only')
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
        return await _run_owner_action(request, application, source,
            application.submit(source, data['parameters'], expected_revision=data['expectedRevision'],
                publication_ready=False, delivery_mode=data.get('deliveryMode')))

    @router.post('/api/roles/{role_id}/generations/{generation_id}/regenerate')
    async def regenerate(role_id: str, generation_id: str, request: Request, application=Depends(current)):
        data = await _body(request)
        if (set(data) - {'deliveryMode'} != {'requestId', 'expectedRevision', 'sourceAttemptId', 'expectedAttachmentId', 'parameters'}
                or data.get('deliveryMode') not in (None, 'replacement', 'history_only')
                or not isinstance(data['requestId'], str) or not re.fullmatch(r'[0-9a-f]{32}|[0-9a-f-]{36}', data['requestId'])
                or type(data['expectedRevision']) is not int or not isinstance(data['parameters'], dict)
                or any(not isinstance(data[key], str) or not re.fullmatch(r'[0-9a-f]{32}', data[key])
                    for key in ('sourceAttemptId', 'expectedAttachmentId'))):
            raise HTTPException(422, '重生成需要新的明确请求、当前设置版本和选中的原图片')
        try:
            source = application.regeneration_source(role_id, generation_id,
                source_attempt_id=data['sourceAttemptId'], request_id=data['requestId'], parameters=data['parameters'])
        except KeyError:
            raise HTTPException(404, '角色归属的原图片记录不存在') from None
        except PermissionError:
            raise HTTPException(403, '原来源当前不允许重生成或自动交付') from None
        return await _run_owner_action(request, application, source,
            application.regenerate(source, generation_id, source_attempt_id=data['sourceAttemptId'],
                expected_attachment_id=data['expectedAttachmentId'], arguments=data['parameters'],
                expected_revision=data['expectedRevision'], publication_ready=False,
                delivery_mode=data.get('deliveryMode')))

    @router.post('/api/roles/{role_id}/generations/{generation_id}/cancel')
    async def cancel(role_id: str, generation_id: str, request: Request, application=Depends(current)):
        data = await _body(request, allow_empty=True)
        if set(data) - {'attemptId'} or ('attemptId' in data and (not isinstance(data['attemptId'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', data['attemptId']))):
            raise HTTPException(422, '停止操作需要有效的原尝试 ID')
        try:
            return {'generation': await application.cancel(role_id, generation_id, attempt_id=data.get('attemptId'))}
        except KeyError:
            raise HTTPException(404, '角色归属的生成记录不存在') from None

    return router
