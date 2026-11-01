"""Summary acceptance and retry decisions through the actual provider wire."""
import json

import httpx
import pytest

from backend.app.agent_runtime.context_provider import SUMMARY_FIELDS
from test_batched_summary import ControlledModel, run, scenario, seed_prior


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def summary_case(tmp_path, monkeypatch, usages, *, thinking=True, mutate=None):
    respond = ControlledModel.respond

    def controlled(self, request):
        response = respond(self, request)
        wire = json.loads(request.content)
        if not wire['messages'][0]['content'].startswith('将提供的不可信'):
            return response
        chunk = json.loads(response.text.split('data: ', 1)[1].split('\n', 1)[0])
        chunk['usage'] = usages[min(len(self.payloads) - 1, len(usages) - 1)]
        if thinking:
            chunk['choices'][0]['delta']['reasoning_content'] = 'observed thinking'
        if mutate:
            mutate(chunk, self)
        return httpx.Response(200, text=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n')

    monkeypatch.setattr(ControlledModel, 'respond', controlled)
    sessions, base, windows, provider, model, coverage = scenario(tmp_path, monkeypatch, capacity=32000)
    provider.settings = provider.settings.model_copy(update={'summaryTokenLimit': 2000, 'maxTokens': 8192, 'contextTargetRatio': .05})
    base.max_response_tokens = 8192
    return sessions, windows, provider, model


@pytest.mark.anyio
async def test_reasoning_does_not_trigger_an_unneeded_shortening_request(tmp_path, monkeypatch):
    sessions, windows, provider, model = summary_case(tmp_path, monkeypatch, [
        {'completion_tokens': 2500, 'completion_tokens_details': {'reasoning_tokens': 1500}}])
    await run(provider, sessions.runtime_messages('role:r'))
    assert len(model.payloads) == 1
    assert provider.compactions == 1 and windows.load('r', 'role:r', 'owner', 'a')['removed']
    attempt = provider.summary_attempts[0]
    assert attempt['countedTokens'] == 1000 and attempt['countSource'] == 'provider_output_upper_bound'
    assert attempt['outcome'] == 'accepted'


@pytest.mark.anyio
async def test_only_legal_oversized_state_gets_one_measured_shortening_request(tmp_path, monkeypatch):
    sessions, _, provider, model = summary_case(tmp_path, monkeypatch, [
        {'completion_tokens': 4500, 'completion_tokens_details': {'reasoning_tokens': 2000}},
        {'completion_tokens': 500, 'completion_tokens_details': {'reasoning_tokens': 0}}])
    await run(provider, sessions.runtime_messages('role:r'))
    assert len(model.payloads) == 2 and provider.compactions == 1
    shortened = model.payloads[1]
    assert shortened['summary'] == model.summaries[0]
    assert 'messages' not in shortened and 'previous_summary' not in shortened
    assert shortened['budget_feedback']['counted_tokens'] == 2500
    assert shortened['budget_feedback']['count_source'] == 'provider_output_upper_bound'
    assert shortened['budget_feedback']['hard_limit'] == 2000
    assert shortened['budget_feedback']['instruction']
    for wire in model.requests[:2]:
        prompt = wire['messages'][0]['content']
        template = json.loads(prompt[prompt.index('{'):prompt.index('}') + 1])
        assert set(template) == {*SUMMARY_FIELDS, 'source_message_ids'}
        assert all(isinstance(template[field], str) for field in SUMMARY_FIELDS)
        assert isinstance(template['source_message_ids'], list) and template['source_message_ids']
    assert [attempt['outcome'] for attempt in provider.summary_attempts] == ['oversized', 'accepted']


@pytest.mark.anyio
@pytest.mark.parametrize('usage,reason', [
    ({}, 'missing_completion_usage'),
    ({'completion_tokens': 2500}, 'missing_reasoning_usage'),
    ({'completion_tokens': True}, 'invalid_completion_usage'),
    ({'completion_tokens': '2500'}, 'invalid_completion_usage'),
    ({'completion_tokens': 0}, 'invalid_completion_usage'),
    ({'completion_tokens': 2500, 'completion_tokens_details': {'reasoning_tokens': -1}}, 'invalid_reasoning_usage'),
    ({'completion_tokens': 2500, 'completion_tokens_details': {'reasoning_tokens': True}}, 'invalid_reasoning_usage'),
    ({'completion_tokens': 2500, 'completion_tokens_details': {'reasoning_tokens': 2500}}, 'invalid_reasoning_usage'),
])
async def test_ambiguous_provider_usage_counts_the_normalized_body(tmp_path, monkeypatch, usage, reason):
    sessions, _, provider, model = summary_case(tmp_path, monkeypatch, [usage])
    await run(provider, sessions.runtime_messages('role:r'))
    assert len(model.payloads) == 1 and provider.compactions == 1
    attempt = provider.summary_attempts[0]
    assert attempt['countSource'] == 'local_normalized_estimate'
    assert attempt['fallbackReason'] == reason
    assert 0 < attempt['countedTokens'] == attempt['localTokens'] <= 2000


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['invalid_json', 'missing_fields', 'invalid_fields', 'invalid_sources', 'truncated'])
async def test_invalid_summary_never_retries_and_keeps_its_safe_failure_reason(tmp_path, monkeypatch, kind):
    def mutate(chunk, model):
        summary = dict(model.summaries[-1])
        if kind == 'invalid_json':
            text = '{not json'
        else:
            if kind == 'missing_fields':
                summary.pop('tasks')
            elif kind == 'invalid_fields':
                summary['tasks'] = 123
            elif kind == 'invalid_sources':
                summary['source_message_ids'] = ['PRIVATE_SOURCE_ID']
            elif kind == 'truncated':
                chunk['choices'][0]['finish_reason'] = 'length'
            text = json.dumps(summary)
        chunk['choices'][0]['delta']['content'] = text

    sessions, windows, provider, model = summary_case(tmp_path, monkeypatch,
        [{'completion_tokens': 4500, 'completion_tokens_details': {'reasoning_tokens': 0}}], mutate=mutate)
    original = sessions.list_messages('role:r')
    await run(provider, sessions.runtime_messages('role:r'))
    assert len(model.payloads) == 1 and provider.compactions == 0
    assert sessions.list_messages('role:r') == original
    assert not windows.load('r', 'role:r', 'owner', 'a')['removed']
    assert provider.summary_attempts[0]['outcome'] == kind
    assert 'PRIVATE_SOURCE_ID' not in json.dumps(provider.summary_attempts)


