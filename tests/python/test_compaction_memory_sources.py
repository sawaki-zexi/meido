"""Compaction proves the full history while memory extracts ordinary dialogue."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.advanced_settings import AdvancedSettingsStore
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage
from backend.app.context_windows import ContextWindowStore
from backend.app.memory_maintenance import MemoryMaintenance
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_store import MemoryStore
from backend.app.models import AgentRun, ModelConfigurationInput
from backend.app.session_store import SessionStore
from test_chat_stop import chat_server  # noqa: F401
from test_context_windows import Adapter


@pytest.fixture
def compaction_history(chat_server, monkeypatch, tmp_path, request):
    client, role, other = chat_server
    monkeypatch.setattr(main, 'roles_root', main.store.root)
    monkeypatch.setattr(main, 'data_root', tmp_path / 'data')
    policy = AdvancedSettingsStore(tmp_path / 'advanced.db')
    policy.update(expected_revision=0, operation_id='fixture', values={
        'maxTokens': 1024, 'summaryTokenLimit': 512, 'contextSafetyMarginTokens': 100,
        'contextTriggerRatio': .75, 'contextTargetRatio': .2})
    monkeypatch.setattr(main, 'advanced_settings', policy)
    configuration = main.model_configuration_store.create(ModelConfigurationInput(
        providerId='custom', provider='fixture', baseUrl='http://127.0.0.1:1',
        model='fixture', contextWindowTokens=64000))
    assert client.put(f'/api/roles/{role.id}/model-configuration',
        json={'configurationId': configuration.id}).status_code == 200
    sessions, channels = main.session_store, main.multichannel_store
    session = f'role:{role.id}'
    sessions.open_role_session(role.id)
    private = getattr(request, 'param', 'desktop') == 'private'
    metadata = {}
    if private:
        channels.register_channel_account(role.id, 'qq', account_id='fixture-qq', enabled=True)
        thread = channels.ensure_channel_thread(role.id, 'qq', 'fixture-qq', 'owner', 'private')
        code = channels.create_pairing_code()
        assert channels.consume_pairing_code(code, role_id=role.id, channel='qq', scope='platform',
            platform_user_id='owner', account_id='fixture-qq', chat_id='owner')
        session = thread.session_key
        metadata = {'channel': 'qq', 'senderIsOwner': True, 'senderId': 'owner', 'deliveryStatus': 'sent'}
    # Terminal failed/cancelled runs leave legitimate input in the original history.
    # It is not an active unfinished turn and must not permanently block eviction.
    now = datetime.now(timezone.utc)
    sessions.create_run(AgentRun(runId='old-failed', roleId=role.id, sessionKey=session,
        status='cancelled' if private else 'failed', startedAt=now, endedAt=now, error='controlled old failure'))
    failed_user = sessions.append_message(session, 'user', 'SAVED_FAILED_USER',
        run_id='old-failed', metadata=metadata)
    sessions.append_message(session, 'assistant', 'FAILED_ASSISTANT', 'failed',
        run_id='old-failed', metadata=metadata)
    tool_ids = []
    for index in range(8):
        run = f'saved-{index}'
        sessions.append_message(session, 'user', f'OWNER_FACT_{index} ' * 120,
            run_id=run, metadata=metadata)
        if index == 0:
            calls = sessions.append_agent_message(session, AssistantMessage('tool intent',
                tool_calls=(ToolCall('saved-call', 'read_file', {'path': 'fixture.txt'}),),
                stop_reason='tool_use', metadata=metadata), run_id=run)
            results = sessions.append_agent_message(session,
                ToolResultMessage('saved-call', 'read_file', 'SAVED_TOOL_RECEIPT'), run_id=run)
            tool_ids = [item.id for item in [*calls, *results]]
        sessions.append_message(session, 'assistant', f'ANSWER_{index} ' * 120,
            run_id=run, metadata=metadata)
        if index == 0:
            completion = sessions.append_task_completion(role.id, session, 'saved-task',
                'SAVED_BACKGROUND_COMPLETION', task_status='completed')
            sessions.update_message(completion.id, completion.content, completion.status,
                metadata={**completion.metadata, 'deliveryStatus': 'sent'})
    extracted = []
    def extract(source, messages):
        extracted.append(tuple(messages))
        assert all(message.messageType == 'text' for message in messages)
        assert all('SAVED_TOOL_RECEIPT' not in message.content and 'FAILED_ASSISTANT' not in message.content
            and 'SAVED_BACKGROUND_COMPLETION' not in message.content for message in messages)
        return {'history': 'SAVED_OWNER_HISTORY', 'pending': '- [preference] SAVED_OWNER_CANDIDATE',
            'recentContext': 'SAVED_RECENT_CONTEXT', 'pendingItems': []}
    maintenance = MemoryMaintenance(main.store.root, sessions, consolidation_provider=extract,
        messages_provider=lambda target, _: channels.owner_context_messages(target),
        ownership_provider=main._memory_ownership, defer_consumers=True)
    service = MemoryService(MemoryStore(tmp_path / 'memory.db'), memory_enabled=lambda: False)
    worker = MemoryWorker(service, maintenance)
    for name, value in {'memory_service': service, 'memory_maintenance': maintenance,
                        'memory_worker': worker, 'model_adapter': Adapter()}.items():
        monkeypatch.setattr(main, name, value)
    before = client.get(f'/api/roles/{role.id}/context-status').json()
    assert before['status'] == 'ready', before
    trigger = int(before['inputTokens'] * .75)
    configuration = main.model_configuration_store.update(configuration.id,
        configuration.model_copy(update={'modelAutoCompactTokenLimit': trigger}))
    return SimpleNamespace(client=client, role=main.store.get(role.id), other=other, sessions=sessions,
        channels=channels, session=session, tool_ids=tool_ids, completion=completion,
        failed_user=failed_user, maintenance=maintenance, worker=worker, extracted=extracted,
        adapter=main.model_adapter, configuration=configuration, policy=policy)


@pytest.mark.parametrize('compaction_history', ['desktop', 'private'], indirect=True)
@pytest.mark.parametrize('entrypoint', ['automatic', 'manual'])
def test_completed_tool_and_background_history_compacts_and_survives_restart(compaction_history, entrypoint, monkeypatch):
    state = compaction_history
    original = state.sessions.list_messages(state.session)
    def consumer_unavailable(event):
        raise RuntimeError('controlled downstream failure')
    state.maintenance.on_consolidation_committed = consumer_unavailable
    if entrypoint == 'manual':
        response = state.client.post(f'/api/roles/{state.role.id}/context-compact')
        assert response.status_code == 200
        assert response.json().get('compacted') is True, response.text
    else:
        response = state.client.post(f'/api/roles/{state.role.id}/messages', json={'content': '继续'})
        assert response.status_code == 200
        assert 'event: assistant_completed' in response.text, response.text
    assert state.adapter.summaries > 0
    history = main._owner_history(state.role.id)
    authority = json.dumps({'role': state.role.id}, sort_keys=True) + history.authority
    restarted = history.windows()
    saved = restarted.load(state.role.id, f'role:{state.role.id}', 'shared_owner', authority)
    assert saved['summary'] and set([*state.tool_ids, state.completion.id, state.failed_user.id]) <= set(saved['removed'])
    assert len(state.extracted) == 1
    progress = state.sessions.maintenance_progress(state.role.id, f'role:{state.role.id}')
    assert progress['memoryVersion'] == 1 and progress['pendingEvents']
    folder = main.store.root / state.role.id / 'memory'
    assert 'SAVED_OWNER_CANDIDATE' in (folder / 'PENDING.md').read_text(encoding='utf-8')
    assert 'SAVED_RECENT_CONTEXT' in (folder / 'RECENT_CONTEXT.md').read_text(encoding='utf-8')
    assert all(item in state.sessions.list_messages(state.session) for item in original)
    summaries = state.adapter.summaries
    monkeypatch.setattr(main, 'session_store', SessionStore(state.sessions.database_path))
    monkeypatch.setattr(main, 'multichannel_store', state.channels.__class__(state.sessions.database_path, main.session_store))
    response = state.client.post(f'/api/roles/{state.role.id}/messages', json={'content': '继续使用已保存摘要'})
    assert response.status_code == 200
    assert 'event: assistant_completed' in response.text, response.text
    assert state.adapter.summaries == summaries
    assert len(state.extracted) == 1
    assert '此前对话的工作摘要' in str(state.adapter.calls[-1][0])
    with sqlite3.connect(state.sessions.database_path) as db:
        assert db.execute('SELECT COUNT(*) FROM tool_audits').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['missing', 'other_role', 'unfinished'])
def test_coverage_does_not_ignore_unknown_or_incomplete_sources(compaction_history, change):
    state = compaction_history
    bad = 'does-not-exist'
    if change == 'other_role':
        state.sessions.open_role_session(state.other.id)
        bad = state.sessions.append_message(f'role:{state.other.id}', 'assistant', 'other history',
            message_type='internal_completion').id
    elif change == 'unfinished':
        bad = state.sessions.append_message(state.session, 'tool', 'not completed', 'running',
            message_type='tool_result', tool_call_id='unfinished', tool_name='read_file').id
    history = main._owner_history(state.role.id)
    provider = main._ordinary_context_provider(MeidoProvider(state.adapter, state.role, state.configuration),
        state.policy.values(), role_id=state.role.id, session_key=f'role:{state.role.id}',
        run_id='', history=history)
    assert not asyncio.run(provider.ensure_coverage([*state.tool_ids, bad]))
    assert not state.extracted and not state.adapter.calls


@pytest.mark.parametrize('failure', ['extract', 'changed_tool', 'revoked'])
@pytest.mark.parametrize('compaction_history', ['private'], indirect=True)
def test_failed_memory_or_changed_full_source_never_requests_summary(compaction_history, failure):
    state = compaction_history
    original = state.maintenance.consolidation_provider
    def extract(source, messages):
        if failure == 'extract':
            raise TimeoutError('controlled extraction failure')
        if failure == 'changed_tool':
            message = next(item for item in state.sessions.list_messages(state.session) if item.id == state.tool_ids[-1])
            state.sessions.update_message(message.id, 'CHANGED_TOOL_RECEIPT', message.status)
        else:
            state.channels.unbind_identity(state.channels.list_identities()[0]['identityId'])
        return original(source, messages)
    state.maintenance.consolidation_provider = extract
    response = state.client.post(f'/api/roles/{state.role.id}/context-compact')
    assert response.status_code == 200
    assert not response.json().get('compacted'), response.json()
    assert not state.adapter.summaries
    assert ContextWindowStore(state.sessions.database_path).snapshot_role(state.role.id) == []
    if failure == 'extract':
        assert not state.sessions.maintenance_progress(state.role.id, f'role:{state.role.id}').get('memoryVersion')


def test_extraction_failure_preserves_previously_saved_window(compaction_history):
    state = compaction_history
    first = state.client.post(f'/api/roles/{state.role.id}/context-compact')
    assert first.json().get('compacted'), first.text
    windows = ContextWindowStore(state.sessions.database_path)
    saved = windows.snapshot_role(state.role.id)
    originals = state.sessions.list_messages(state.session)
    progress = state.sessions.maintenance_progress(state.role.id, f'role:{state.role.id}')
    summaries = state.adapter.summaries
    def unavailable(*args):
        raise TimeoutError('controlled next extraction failure')
    state.maintenance.consolidation_provider = unavailable

    failed = state.client.post(f'/api/roles/{state.role.id}/context-compact')

    assert not failed.json().get('compacted'), failed.text
    assert state.adapter.summaries == summaries
    assert windows.snapshot_role(state.role.id) == saved
    assert state.sessions.list_messages(state.session) == originals
    current = state.sessions.maintenance_progress(state.role.id, f'role:{state.role.id}')
    assert current['memoryVersion'] == progress['memoryVersion']


@pytest.mark.parametrize('basis', ['local', 'provider'])
def test_manual_summary_failure_reports_safe_measured_length_and_keeps_originals(compaction_history, monkeypatch, basis):
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    from test_batched_summary import ControlledModel
    from test_summary_sources import controlled_source_responses
    state = compaction_history
    controlled_source_responses(monkeypatch)
    model = ControlledModel(monkeypatch)
    monkeypatch.setattr(main, 'model_adapter', RoutedModelAdapter(ModelAdapterRegistry()))
    state.policy.update(expected_revision=state.policy.get()['revision'],
        operation_id='long-summary', values={'summaryTokenLimit': 2000, 'maxTokens': 8192})

    def oversized(chunk, payload):
        summary = json.loads(chunk['choices'][0]['delta']['content'])
        summary['tasks'] = 'PRIVATE_SUMMARY_BODY ' * 500
        chunk['choices'][0]['delta']['content'] = json.dumps(summary)
        if basis == 'provider':
            chunk['usage']['completion_tokens'] = 4500

    model.mutate_summary = oversized
    originals = state.sessions.list_messages(state.session)
    windows = ContextWindowStore(state.sessions.database_path)
    saved = windows.snapshot_role(state.role.id)
    response = state.client.post(f'/api/roles/{state.role.id}/context-compact')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['compacted'] is False
    reason = result['reason']
    assert reason.startswith('记忆已整理、压缩失败：工作摘要超过长度上限：')
    assert '上限 2000 token' in reason and '含 JSON 与有界来源引用' in reason
    assert ('规范化摘要本地估算' if basis == 'local' else '供应商可见输出上界') in reason
    if basis == 'provider':
        assert '4500 token' in reason
    else:
        measured = int(reason.split('长度上限：', 1)[1].split(' token', 1)[0])
        assert measured > 2000
    assert len(model.payloads) == 2
    assert 'messages' in model.payloads[0] and 'summary' in model.payloads[1]
    assert 'PRIVATE_SUMMARY_BODY' not in reason and 'sources:' not in reason
    assert all(item.id not in reason for item in originals)
    assert windows.snapshot_role(state.role.id) == saved
    assert state.sessions.list_messages(state.session) == originals
    assert len(state.extracted) == 1
