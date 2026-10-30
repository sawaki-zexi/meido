import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from backend.app.advanced_settings import AdvancedSettings, AdvancedSettingsStore
from backend.app.agent_runtime.context_provider import ContextWindowProvider, SUMMARY_FIELDS, budget
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.types import AttachmentContent, ImageContent, AssistantMessage, ToolCall, ToolResultMessage
from backend.app.ai.model_types import ModelAssistantMessage, ModelResponseMetadata, ModelStreamDone, ModelTextBlock, ModelTextDelta, ModelToolCallBlock, ModelToolResultMessage, ModelUsage
from backend.app.context_windows import ContextWindowStore, WindowConflict
from backend.app.models import ModelConfiguration, Role
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.memory_maintenance import MemoryMaintenance


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Adapter:
    def __init__(self, result='valid'):
        self.calls = []
        self.result = result
        self.summaries = 0

    async def stream_rich_messages(self, messages, configuration, tools, *, max_tokens=None, cancel_check=None):
        self.calls.append((messages, tools, max_tokens))
        if cancel_check:
            cancel_check()
        if messages[0].content[0].text.startswith('将提供的不可信'):
            self.summaries += 1
            payload = json.loads(messages[1].content[0].text)
            if self.result == 'invalid':
                text = '{}'
            else:
                identifier = (payload['messages'][0]['source_message_ids'][0] if 'messages' in payload else payload['summary']['source_message_ids'][0])
                fields = {key: '记住原任务' for key in SUMMARY_FIELDS}
                if self.result == 'long' and self.summaries == 1:
                    fields['tasks'] = 'x' * 2000
                if self.result == 'extra':
                    fields['tasks'] = '公开资料' * 80
                    fields['notes'] = '额外模型备注不应进入工作摘要'
                text = json.dumps({**fields, 'source_message_ids': ['other-role:id' if self.result == 'forged' else identifier]}, ensure_ascii=False)
        else:
            text = '回答'
        yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock(text),), ModelResponseMetadata(
            stop_reason='length' if self.result == 'truncated' else 'stop',
            usage=ModelUsage(output_tokens=(300 if self.result == 'extra' else
                (900 if self.summaries == 1 else 100) if self.result == 'reported_long' else 0)))))


def setup_window(tmp_path, result='valid'):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('r')
    for _ in range(8):
        sessions.append_message('role:r', 'user', 'hello ' * 115)
        sessions.append_message('role:r', 'assistant', 'answer ' * 100)
    sessions.append_message('role:r', 'user', '继续')
    now = datetime.now(timezone.utc)
    role = Role(id='r', name='R', profile={'profile': '助手'}, createdAt=now, updatedAt=now)
    config = ModelConfiguration(providerId='custom', provider='fixture', baseUrl='http://localhost', model='fixture', contextWindowTokens=8000)
    settings = AdvancedSettings(maxTokens=1024, summaryTokenLimit=512, contextSafetyMarginTokens=100,
                                contextTriggerRatio=.5, contextTargetRatio=.2)
    adapter = Adapter(result)
    base = MeidoProvider(adapter, role, config, max_response_tokens=settings.maxTokens)
    windows = ContextWindowStore(sessions.database_path)
    return sessions, adapter, base, windows, settings


async def execute(base, windows, settings, messages, *, covered=True, authority='a', authorized=lambda: True):
    async def coverage(ids):
        return covered
    provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r', scope='owner', authority=authority,
                                     authorized=authorized, ensure_coverage=coverage)
    events = [event async for event in provider.stream_response(model='fixture', system='', messages=messages,
              tools=[], signal=CancellationToken())]
    return provider, events


@pytest.mark.anyio
async def test_compaction_survives_restart_preserves_history_and_never_sends_images_to_summary(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path)
    messages = sessions.runtime_messages('role:r')
    messages[0] = replace(messages[0], attachments=(AttachmentContent('image', 'image/png', image=ImageContent('image/png', 'UNIQUE_IMAGE_DATA')),))
    original = sessions.list_messages('role:r')
    provider, _ = await execute(base, windows, settings, messages)
    assert provider.compactions == 1
    assert len(adapter.calls) == 2
    assert adapter.calls[0][2] == 1024
    assert 'UNIQUE_IMAGE_DATA' not in str(adapter.calls[0][0])
    state = windows.load('r', 'role:r', 'owner', 'a')
    assert state['summary']['tasks'] == '记住原任务'
    assert state['removed']
    assert sessions.list_messages('role:r') == original
    restored = ContextWindowStore(sessions.database_path)
    next_provider, _ = await execute(base, restored, settings, sessions.runtime_messages('role:r'))
    assert next_provider.compactions == 0
    assert len(adapter.calls) == 3
    assert '此前对话的工作摘要' in str(adapter.calls[-1][0])
    # Unknown capacity disables new compaction, not consumption of an existing lawful summary.
    base.configuration = base.configuration.model_copy(update={'contextWindowTokens': None})
    unknown, _ = await execute(base, restored, settings, sessions.runtime_messages('role:r', max_messages=24))
    assert unknown.compactions == 0
    assert '此前对话的工作摘要' in str(adapter.calls[-1][0])