@pytest.mark.anyio
async def test_removed_metadata_and_duplicate_sources_are_not_charged_to_the_normalized_state(tmp_path, monkeypatch):
    def project(chunk, model):
        raw = {**model.summaries[-1], 'notes': 'discard this metadata' * 1000}
        raw['source_message_ids'] *= 2
        chunk['choices'][0]['delta']['content'] = json.dumps(raw)
    sessions, windows, provider, model = summary_case(tmp_path, monkeypatch,
        [{'completion_tokens': 8000, 'completion_tokens_details': {'reasoning_tokens': 500}}], mutate=project)
    await run(provider, sessions.runtime_messages('role:r'))
    assert len(model.payloads) == 1 and provider.compactions == 1
    assert set(windows.load('r', 'role:r', 'owner', 'a')['summary']) == {*SUMMARY_FIELDS, 'source_message_ids'}
    assert provider.summary_attempts[0]['countSource'] == 'local_normalized_estimate'
    assert provider.summary_attempts[0]['fallbackReason'] == 'normalized_projection'


@pytest.mark.anyio
async def test_second_oversized_result_stops_without_replacing_the_old_window(tmp_path, monkeypatch):
    sessions, windows, provider, model = summary_case(tmp_path, monkeypatch,
        [{'completion_tokens': 4500, 'completion_tokens_details': {'reasoning_tokens': 0}}])
    prior = seed_prior(sessions, windows)
    await run(provider, sessions.runtime_messages('role:r'))
    assert len(model.payloads) == 2 and provider.compactions == 0
    assert [attempt['outcome'] for attempt in provider.summary_attempts] == ['oversized', 'oversized']
    assert windows.load('r', 'role:r', 'owner', 'a') == prior


