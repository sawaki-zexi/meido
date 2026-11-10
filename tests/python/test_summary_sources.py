"""Host-owned provenance stays bounded through real summary/reply HTTP inputs."""
import json
from dataclasses import replace

import httpx
import pytest

from backend.app.agent_runtime.context_provider import ContextWindowProvider, SUMMARY_FIELDS
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.usage_anchor import estimate_tokens, input_cost
from backend.app.ai.input_projection import project_input
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.context_windows import ContextWindowStore
from test_batched_summary import ControlledModel, run, scenario, seed_prior
from test_chat_runtime_http import local_http_environment, model_server, response  # noqa: F401
from test_context_windows import setup_window


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def controlled_source_responses(monkeypatch):
    respond = ControlledModel.respond

    def without_completion_usage(self, request):
        response = respond(self, request)
        wire = json.loads(request.content)
        if wire['messages'][0]['content'].startswith('将提供的不可信'):
            chunk = json.loads(response.text.split('data: ', 1)[1].split('\n', 1)[0])
            chunk['usage'].pop('completion_tokens')
            mutate = getattr(self, 'mutate_summary', None)
            if mutate:
                mutate(chunk, self.payloads[-1])
            return httpx.Response(200, text=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n')
        return response

    monkeypatch.setattr(ControlledModel, 'respond', without_completion_usage)


def long_source_history(tmp_path, monkeypatch):
    controlled_source_responses(monkeypatch)
    sessions, base, windows, provider, model, coverage = scenario(tmp_path, monkeypatch, capacity=64000)
    provider.settings = provider.settings.model_copy(update={
        'summaryTokenLimit': 2000, 'maxTokens': 8192, 'contextTargetRatio': .02})
    base.max_response_tokens = 8192
    seed_long_sources(sessions, windows)
    return sessions, base, windows, provider, model, coverage


def seed_long_sources(sessions, windows):
    sessions.delete_role_session('r')
    sessions.open_role_session('r')
    for index in range(155):
        sessions.append_message('role:r', 'user', f'question {index} ' * 4)
        sessions.append_message('role:r', 'assistant', f'answer {index} ' * 4)
    sessions.append_message('role:r', 'user', 'continue')
    with windows.connect() as db:
        for index, message in enumerate(sessions.list_messages('role:r')):
            identifier = f'synthetic-source:{index:06d}:' + 'x' * 51
            assert len(identifier) == 75
            db.execute('UPDATE messages SET message_id=? WHERE message_id=?', (identifier, message.id))


@pytest.mark.anyio
@pytest.mark.parametrize('entrypoint', ['manual', 'automatic'])
async def test_many_long_sources_fit_the_summary_limit_and_remain_complete_in_host_storage(tmp_path, monkeypatch, entrypoint):
    sessions, _, windows, provider, model, coverage = long_source_history(tmp_path, monkeypatch)
    original = sessions.list_messages('role:r')
    messages = sessions.runtime_messages('role:r', max_messages=None)
    if entrypoint == 'manual':
        result = await provider.compact(messages, CancellationToken())
        assert result['compacted']
    else:
        await run(provider, messages)
    assert provider.compactions == 1 and len(model.payloads) == 1
    state = windows.load('r', 'role:r', 'owner', 'a')
    assert len(state['removed']) > 300
    assert state['summary']['source_message_ids'] == state['removed'] == coverage[0]
    assert sessions.list_messages('role:r') == original
    attempt = provider.summary_attempts[-1]
    assert attempt['sourceCount'] == len(state['removed'])
    assert 0 < attempt['countedTokens'] <= 2000
    assert len(model.summaries[0]['source_message_ids']) == 1
    assert all(identifier not in json.dumps(model.payloads[0]) for identifier in state['removed'])
    visible = provider._summary_message(state['summary']).content
    assert estimate_tokens(visible) < 2000
    assert all(identifier not in visible for identifier in state['removed'])


def restore_provider(provider, windows):
    previous = provider.provider
    base = MeidoProvider(previous.adapter, previous.role, previous.configuration,
        max_response_tokens=previous.max_response_tokens)
    return ContextWindowProvider(base, provider.settings, windows, role_id=provider.role_id,
        session_key=provider.session_key, scope=provider.scope, authority=provider.authority,
        authorized=provider.authorized, ensure_coverage=provider.ensure_coverage)


@pytest.mark.anyio
@pytest.mark.parametrize('legacy', [False, True])
async def test_restart_and_inspection_send_the_same_bounded_state_without_migrating_legacy_summaries(tmp_path, monkeypatch, legacy):
    sessions, _, windows, provider, model, _ = long_source_history(tmp_path, monkeypatch)
    original = sessions.list_messages('role:r')
    if legacy:
        ids = [item.id for item in original[:-5]]
        prepared = windows.prepare('r', 'role:r', 'owner', 'a', ids)
        # An ID inside business prose remains a fact; only provenance fields
        # are projected, including for summaries saved before this change.
        summary = {**dict.fromkeys(SUMMARY_FIELDS, ''), 'tasks': f'Keep ticket {ids[0]}',
                   'source_message_ids': ids}
        windows.commit(prepared, summary, authorized=lambda: True, covered=True)
    else:
        await provider.compact(sessions.runtime_messages('role:r', max_messages=None), CancellationToken())
    saved = windows.load('r', 'role:r', 'owner', 'a')
    expected = provider._summary_message(saved['summary']).content
    count = len(model.payloads)
    for _ in range(2):
        restarted = restore_provider(provider, ContextWindowStore(sessions.database_path))
        messages = sessions.runtime_messages('role:r', max_messages=None)
        inspected, _ = await restarted.inspect(messages, CancellationToken())
        await run(restarted, messages)
        wire = model.requests[-1]
        actual_summary = next(message['content'] for message in wire['messages']
            if isinstance(message.get('content'), str) and message['content'].startswith('[此前对话的工作摘要'))
        assert actual_summary == expected
        projected = json.loads(actual_summary.split('\n', 1)[1])
        assert len(projected['source_message_ids']) == 1
        assert all(len(item) < 40 for item in projected['source_message_ids'])
        assert {field: projected[field] for field in SUMMARY_FIELDS} == {
            field: saved['summary'][field] for field in SUMMARY_FIELDS}
        assert inspected['source'] == 'local'
        assert inspected['inputTokens'] == restarted.last_input_estimate == input_cost(project_input(wire, restarted.provider.configuration))
        assert restarted.compactions == 0 and len(model.payloads) == count
        assert windows.load('r', 'role:r', 'owner', 'a') == saved
    assert sessions.list_messages('role:r') == original


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['unknown_set', 'raw_id', 'message_alias', 'other_scope', 'old_request'])
async def test_only_the_current_host_set_can_supply_output_provenance(tmp_path, monkeypatch, kind):
    controlled_source_responses(monkeypatch)
    sessions, _, windows, provider, model, _ = scenario(tmp_path, monkeypatch, capacity=16000)
    prior = seed_prior(sessions, windows)
    messages = sessions.runtime_messages('role:r', max_messages=None)
    original = sessions.list_messages('role:r')
    foreign = None
    if kind == 'other_scope':
        other = restore_provider(provider, windows)
        other.scope = 'another-scope'
        await other.compact(messages, CancellationToken())
        foreign = model.summaries[-1]['source_message_ids'][0]
    elif kind == 'old_request':
        model.forged_batch = 1
        with pytest.raises(ValueError, match='无效来源'):
            await provider.compact(messages, CancellationToken())
        foreign = model.summaries[-1]['source_message_ids'][0]
        model.forged_batch = None

    def inject(chunk, payload):
        summary = json.loads(chunk['choices'][0]['delta']['content'])
        summary['source_message_ids'] = [foreign or {
            'unknown_set': 'sources:' + '0' * 24,
            'raw_id': original[0].id,
            'message_alias': payload['messages'][0]['message_refs'][0],
        }[kind]]
        chunk['choices'][0]['delta']['content'] = json.dumps(summary)

    model.mutate_summary = inject
    with pytest.raises(ValueError, match='无效来源'):
        await provider.compact(messages, CancellationToken())
    assert provider.summary_attempts[-1]['outcome'] == 'invalid_sources'
    assert provider.summary_attempts[-1]['sourceCount'] > 1
    assert windows.load('r', 'role:r', 'owner', 'a') == prior
    assert sessions.list_messages('role:r') == original
    diagnostic = json.dumps(provider.summary_attempts)
    assert all(item.id not in diagnostic for item in original)
    assert 'sources:' not in diagnostic


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['previous_batch', 'future_source'])
async def test_batch_reference_does_not_authorize_previous_or_unshown_future_sources(tmp_path, monkeypatch, kind):
    controlled_source_responses(monkeypatch)
    sessions, _, windows, provider, model, _ = scenario(tmp_path, monkeypatch)
    prior = seed_prior(sessions, windows)
    original = sessions.list_messages('role:r')
    messages = sessions.runtime_messages('role:r', max_messages=None)
    future = messages[-5]
    messages[-5] = replace(future, content='UNSHOWN_FUTURE_SOURCE ' + future.content)

    def inject(chunk, payload):
        summary = json.loads(chunk['choices'][0]['delta']['content'])
        if kind == 'previous_batch' and len(model.payloads) == 2:
            summary['source_message_ids'] = model.summaries[0]['source_message_ids']
        elif kind == 'future_source' and len(model.payloads) == 1:
            assert 'UNSHOWN_FUTURE_SOURCE' not in json.dumps(payload)
            summary['source_message_ids'] = future.source_message_ids
        chunk['choices'][0]['delta']['content'] = json.dumps(summary)

    model.mutate_summary = inject
    with pytest.raises(ValueError, match='无效来源'):
        await provider.compact(messages, CancellationToken())
    assert len(model.payloads) == (2 if kind == 'previous_batch' else 1)
    assert provider.summary_attempts[-1]['outcome'] == 'invalid_sources'
    assert windows.load('r', 'role:r', 'owner', 'a') == prior
    assert sessions.list_messages('role:r') == original