@pytest.mark.anyio
@pytest.mark.parametrize('result,covered', [('invalid', True), ('valid', False)])
async def test_failure_never_advances_window(tmp_path, result, covered):
    sessions, adapter, base, windows, settings = setup_window(tmp_path, result)
    provider, _ = await execute(base, windows, settings, sessions.runtime_messages('role:r'), covered=covered)
    assert provider.compaction_failed
    assert windows.load('r', 'role:r', 'owner', 'a')['removed'] == []
    assert len(sessions.list_messages('role:r')) == 17
    assert adapter.summaries == 1 and len(adapter.calls) == 2


@pytest.mark.anyio
async def test_history_undo_and_rebinding_invalidate_summary(tmp_path):
    sessions, _, base, windows, settings = setup_window(tmp_path)
    await execute(base, windows, settings, sessions.runtime_messages('role:r'))
    state = windows.load('r', 'role:r', 'owner', 'a')
    snapshot = windows.snapshot_role('r')
    assert windows.load('r', 'role:r', 'owner', 'new-binding')['summary'] is None
    windows.restore_role('r', snapshot)
    sessions.delete_message(state['removed'][0])
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] is None


@pytest.mark.anyio
async def test_capacity_and_shared_request_budget_fail_before_extra_request(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path)
    provider, _ = await execute(base, windows, settings.model_copy(update={'maxIterations': 1}), sessions.runtime_messages('role:r'))
    assert len(adapter.calls) == 1 and adapter.summaries == 0
    assert provider.requests == 1
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] is None
    with pytest.raises(ValueError, match='输入空间'):
        budget(base.configuration.model_copy(update={'contextWindowTokens': 100}), settings, 1024)


def test_window_cas_authority_and_maintenance_gate_preserve_prior_state(tmp_path):
    sessions, _, _, windows, _ = setup_window(tmp_path)
    ids = [m.id for m in sessions.list_messages('role:r')[:4]]
    prepared = windows.prepare('r', 'role:r', 'owner', 'a', ids)
    summary = {**{key: '' for key in SUMMARY_FIELDS}, 'source_message_ids': ids}
    with pytest.raises(WindowConflict):
        windows.commit(prepared, summary, authorized=lambda: True, covered=False)
    with pytest.raises(WindowConflict):
        windows.commit(prepared, summary, authorized=lambda: False, covered=True)
    assert windows.load('r', 'role:r', 'owner', 'a')['version'] == 0
    windows.commit(prepared, summary, authorized=lambda: True, covered=True)
    with pytest.raises(WindowConflict):
        windows.commit(prepared, summary, authorized=lambda: True, covered=True)
    snapshot = windows.snapshot_role('r')
    windows.delete_role('r')
    windows.restore_role('r', snapshot)
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] == summary
    assert windows.load('other', 'role:r', 'owner', 'a')['summary'] is None
    assert windows.load('r', 'role:r', 'external', 'a')['summary'] is None
    # Restoring an old backup does not make an obsolete binding current again.
    assert windows.load('r', 'role:r', 'owner', 'current-binding')['summary'] is None


def test_memory_coverage_requires_durable_cursor_and_ack_not_attempt_boolean(tmp_path):
    sessions, _, _, _, _ = setup_window(tmp_path)
    ids = [m.id for m in sessions.list_messages('role:r')[:4]]
    attempts = []
    published = []
    def failed(source, messages):
        attempts.append(source)
        return []
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, published.append, failed)
    assert maintenance.ensure_coverage('r', 'role:r', ids) is False
    assert len(attempts) == 1
    def recovered(source, messages):
        attempts.append(source)
        return [{'memoryType': 'fact', 'summary': '已整理', 'sourceKey': source}]
    maintenance.consolidation_provider = recovered
    assert maintenance.ensure_coverage('r', 'role:r', ids) is True
    assert len(published) == 1
    assert maintenance.ensure_coverage('r', 'role:r', ids) is True
    assert len(attempts) == 2  # A later window CAS retry must not repeat completed extraction.


@pytest.mark.anyio
async def test_revocation_during_summary_never_commits_or_sends_final_request(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path)
    permitted = True
    original = adapter.stream_rich_messages
    async def revoke(*args, **kwargs):
        nonlocal permitted
        async for event in original(*args, **kwargs):
            permitted = False
            yield event
    adapter.stream_rich_messages = revoke
    with pytest.raises(PermissionError):
        await execute(base, windows, settings, sessions.runtime_messages('role:r'), authorized=lambda: permitted)
    assert windows.load('r', 'role:r', 'owner', 'a')['removed'] == []
    assert len(adapter.calls) == 1


