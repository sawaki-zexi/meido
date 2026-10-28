import asyncio

import pytest

from backend.app.advanced_settings import AdvancedSettings
from backend.app.agent_runtime.context_provider import ContextWindowProvider
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime import CancellationToken
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage
from backend.app.ai.model_types import ModelToolCallBlock, ModelToolResultMessage
from backend.app.conversation_history import ConversationHistoryView
from backend.app.models import ModelConfiguration, Role
from test_context_windows import Adapter
from test_message_history_tools import setup_history
from test_owner_history_view import pair, private


def scenario(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    thread = private(channels); pair(channels, thread)
    for index in range(8):
        key = thread.session_key if index % 2 == 0 else 'role:r1'
        sessions.append_message(key, 'user', 'hello ' * 115, run_id=str(index))
        sessions.append_message(key, 'assistant', 'answer ' * 100, run_id=str(index), metadata={'deliveryStatus': 'sent'})
    sessions.append_message(thread.session_key, 'user', '手机最后的工具问题', run_id='last')
    sessions.append_agent_message(thread.session_key, AssistantMessage(tool_calls=(ToolCall('read', 'read_file', {'path': 'x'}),)), run_id='last')
    sessions.append_agent_message(thread.session_key, ToolResultMessage('read', 'read_file', '完整结果'), run_id='last')
    sessions.append_message(thread.session_key, 'assistant', '已经读完', run_id='last', metadata={'deliveryStatus': 'sent'})
    current = sessions.append_message('role:r1', 'user', '继续')
    config = ModelConfiguration(providerId='custom', provider='fixture', baseUrl='http://localhost', model='fixture', contextWindowTokens=8000)
    settings = AdvancedSettings(maxTokens=1024, summaryTokenLimit=512, contextSafetyMarginTokens=100,
        contextTriggerRatio=.5, contextTargetRatio=.2, compactionRetainedTurns=2)
    role = Role(id='r1', name='R', profile={'profile': '助手'}, createdAt=current.createdAt, updatedAt=current.createdAt)
    view = ConversationHistoryView(channels, role_id='r1', through_message_id=current.id)
    adapter = Adapter()
    base = MeidoProvider(adapter, role, config, max_response_tokens=settings.maxTokens)
    base.request_guard = view.require_snapshot_current
    async def coverage(ids):
        return True
    provider = ContextWindowProvider(base, settings, view.windows(), role_id='r1', session_key='role:r1', scope='shared_owner',
        authority=view.authority, authorized=view.authorized, ensure_coverage=coverage)
    return sessions, channels, thread, view, adapter, base, provider


def run(provider, messages):
    async def collect():
        return [event async for event in provider.stream_response(model='fixture', system='', messages=messages, tools=[], signal=CancellationToken())]
    return asyncio.run(collect())


def test_shared_compaction_retains_tool_groups_and_invalidates_on_foreign_transcript_deletion(tmp_path):
    sessions, channels, thread, view, adapter, base, provider = scenario(tmp_path)
    original = sessions.list_messages(thread.session_key)
    run(provider, view.runtime_messages())
    assert provider.compactions == 1
    actual = adapter.calls[-1][0]
    calls = [block.id for message in actual for block in message.content if isinstance(block, ModelToolCallBlock)]
    results = [message.tool_call_id for message in actual if isinstance(message, ModelToolResultMessage)]
    assert calls == results == ['read']
    state = view.windows().load('r1', 'role:r1', 'shared_owner', view.authority)
    assert state['summary'] and state['removed']
    assert sessions.list_messages(thread.session_key) == original
    removed_private = next(message.id for message in original if message.id in state['removed'])
    sessions.delete_message(removed_private)
    restored = ConversationHistoryView(channels, role_id='r1')
    assert restored.windows().load('r1', 'role:r1', 'shared_owner', restored.authority)['summary'] is None


def test_repair_while_summarizing_cannot_commit_or_dispatch_next_request(tmp_path):
    _, channels, thread, view, adapter, _, provider = scenario(tmp_path)
    original = adapter.stream_rich_messages
    async def revoke(*args, **kwargs):
        async for event in original(*args, **kwargs):
            pair(channels, thread)
            yield event
    adapter.stream_rich_messages = revoke
    with pytest.raises((ValueError, PermissionError)):
        run(provider, view.runtime_messages())
    assert len(adapter.calls) == 1
    fresh = ConversationHistoryView(channels, role_id='r1')
    assert fresh.windows().load('r1', 'role:r1', 'shared_owner', fresh.authority)['summary'] is None


def test_revocation_during_summary_transport_wait_blocks_original_history_request(tmp_path):
    _, channels, thread, view, adapter, _, provider = scenario(tmp_path)
    original = adapter.stream_rich_messages
    dispatched = []

    async def delayed_transport(messages, configuration, tools, *, max_tokens=None, cancel_check=None):
        # Model adapters can await connection setup before sending the body.
        await asyncio.sleep(0)
        pair(channels, thread)
        if cancel_check is not None:
            cancel_check()
        dispatched.append(messages)
        async for event in original(messages, configuration, tools, max_tokens=max_tokens, cancel_check=cancel_check):
            yield event

    adapter.stream_rich_messages = delayed_transport
    with pytest.raises((ValueError, PermissionError)):
        run(provider, view.runtime_messages())
    assert not dispatched


def test_revocation_between_provider_start_and_request_blocks_adapter(tmp_path):
    _, channels, thread, view, adapter, base, _ = scenario(tmp_path)
    async def collect():
        stream = base.stream_response(model='fixture', system='', messages=view.runtime_messages(), tools=[], signal=CancellationToken())
        await anext(stream)
        pair(channels, thread)
        return [event async for event in stream]
    events = asyncio.run(collect())
    assert not adapter.calls
    assert '主人历史绑定已变化' in str(events)


def test_original_deleted_after_media_read_is_not_sent_to_model(tmp_path):
    sessions, _, _, view, adapter, base, _ = scenario(tmp_path)
    async def collect():
        messages = view.runtime_messages()
        stream = base.stream_response(model='fixture', system='', messages=messages, tools=[], signal=CancellationToken())
        await anext(stream)
        sessions.delete_message(messages[0].source_message_ids[0])
        return [event async for event in stream]
    events = asyncio.run(collect())
    assert not adapter.calls
    assert '历史消息在当前回合中已变化' in str(events)