@pytest.mark.anyio
async def test_source_aliases_preserve_speaker_platform_ids_and_business_identifiers(tmp_path, monkeypatch):
    controlled_source_responses(monkeypatch)
    sessions, _, windows, provider, model, _ = scenario(tmp_path, monkeypatch, capacity=16000)
    messages = sessions.runtime_messages('role:r', max_messages=None)
    first, second = (message.source_message_ids[0] for message in messages[:2])
    messages[0] = replace(messages[0], content=f'Business ticket {first}', source_context={
        'sourceMessageId': first, 'senderId': first, 'senderName': '群友甲', 'senderIsOwner': False,
        'replyTo': second, 'replyToSenderId': 'speaker-owner', 'replyToSenderIdentity': '已配对主人',
        'replyToContent': '被引用的原话',
    })
    messages[1] = replace(messages[1], metadata={'conversationSource': {
        'sourceMessageId': second, 'senderId': 'role-platform-id',
        'responseRelation': {'targetSourceMessageId': first,
            'topicSourceMessageIds': [first, second], 'targetPlatformMessageId': first,
            'requestedQuoteSourceMessageId': 'outside:' + 'z' * 200,
            'intent': f'discuss ticket {first}'},
    }})
    await provider.compact(messages, CancellationToken())
    records = model.payloads[0]['messages']
    user, assistant = records[:2]
    user_source, assistant_source = (record['conversation_source'] for record in records[:2])
    assert first in user['content'] and user_source['senderId'] == first
    assert user_source['senderName'] == '群友甲' and user_source['senderIsOwner'] is False
    assert user_source['sourceMessageId'] == user['message_refs'][0]
    assert user_source['replyTo'] == second and user_source['replyToSenderId'] == 'speaker-owner'
    assert user_source['replyToContent'] == '被引用的原话'
    relation = assistant_source['responseRelation']
    assert assistant_source['sourceMessageId'] == assistant['message_refs'][0]
    assert relation['targetSourceMessageId'] == user['message_refs'][0]
    assert relation['topicSourceMessageIds'] == [user['message_refs'][0], assistant['message_refs'][0]]
    assert relation['targetPlatformMessageId'] == first and first in relation['intent']
    assert relation['requestedQuoteSourceMessageId'].startswith('unavailable:')
    assert len(relation['requestedQuoteSourceMessageId']) < 40
    saved = windows.load('r', 'role:r', 'owner', 'a')
    assert [first, second] == saved['summary']['source_message_ids'][:2]