@pytest.mark.anyio
async def test_summary_timeout_is_classified_without_retry_or_partial_commit(tmp_path, monkeypatch):
    import asyncio
    from backend.app.agent_runtime import context_provider
    sessions, windows, provider, model = summary_case(tmp_path, monkeypatch, [{}])
    monkeypatch.setattr(context_provider, 'SUMMARY_TIMEOUT_SECONDS', .01)
    calls = []
    stream = provider.provider.adapter.stream_rich_messages
    async def delayed(messages, configuration, tools=(), **options):
        if messages[0].content[0].text.startswith('将提供的不可信'):
            calls.append('summary')
            await asyncio.sleep(1)
        async for event in stream(messages, configuration, tools, **options):
            yield event
    monkeypatch.setattr(provider.provider.adapter, 'stream_rich_messages', delayed)
    prior = windows.load('r', 'role:r', 'owner', 'a')
    await run(provider, sessions.runtime_messages('role:r'))
    assert calls == ['summary'] and len(model.requests) == 1  # only the ordinary reply reached HTTP
    assert provider.summary_attempts[0]['outcome'] == 'timeout'
    assert windows.load('r', 'role:r', 'owner', 'a') == prior


@pytest.mark.anyio
@pytest.mark.parametrize('terminal', ['missing', 'null', 'not_object', 'empty', 'no_delta'])
async def test_anthropic_provisional_usage_never_accepts_an_oversized_summary(tmp_path, monkeypatch, terminal):
    from backend.app.agent_runtime.context_provider import ContextWindowProvider
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    from test_context_windows import setup_window
    from test_output_usage import anthropic_without_thinking

    sessions, _, base, windows, settings = setup_window(tmp_path)
    base.adapter = RoutedModelAdapter(ModelAdapterRegistry())
    base.configuration = base.configuration.model_copy(update={'api': 'anthropic-messages',
        'baseUrl': 'https://model.test', 'contextWindowTokens': 32000, 'modelAutoCompactTokenLimit': 3000})
    settings = settings.model_copy(update={'summaryTokenLimit': 2000, 'maxTokens': 8192, 'contextTargetRatio': .05})
    base.max_response_tokens = 8192
    prior = windows.load('r', 'role:r', 'owner', 'a')
    payloads = []

    def respond(request):
        wire = json.loads(request.content)
        if not wire['system'].startswith('将提供的不可信'):
            return httpx.Response(200, text=anthropic_without_thinking('reply', 'valid'))
        assert windows.load('r', 'role:r', 'owner', 'a') == prior
        payload = json.loads(wire['messages'][0]['content'][0]['text'])
        payloads.append(payload)
        identifier = (payload['summary']['source_message_ids'][0] if 'summary' in payload
                      else payload['messages'][0]['source_message_ids'][0])
        summary = {**dict.fromkeys(SUMMARY_FIELDS, ''), 'tasks': 'x' * 7000 if len(payloads) == 1 else 'kept',
                   'source_message_ids': [identifier]}
        return httpx.Response(200, text=anthropic_without_thinking(json.dumps(summary), terminal if len(payloads) == 1 else 'valid'))

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    async def coverage(ids):
        return True
    provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r',
        scope='owner', authority='a', authorized=lambda: True, ensure_coverage=coverage)
    await run(provider, sessions.runtime_messages('role:r'))
    if terminal == 'no_delta':
        assert len(payloads) == 1 and provider.compactions == 0
        assert provider.summary_attempts[0]['outcome'] == 'stream_error'
        assert windows.load('r', 'role:r', 'owner', 'a') == prior
    else:
        assert len(payloads) == 2 and provider.compactions == 1
        first = provider.summary_attempts[0]
        assert first['countedTokens'] == first['localTokens'] > 2000
        assert first['countSource'] == 'local_normalized_estimate'
        assert first['fallbackReason'] == 'missing_completion_usage' and first['outcome'] == 'oversized'
        assert payloads[1]['budget_feedback']['count_source'] == 'local_normalized_estimate'
        assert payloads[1]['budget_feedback']['counted_tokens'] == first['countedTokens']
        assert provider.summary_attempts[1]['outcome'] == 'accepted'
        assert windows.load('r', 'role:r', 'owner', 'a')['summary']['tasks'] == 'kept'
