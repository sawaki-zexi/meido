"""Trusted settings API, separate from the model tool directory."""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from .plugin_configuration import PARAMETERS, PluginConflict

PLUGIN_METADATA = {
    'browser': ('浏览器', '访问网页和交互；会话分别归属角色。'),
    'screen': ('屏幕感知', '使用指定视觉连接观察主屏并返回摘要。'),
    'computer': ('桌面控制', '窗口截图发送到当前聊天模型，操作键盘和鼠标。'),
    'pet': ('桌宠', '在桌面显示角色，动作包按角色配置。'),
    'autonomy': ('主动推送', '主动与你交流，空闲时执行任务。'),
}


class EnabledInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: StrictBool
    expectedRevision: StrictInt


class ValuesInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    values: dict
    expectedRevision: StrictInt


async def bounded_body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16 * 1024:
            raise HTTPException(413, '插件设置超过大小限制')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, RecursionError):
        raise HTTPException(422, '插件设置必须为 JSON 对象') from None


def plugin_router(store, cleanup, activated, validate):
    router = APIRouter(prefix='/api/plugin-management')
    serial = asyncio.Lock()

    def status(identifier):
        if identifier not in PLUGIN_METADATA:
            raise HTTPException(404, '插件不存在')
        state = store().get(identifier)
        name, description = PLUGIN_METADATA[identifier]
        return {'id': identifier, 'name': name, 'description': description,
                **state.model_dump(), 'enabled': state.enabled and not state.cleanupPending,
                'schema': PARAMETERS[identifier].model_json_schema()}

    @router.get('')
    async def listing():
        return {'plugins': [status(identifier) for identifier in PLUGIN_METADATA]}

    @router.get('/{identifier}')
    async def read(identifier: str):
        return status(identifier)

    async def save(identifier, request, *, parameters):
        status(identifier)
        try:
            data = (ValuesInput if parameters else EnabledInput).model_validate(await bounded_body(request))
        except ValueError:
            raise HTTPException(422, '插件设置字段无效') from None
        async with serial:
            try:
                changes = {'values': data.values} if parameters else {'enabled': data.enabled}
                validate(identifier, changes)
                state = store().prepare(identifier, expected_revision=data.expectedRevision, **changes)
            except PluginConflict as error:
                raise HTTPException(409, str(error)) from None
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            try:
                await cleanup(identifier, state.values)
                store().finish(identifier, state.revision)
                await activated(identifier)
            except BaseException as error:
                store().hold(identifier, state.revision)
                if isinstance(error, (OSError, RuntimeError)):
                    raise HTTPException(503, '插件原资源仍待收尾，请刷新后重试关闭') from error
                raise
            return status(identifier)

    @router.put('/{identifier}/enabled')
    async def enabled(identifier: str, request: Request):
        return await save(identifier, request, parameters=False)

    @router.put('/{identifier}/configuration')
    async def configuration(identifier: str, request: Request):
        return await save(identifier, request, parameters=True)

    return router