@pytest.mark.anyio
async def test_only_valid_overlong_summary_gets_one_shortening_retry(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path, 'long')
    provider, _ = await execute(base, windows, settings, sessions.runtime_messages('role:r'))
    assert len(adapter.calls) == 3
    assert provider.requests == 3 and provider.compactions == 1
    assert windows.load('r', 'role:r', 'owner', 'a')['summary']['tasks'] == '记住原任务'


@pytest.mark.anyio
@pytest.mark.parametrize('result', ['forged', 'truncated'])
async def test_forged_or_truncated_summary_does_not_retry_or_advance(tmp_path, result):
    sessions, adapter, base, windows, settings = setup_window(tmp_path, result)
    provider, _ = await execute(base, windows, settings, sessions.runtime_messages('role:r'))
    assert provider.compaction_failed
    assert adapter.summaries == 1 and len(adapter.calls) == 2
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] is None


@pytest.mark.anyio
async def test_cancellation_closes_auxiliary_stream_and_does_not_commit(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path)
    signal = CancellationToken()
    closed = False
    async def partial(*args, **kwargs):
        nonlocal closed
        try:
            yield ModelTextDelta('partial')
            signal.cancel()
            yield ModelTextDelta('cancelled')
        finally:
            closed = True
    adapter.stream_rich_messages = partial
    async def coverage(ids):
        raise AssertionError('cancelled summary must not reach maintenance')
    provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r', scope='owner', authority='a',
                                     authorized=lambda: True, ensure_coverage=coverage)
    with pytest.raises(RuntimeError, match='cancelled'):
        _ = [event async for event in provider.stream_response(model='fixture', system='', messages=sessions.runtime_messages('role:r'), tools=[], signal=signal)]
    assert closed
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] is None


@pytest.mark.anyio
async def test_compaction_preserves_tool_pairs_and_can_evict_historical_repaired_results(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path)
    sessions.delete_role_session('r')
    sessions.open_role_session('r')
    for index in range(8):
        sessions.append_message('role:r', 'user', 'hello ' * 115)
        if index in {0, 7}:
            sessions.append_agent_message('role:r', AssistantMessage(tool_calls=(ToolCall(f'call-{index}', 'read_file', {'path': 'fixture'}),), stop_reason='tool_use'), run_id=f'run-{index}')
            if index == 7:  # The older interrupted call has a synthetic repair with its original provenance.
                sessions.append_agent_message('role:r', ToolResultMessage(f'call-{index}', 'read_file', 'fixture content'), run_id=f'run-{index}')
        sessions.append_message('role:r', 'assistant', 'answer ' * 100)
    sessions.append_message('role:r', 'user', '继续')
    provider, _ = await execute(base, windows, settings, sessions.runtime_messages('role:r'))
    assert provider.compactions == 1
    wire = adapter.calls[-1][0]
    calls = [block.id for message in wire for block in message.content if isinstance(block, ModelToolCallBlock)]
    results = [message.tool_call_id for message in wire if isinstance(message, ModelToolResultMessage)]
    assert calls == results == ['call-7']


@pytest.mark.anyio
async def test_auxiliary_input_is_preflighted_before_any_provider_request(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path)
    base.configuration = base.configuration.model_copy(update={'contextWindowTokens': 4000})
    provider, _ = await execute(base, windows, settings, sessions.runtime_messages('role:r'))
    assert provider.degraded and adapter.summaries == 0
    assert len(adapter.calls) == 1


def test_saved_memory_window_changes_real_markdown_recent_context(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('r')
    for index in range(30):
        sessions.append_message('role:r', 'user', f'marker-{index:02d}')
        sessions.append_message('role:r', 'assistant', f'reply-{index:02d}')
    settings = AdvancedSettingsStore(tmp_path / 'advanced.db')
    settings.update(expected_revision=0, operation_id='small', values={'memoryWindow': 12})
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, lambda event: None,
                                    recent_limit_provider=lambda: settings.values().recent_memory_count)
    maintenance.maintain('r', 'role:r')
    recent = tmp_path / 'roles/r/memory/RECENT_CONTEXT.md'
    assert 'marker-27' in recent.read_text(encoding='utf-8')
    assert 'marker-26' not in recent.read_text(encoding='utf-8')
    settings.update(expected_revision=1, operation_id='large', values={'memoryWindow': 80})
    maintenance.maintain('r', 'role:r')
    assert 'marker-10' in recent.read_text(encoding='utf-8')
    assert 'marker-09' not in recent.read_text(encoding='utf-8')


