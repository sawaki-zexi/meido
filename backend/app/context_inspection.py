"""Read the saved desktop input without accepting a turn or changing its window.

Adapted from Shiori c14152ed context_window_request.py / context_requests.py.
Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from .advanced_settings import AdvancedSettings
from .agent_runtime.natural_reply import finish_reply_schema, natural_chat_enabled
from .agent_runtime.provider import CancellationToken
from .plugin_configuration import PARAMETERS

logger = logging.getLogger(__name__)


def unavailable(role_id, reason, *, busy=False):
    return {'roleId': role_id, 'sessionKey': f'role:{role_id}',
        'status': 'busy' if busy else 'unavailable', 'reason': reason,
        **dict.fromkeys(('inputTokens', 'source', 'capacity', 'configurationId', 'model',
            'inputLimit', 'outputReserve', 'safetyMargin', 'compactTrigger', 'compactTarget'))}


def _busy(host, role_id):
    # Reuse current owners; do not acquire their locks or create a second lane.
    service = host._chat_run_service
    active = service.active(role_id) if service is not None and service.sessions is host.session_store else None
    locks = (host.role_locks.get(role_id), host.runtime_manager.role_locks.get(role_id),
             host.memory_worker.role_lock_for(role_id))
    maintenance = any(owner == role_id and not task.done() for worker in
        (host.memory_worker, host.memory_optimizer_worker) for task, owner in worker._tasks.items())
    return bool(active or role_id in host._channel_roles_deleting or maintenance or any(lock and lock.locked() for lock in locks))


def _facts(host, role_id):
    role = host.store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail='角色不存在')
    return (role, host.model_configuration_store.get(role.modelConfigurationId), host.advanced_settings.get(),
        host.memory_service.context_block([], host._role_memory_context(role_id, include_recent=False, readonly=True)),
        tuple(host._external_activity_blocks(role_id)),
        tuple(host.plugin_configuration.get(name) for name in PARAMETERS),
        tuple((config, host.mcp_host.observed_catalog(role_id, config.server_id))
            for config in host.mcp_host.store.all() if role_id in config.role_ids))


def _read(host, role_id):
    facts = _facts(host, role_id)
    history = host._owner_history(role_id)
    configuration = facts[1]
    limit = None if configuration and configuration.contextWindowTokens else max(100, AdvancedSettings.model_validate(facts[2]['values']).recent_memory_count)
    messages = history.runtime_messages(max_messages=limit)
    return facts, history, messages


def _current(host, role_id, facts, history, window, state):
    if _facts(host, role_id) != facts:
        return False
    # The normal turn fence deliberately ignores new appends. Inspection must
    # include them, so compare against a fresh *role-scoped* view after the await.
    current = host._owner_history(role_id)
    if current.authority != history.authority or current.read() != history.read():
        return False
    return window.windows.load(role_id, window.session_key, window.scope, window.authority) == state


async def inspect_context(host, role_id):
    if _busy(host, role_id):
        return unavailable(role_id, '正在回复或整理上下文，请稍后重试', busy=True)
    signal = CancellationToken()
    capabilities = None
    try:
        facts, history, messages = await asyncio.to_thread(_read, host, role_id)
        role, configuration, advanced, memory, *_ = facts
        if configuration is None:
            return unavailable(role_id, '该角色绑定的模型已不存在，请重新选择' if role.modelConfigurationId else '尚未配置聊天模型')
        if _busy(host, role_id) or await asyncio.to_thread(host.session_store.active_run, role_id):
            return unavailable(role_id, '正在回复或整理上下文，请稍后重试', busy=True)
        settings = AdvancedSettings.model_validate(advanced['values'])
        # Preview builds the same tools without authoring Drift files, binding
        # executions to a fake run, or recreating a missing workspace directory.
        candidates = await asyncio.to_thread(host._runtime_tool_candidates, role,
            include_channel=await asyncio.to_thread(host._role_has_usable_channel_account, role_id),
            source_thread_id=f'desktop:{role_id}', history=history, preview=True)
        capabilities = await host._runtime_capabilities(role, session_key=history.session_key, run_id='',
            available_tools=candidates, advanced_snapshot=advanced, inspection=True)
        provider = host._desktop_provider(role, configuration, settings, capabilities, memory, readonly=True)
        window = await asyncio.to_thread(host._ordinary_context_provider, provider, settings,
            role_id=role_id, session_key=history.session_key, run_id='', history=history, readonly=True)
        window.bind_capabilities(capabilities)
        value, state = await window.inspect(messages, signal,
            protocol_tools=(finish_reply_schema(),) if natural_chat_enabled(role) else ())
        current = await asyncio.to_thread(_current, host, role_id, facts, history, window, state)
        if _busy(host, role_id):
            return unavailable(role_id, '正在回复或整理上下文，请稍后重试', busy=True)
        if not current:
            return unavailable(role_id, '上下文或设置在读取期间发生变化，请重新检查')
        return {'roleId': role_id, 'sessionKey': history.session_key, 'status': 'ready', **value}
    except asyncio.CancelledError:
        signal.cancel()
        raise
    except HTTPException:
        raise
    except (ValueError, PermissionError) as error:
        return unavailable(role_id, str(error))
    except Exception:
        logger.warning('Saved context inspection failed role=%s', role_id, exc_info=True)
        return unavailable(role_id, '读取当前上下文失败，请重新检查')
    finally:
        if capabilities is not None:
            await capabilities.close()