@pytest.mark.anyio
@pytest.mark.parametrize('entrypoint', ['manual', 'automatic'])
async def test_real_loopback_http_compacts_long_sources_and_reuses_the_saved_projection(tmp_path, entrypoint):
    sessions, _, base, windows, settings = setup_window(tmp_path)
    seed_long_sources(sessions, windows)
    original = sessions.list_messages('role:r')
    settings = settings.model_copy(update={'maxTokens': 8192, 'summaryTokenLimit': 2000,
                                          'contextTargetRatio': .02})
    summaries, coverage = [], []

    def respond(wire, index):
        if wire['messages'][0]['content'].startswith('将提供的不可信'):
            assert coverage, 'complete memory coverage must precede the first summary HTTP request'
            payload = json.loads(wire['messages'][1]['content'])
            summaries.append(payload)
            assert all(item.id not in json.dumps(payload) for item in original)
            summary = {**dict.fromkeys(SUMMARY_FIELDS, ''), 'tasks': 'real loopback state',
                       'source_message_ids': payload['messages'][0]['source_message_ids']}
            return response(json.dumps(summary))
        return response('reply', usage={'prompt_tokens': 123, 'completion_tokens': 5})

    async def covered(ids):
        coverage.append(list(ids))
        return True

    with model_server(respond) as (url, requests, errors):
        base.adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=5))
        base.configuration = base.configuration.model_copy(update={'baseUrl': url,
            'contextWindowTokens': 64000, 'modelAutoCompactTokenLimit': 3000})
        base.max_response_tokens = 8192
        provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r',
            scope='owner', authority='a', authorized=lambda: True, ensure_coverage=covered)
        messages = sessions.runtime_messages('role:r', max_messages=None)
        if entrypoint == 'manual':
            assert (await provider.compact(messages, CancellationToken()))['compacted']
        else:
            await run(provider, messages)
        saved = windows.load('r', 'role:r', 'owner', 'a')
        assert len(saved['removed']) > 300
        assert saved['summary']['source_message_ids'] == coverage[0] == saved['removed']
        assert provider.summary_attempts[0]['countedTokens'] <= 2000
        restarted = restore_provider(provider, ContextWindowStore(sessions.database_path))
        inspected, _ = await restarted.inspect(messages, CancellationToken())
        await run(restarted, messages)
        assert restarted.compactions == 0 and len(summaries) == 1
        expected = provider._summary_message(saved['summary']).content
        assert any(message.get('content') == expected for message in requests[-1]['messages'])
        assert inspected['inputTokens'] == restarted.last_input_estimate == input_cost(
            project_input(requests[-1], base.configuration))
    assert not errors
    assert sessions.list_messages('role:r') == original