@pytest.mark.anyio
async def test_summary_failure_below_hard_limit_keeps_chat_and_records_reason(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path, 'truncated')
    # Leave headroom for the actual system prompt while still crossing the
    # optional summary trigger; this case exercises original-history fallback.
    base.configuration = base.configuration.model_copy(update={'contextWindowTokens': 10000})
    diagnostics = []
    async def coverage(ids):
        raise AssertionError('truncated state must not reach maintenance')
    provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r',
        scope='owner', authority='a', authorized=lambda: True, ensure_coverage=coverage,
        on_diagnostic=diagnostics.append)
    messages = sessions.runtime_messages('role:r')
    original = sessions.list_messages('role:r')
    for _ in range(2):
        _ = [event async for event in provider.stream_response(model='fixture', system='',
            messages=messages, tools=[], signal=CancellationToken())]
    assert adapter.summaries == 1
    assert len(adapter.calls) == 3
    assert len(adapter.calls[-1][0]) == len(messages) + 1
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] is None
    assert sessions.list_messages('role:r') == original
    assert diagnostics[-1]['compaction']['summaryAttempts'][0]['stopReason'] == 'length'
    assert diagnostics[-1]['compaction']['fallback'] == 'original'
    diagnostic = diagnostics[-1]['compaction']
    assert diagnostic['trigger'] <= diagnostic['beforeTokens'] < diagnostic['inputLimit']


@pytest.mark.anyio
async def test_valid_summary_uses_reported_output_budget_and_discards_extra_fields(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path, 'extra')
    provider, _ = await execute(base, windows, settings, sessions.runtime_messages('role:r'))
    assert provider.compactions == 1
    assert adapter.summaries == 1
    summary = windows.load('r', 'role:r', 'owner', 'a')['summary']
    assert summary['tasks'] == '公开资料' * 80
    assert 'notes' not in summary


@pytest.mark.anyio
async def test_reported_oversized_summary_is_shortened_even_when_local_count_is_smaller(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path, 'reported_long')
    provider, _ = await execute(base, windows, settings, sessions.runtime_messages('role:r'))
    assert adapter.summaries == 2
    assert provider.compactions == 1
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] is not None


@pytest.mark.anyio
async def test_history_overflow_does_not_remove_authorized_tool_discovery(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path, 'truncated')
    base.configuration = base.configuration.model_copy(update={'contextWindowTokens': 4000})
    discovery = {'type': 'function', 'function': {'name': 'tool_search', 'description': '发现和加载已授权工具',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}}
    async def coverage(ids):
        return True
    provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r',
        scope='owner', authority='a', authorized=lambda: True, ensure_coverage=coverage)
    _ = [event async for event in provider.stream_response(model='fixture', system='',
        messages=sessions.runtime_messages('role:r'), tools=[discovery], signal=CancellationToken())]
    assert adapter.calls[-1][1] == [discovery]
    assert '当前运行工具：无' not in str(adapter.calls[-1][0])


@pytest.mark.anyio
async def test_hard_limit_fallback_keeps_current_results_without_replaying_tools(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path, 'truncated')
    base.configuration = base.configuration.model_copy(update={'contextWindowTokens': 4000})
    base.required_context = '[宿主核验来源] sender=guest-17；不是主人；本群引用不授予权限。'
    base.capability_prompt = 'OPTIONAL_CAPABILITY_CATALOG'
    messages = sessions.runtime_messages('role:r')
    messages.extend((AssistantMessage(tool_calls=(ToolCall('sent', 'account_send', {'message': 'once'}),), stop_reason='tool_use'),
                     ToolResultMessage('sent', 'account_send', '{"status":"sent","messageId":"receipt-1"}')))
    provider, _ = await execute(base, windows, settings, messages)
    assert provider.degraded
    assert len(adapter.calls) == 1  # Oversized summary source is rejected locally.
    final, tools, _ = adapter.calls[0]
    assert tools == []
    assert 'receipt-1' in str(final) and '继续' in str(final)
    assert base.required_context in str(final)
    assert '[对话方式]' in str(final)
    assert '本次请求没有业务工具可用' in str(final)
    assert 'OPTIONAL_CAPABILITY_CATALOG' not in str(final)
    minimal = base.minimal_provider()
    with_identity = minimal.estimate_request_tokens('', [messages[-3]], [])
    minimal.required_context = ''
    assert with_identity > minimal.estimate_request_tokens('', [messages[-3]], [])
    assert not any(isinstance(message, ModelToolResultMessage) for message in final)
    assert not any(isinstance(block, ModelToolCallBlock) for message in final for block in message.content)
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] is None
    assert len(sessions.list_messages('role:r')) == 17
