"""Bind a real hearing input to the ordinary source-scoped tool runtime."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import replace
import time
import json

from ..agent_runtime.events import MessageEndEvent
from ..agent_runtime.provider import AssistantDoneEvent, ToolCallEndEvent
from ..agent_runtime.types import AssistantMessage, ToolResultMessage, UserMessage


class ExpressionProvider:
    """Correct malformed expression once using existing results and no new tools."""
    def __init__(self, provider):
        self.provider = provider
        self.corrected = False

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def stream_response(self, **kwargs):
        events = [event async for event in self.provider.stream_response(**kwargs)]
        done = next((event.message for event in reversed(events) if isinstance(event, AssistantDoneEvent)), None)
        if done is not None and not done.tool_calls and done.stop_reason == 'stop' and done.content.strip():
            try:
                value = json.loads(done.content)
                valid = isinstance(value, dict) and bool(value) and (
                    set(value) <= {'text', 'candidate_id'} or set(value) == {'response'})
            except ValueError:
                valid = False
            if not valid and not self.corrected:
                self.corrected = True
                correction = UserMessage('[自然表达格式纠正] 刚才的正文没有发送。基于已有真实结果，仅返回'
                    ' {"text":"短回复","candidate_id":null} 或 {"response":完整新选择}。不要重复执行工具。')
                corrected = [event async for event in self.provider.stream_response(**{**kwargs, 'tools': (),
                        'messages': [*kwargs['messages'], done, correction]})]
                if any(isinstance(event, ToolCallEndEvent) or isinstance(event, AssistantDoneEvent)
                        and event.message.tool_calls for event in corrected):
                    raise ValueError('格式纠正不能调用业务工具')
                for event in corrected:
                    yield event
                return
        for event in events:
            yield event


async def execute(host, conversation, run, provider, messages, authorized):
    batch, thread = conversation.batch, conversation.batch.thread
    current = batch.snapshot[-1]
    if current.record is None or current.source is None:
        raise PermissionError('natural execution requires its actual admitted input')
    source = current.source
    record = next((item for item in conversation.records if item['id'] == current.record['id']), current.record)

    def valid():
        return authorized() and source.authorization_denial(host.multichannel_store) is None

    run = run.model_copy(update={'modelSnapshot': {**run.modelSnapshot,
        'channelTurnSource': source.to_dict(), 'currentInputId': current.record['id']}})
    run = await host.runtime_manager.reserve_natural_run(run, record, can_start=valid)
    batch.execution_runs[current.message_id] = run.runId
    capabilities = None
    started = False
    try:
        remaining = batch.deadline - conversation.clock()
        deadline = time.monotonic() + remaining
        async with asyncio.timeout(max(.001, remaining)):
            available = host._runtime_tool_candidates(conversation.role,
                is_channel_owner=source.owner_context, source_thread_id=thread.thread_id,
                source_account_id=thread.account_id, include_channel=True)
            attachments = [item for key in ('attachments', 'quotedAttachments')
                for item in current.record['metadata'].get(key, []) if isinstance(item, dict)]
            available = (*available, host.ReadChannelAttachmentTool(
                thread.role_id, thread.session_key, attachments, authorized=valid))
            capabilities = await host._runtime_capabilities(conversation.role,
                session_key=thread.session_key, run_id=run.runId,
                available_tools=available, prompt_text=current.content,
                external_only=not source.owner_context, source_authority=source)
            # These are the ordinary declarations and current-registry policy.
            provider.capability_prompt += '\n' + capabilities.prompt_context()
            provider.skill_state = capabilities._skill_state
            provider.required_context += ('\n[本轮执行来源（宿主固定）]\n当前发送者 ID：' + source.sender_id
                + '\n当前发送者身份：' + ('已配对主人' if source.owner_context else '未配对成员')
                + '\n选择回应对象不会更换此来源。群内不读取主人私聊记忆。'
                + '\n长生图使用 background=true；accepted 表示已受理，不能声称生成或发送完成。')
            provider.current_input = next((item for item in messages
                if current.record['id'] in item.source_message_ids), None)
            host.session_store.update_run(run.runId, status='running', model_snapshot={
                **run.modelSnapshot, 'capabilities': capabilities.run_metadata()})
            host._bind_spawn_runtime(capabilities, provider, max_iterations=conversation.limits.max_calls)
            owner_loop = asyncio.get_running_loop()
            async def child_request_start(prepared, signal):
                # Synchronous child requests belong to this short batch. Their
                # executor waits for the slot here, then commits synchronously
                # on its own loop immediately before adapter entry.
                pending = asyncio.run_coroutine_threadsafe(provider.request_start(prepared, signal), owner_loop)
                return await asyncio.wrap_future(pending)
            for tool in capabilities.registered_tools():
                if isinstance(tool, host.SpawnTool):
                    tool.synchronous_request_start = child_request_start
            # Replanning retains finished tool facts, including uncertain effects.
            # Other inputs see only the shared, confirmed conversation timeline.
            records = [item for item in host.session_store.list_messages(thread.session_key)
                if item.runId == run.runId and (item.role == 'tool'
                    or item.role == 'assistant' and item.messageType == 'tool_call')]
            facts = host.session_store.runtime_messages(thread.session_key, stored_messages=records)
            messages = [*messages, *facts]

            def before_tool():
                if not valid() or batch.tool_calls >= conversation.limits.max_tools:
                    return False
                batch.tool_calls += 1
                return True

            def persist(event):
                message = event.message
                if isinstance(message, ToolResultMessage) or isinstance(message, AssistantMessage) and message.tool_calls:
                    if isinstance(message, AssistantMessage):
                        message = replace(message, content='')
                    return host.session_store.append_agent_message(thread.session_key, message, run_id=run.runId)

            result = None
            started = True
            async with aclosing(host.runtime_manager.run(run, provider=ExpressionProvider(provider),
                    model=conversation.configuration.model if conversation.configuration else '',
                    system='', messages=messages, capabilities=capabilities,
                    max_turns=conversation.limits.max_calls, provider_timeout=min(30., remaining),
                    tool_timeout=remaining, deadline=deadline,
                    can_continue=valid, before_tool=before_tool, on_message_end=persist,
                    defer_reply=True, keep_open=True)) as stream:
                async for event in stream:
                    if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage) and not event.message.tool_calls:
                        result = event.message
            return result or AssistantMessage()
    finally:
        if not started:
            if capabilities is not None:
                await capabilities.close()
            host.runtime_manager.cancel_unstarted_run(run.runId, '自然来源或预算已失效')
