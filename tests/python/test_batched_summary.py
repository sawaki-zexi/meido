"""Long histories fold through the real model protocol without partial commits."""
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from backend.app.agent_runtime.context_provider import ContextWindowProvider, SUMMARY_FIELDS, budget
from backend.app.agent_runtime import CapabilityRegistry, ToolDefinition, AgentToolResult
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.types import AttachmentContent, ImageContent, AssistantMessage, ToolCall, ToolResultMessage
from backend.app.agent_runtime.usage_anchor import input_cost
from backend.app.agent_runtime.summary_batches import next_summary_batch
from backend.app.ai.input_projection import project_input
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from test_context_windows import setup_window


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class ControlledModel:
    def __init__(self, monkeypatch):
        self.requests = []
        self.payloads = []
        self.summaries = []
        self.on_summary = lambda payload: None
        self.on_reply = lambda: None
        self.fail_batch = None
        self.forged_batch = None
        self.oversized = False
        self.reply_usage = 123
        self.tool_state = ''
        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
            **kwargs, transport=httpx.MockTransport(self.respond)))

    def respond(self, request):
        wire = json.loads(request.content)
        self.requests.append(wire)
        is_summary = wire['messages'][0]['content'].startswith('将提供的不可信')
        if is_summary:
            payload = json.loads(wire['messages'][1]['content'])
            self.payloads.append(payload)
            self.on_summary(payload)
            previous = payload.get('previous_summary') or payload.get('summary') or {}
            identifiers = [*previous.get('source_message_ids', []),
                *(identifier for record in payload.get('messages', []) for identifier in record['source_message_ids'])]
            summary = {**dict.fromkeys(SUMMARY_FIELDS, ''), 'tasks': f'validated state {len(self.payloads)}',
                       'tool_state': self.tool_state,
                       'source_message_ids': list(dict.fromkeys(identifiers))}
            if self.forged_batch == len(self.payloads):
                summary['source_message_ids'].append('foreign-role:forged')
            self.summaries.append(summary)
            text = '{}' if self.fail_batch == len(self.payloads) else json.dumps(summary)
            usage = 700 + len(self.payloads)
        else:
            self.on_reply()
            text, usage = 'reply', self.reply_usage
        chunk = {'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': usage, 'completion_tokens':
                    900 if is_summary and self.oversized and 'messages' in payload else 100}}
        return httpx.Response(200, text=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n')


def scenario(tmp_path, monkeypatch, *, capacity=4000):
    sessions, _, base, windows, settings = setup_window(tmp_path)
    base.adapter = RoutedModelAdapter(ModelAdapterRegistry())
    base.configuration = base.configuration.model_copy(update={
        'baseUrl': 'https://model.test', 'contextWindowTokens': capacity,
        'modelAutoCompactTokenLimit': 3000})
    settings = settings.model_copy(update={'contextTargetRatio': .1})
    model = ControlledModel(monkeypatch)
    coverage_calls = []

    async def coverage(ids):
        coverage_calls.append(list(ids))
        return True

    provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r',
        scope='owner', authority='a', authorized=lambda: True, ensure_coverage=coverage)
    return sessions, base, windows, provider, model, coverage_calls


async def run(provider, messages, *, signal=None, tools=()):
    return [event async for event in provider.stream_response(model='fixture', system='',
        messages=messages, tools=tools, signal=signal or CancellationToken())]


def seed_prior(sessions, windows):
    identifiers = [message.id for message in sessions.list_messages('role:r')[:2]]
    prepared = windows.prepare('r', 'role:r', 'owner', 'a', identifiers)
    summary = {**dict.fromkeys(SUMMARY_FIELDS, ''), 'tasks': 'previous lawful task',
               'source_message_ids': identifiers}
    windows.commit(prepared, summary, authorized=lambda: True, covered=True)
    return windows.load('r', 'role:r', 'owner', 'a')


@pytest.mark.anyio
@pytest.mark.parametrize('capacity', [16000, 4000])
@pytest.mark.parametrize('has_prior', [False, True])
async def test_history_folds_in_longest_ordered_batches_and_commits_once(tmp_path, monkeypatch, capacity, has_prior):
    sessions, base, windows, provider, model, coverage = scenario(tmp_path, monkeypatch, capacity=capacity)
    original = sessions.list_messages('role:r')
    prior = seed_prior(sessions, windows) if has_prior else windows.load('r', 'role:r', 'owner', 'a')
    commits = []
    commit = windows.commit

    def record_commit(*args, **kwargs):
        commits.append(args[1])
        return commit(*args, **kwargs)

    monkeypatch.setattr(windows, 'commit', record_commit)
    model.on_summary = lambda payload: unchanged()

    def unchanged():
        assert windows.load('r', 'role:r', 'owner', 'a') == prior

    await run(provider, sessions.runtime_messages('role:r', max_messages=None))
    assert provider.compactions == 1
    assert not provider.compaction_failed and not provider.degraded
    assert len(model.payloads) == 1 if capacity == 16000 else len(model.payloads) > 1
    state = windows.load('r', 'role:r', 'owner', 'a')
    assert state['version'] == prior['version'] + 1 and commits == [state['summary']]
    assert coverage == [state['removed']]
    assert sessions.list_messages('role:r') == original
    records = [record for payload in model.payloads for record in payload['messages']]
    assert [*prior['removed'], *(identifier for record in records for identifier in record['source_message_ids'])] == state['removed']
    assert state['removed'] == [message.id for message in original[:len(state['removed'])]]
    assert state['summary']['source_message_ids'] == state['removed']
    assert model.payloads[0]['previous_summary'] == prior['summary']
    limit = budget(base.configuration, provider.settings, provider.settings.maxTokens)[0]
    consumed = 0
    for index, payload in enumerate(model.payloads):
        wire = model.requests[index]
        assert wire['response_format'] == {'type': 'json_object'}
        assert not wire.get('tools')
        assert input_cost(project_input(wire, base.configuration)) <= limit
        if index:
            assert payload['previous_summary'] == model.summaries[index - 1]
        consumed += len(payload['messages'])
        if consumed < len(records):
            extended = deepcopy(wire)
            extended_payload = deepcopy(payload)
            extended_payload['messages'].append(records[consumed])
            extended['messages'][1]['content'] = json.dumps(extended_payload, ensure_ascii=False)
            assert input_cost(project_input(extended, base.configuration)) > limit
    assert provider.requests == len(model.payloads) + 1 == len(model.requests)
    assert [attempt['usage']['input_tokens'] for attempt in provider.summary_attempts] == [
        701 + index for index in range(len(model.payloads))]
    assert all(attempt['elapsedMs'] >= 0 for attempt in provider.summary_attempts)
    assert [attempt['batch'] for attempt in provider.summary_attempts] == list(range(1, len(model.payloads) + 1))
    assert all(attempt['inputSource'] == 'local' for attempt in provider.summary_attempts)


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['text', 'attachment', 'tool'])
async def test_single_oversized_source_shrinks_text_but_keeps_ids_head_tail_and_media_references(tmp_path, monkeypatch, kind):
    sessions, _, windows, provider, model, _ = scenario(tmp_path, monkeypatch)
    sessions.delete_role_session('r')
    sessions.open_role_session('r')
    long_text = 'IMPORTANT_HEAD ' + 'original data\n' * 5000 + ' IMPORTANT_TAIL'
    first = sessions.append_message('role:r', 'user', long_text if kind == 'text' else 'source follows')
    if kind == 'tool':
        sessions.append_agent_message('role:r', AssistantMessage(tool_calls=(ToolCall('read', 'read_file', {'path': 'fixture'}),)))
        sessions.append_agent_message('role:r', ToolResultMessage('read', 'read_file', long_text))
    sessions.append_message('role:r', 'assistant', 'old answer')
    sessions.append_message('role:r', 'user', 'recent question')
    sessions.append_message('role:r', 'assistant', 'recent answer')
    sessions.append_message('role:r', 'user', 'continue')
    original = sessions.list_messages('role:r')
    messages = sessions.runtime_messages('role:r', max_messages=None)
    messages[0] = replace(messages[0], attachments=(AttachmentContent('source.png', 'image/png',
        image=ImageContent('image/png', 'DO_NOT_SEND_IMAGE_BYTES')),
        AttachmentContent('source.txt', 'text/plain', text=long_text if kind == 'attachment' else 'reference')))
    if kind == 'tool':
        messages[2] = replace(messages[2], images=(ImageContent('image/png', 'DO_NOT_SEND_TOOL_IMAGE_BYTES'),))
    await run(provider, messages)
    assert provider.compactions == 1 and not provider.degraded
    sent = [record for payload in model.payloads for record in payload['messages']]
    text = json.dumps(sent)
    assert 'chars truncated' in text and 'IMPORTANT_HEAD' in text and 'IMPORTANT_TAIL' in text
    assert 'DO_NOT_SEND' not in text and 'source.png' in text
    state = windows.load('r', 'role:r', 'owner', 'a')
    assert first.id in state['removed']
    assert state['removed'] == [identifier for record in sent for identifier in record['source_message_ids']]
    assert sessions.list_messages('role:r') == original


@pytest.mark.anyio
async def test_minimum_source_that_still_cannot_fit_never_calls_summary_or_advances(tmp_path, monkeypatch):
    sessions, _, windows, provider, model, coverage = scenario(tmp_path, monkeypatch)
    original = sessions.list_messages('role:r')
    messages = sessions.runtime_messages('role:r', max_messages=None)
    # Each leaf is already below the upstream shrinking floor. Dropping whole
    # fields or skipping this record would falsely claim all sources were read.
    messages[0] = replace(messages[0], attachments=tuple(
        AttachmentContent(f'part-{index}.txt', 'text/plain', text='x' * 180) for index in range(100)))
    await run(provider, messages)
    assert provider.compaction_failed and provider.degraded
    assert provider.compaction_diagnostic['step'] == 'select_batch'
    assert not model.payloads and len(model.requests) == 1
    assert not coverage and windows.load('r', 'role:r', 'owner', 'a')['version'] == 0
    assert sessions.list_messages('role:r') == original


def test_shrinking_stops_at_upstream_floor_and_never_shortens_source_ids():
    identifier = 'source:' + 'x' * 500
    record = {'content': 'head ' + 'x' * 50000 + ' tail', 'source_message_ids': [identifier]}
    count, payload = next_summary_batch(None, [record], lambda request: len(request['messages'][0]['content']) <= 350)
    assert count == 1 and payload['messages'][0]['source_message_ids'] == [identifier]
    assert 'head ' in payload['messages'][0]['content'] and ' tail' in payload['messages'][0]['content']
    # 10000 // 2 ... reaches 312, then 156 is below the 200-character floor.
    # The line-count header is outside the 312-character head/marker/tail body.
    assert len(payload['messages'][0]['content'].split('\n\n', 1)[1]) == 312
    with pytest.raises(ValueError, match='最低可用原文'):
        next_summary_batch(None, [record], lambda request: len(request['messages'][0]['content']) <= 310)


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['malformed', 'forged', 'cancelled', 'request_error', 'budget'])
async def test_later_failure_preserves_previous_summary_and_raw_history(tmp_path, monkeypatch, failure):
    sessions, _, windows, provider, model, coverage = scenario(tmp_path, monkeypatch)
    prior = seed_prior(sessions, windows)
    original = sessions.list_messages('role:r')
    signal = CancellationToken()
    if failure == 'malformed':
        model.fail_batch = 2
    elif failure == 'forged':
        model.forged_batch = 2
    elif failure == 'budget':
        provider.settings = provider.settings.model_copy(update={'maxIterations': 2})

    def on_summary(payload):
        assert windows.load('r', 'role:r', 'owner', 'a') == prior
        if len(model.payloads) == 2:
            if failure == 'cancelled':
                signal.cancel()
            elif failure == 'request_error':
                raise httpx.ReadTimeout('controlled failure')
    model.on_summary = on_summary
    if failure == 'cancelled':
        with pytest.raises(RuntimeError, match='cancelled'):
            await run(provider, sessions.runtime_messages('role:r', max_messages=None), signal=signal)
        assert len(model.requests) == len(model.payloads) == 2
    else:
        await run(provider, sessions.runtime_messages('role:r', max_messages=None), signal=signal)
        assert provider.compaction_failed and provider.degraded
        assert len(model.payloads) == (1 if failure == 'budget' else 2)
        assert 'previous lawful task' in json.dumps(model.requests[-1])
    assert provider.requests == len(model.requests)
    assert provider.compactions == 0 and not coverage
    assert windows.load('r', 'role:r', 'owner', 'a') == prior
    assert sessions.list_messages('role:r') == original


@pytest.mark.anyio
async def test_later_batch_soft_failure_continues_with_all_original_messages(tmp_path, monkeypatch):
    sessions, base, windows, provider, model, coverage = scenario(tmp_path, monkeypatch, capacity=8000)
    sessions.delete_role_session('r')
    sessions.open_role_session('r')
    # Many short stored messages cost more in the summary's provenance-rich JSON
    # than in ordinary protocol messages, while the ordinary request still fits.
    for _ in range(100):
        sessions.append_message('role:r', 'user', 'question')
        sessions.append_message('role:r', 'assistant', 'answer')
    sessions.append_message('role:r', 'user', 'continue')
    messages = sessions.runtime_messages('role:r', max_messages=None)
    local = base.estimate_request_tokens('', messages, ())
    limit, trigger, _ = budget(base.configuration, provider.settings, provider.settings.maxTokens)
    assert trigger <= local < limit
    model.fail_batch = 2
    await run(provider, messages)
    assert provider.compaction_failed and not provider.degraded
    assert provider.compaction_diagnostic['fallback'] == 'original'
    assert len(model.payloads) == 2
    final = model.requests[-1]['messages']
    assert sum(message.get('content') == 'question' for message in final) == 100
    assert sum(message.get('content') == 'answer' for message in final) == 100
    assert not coverage and windows.load('r', 'role:r', 'owner', 'a')['version'] == 0


@pytest.mark.anyio
async def test_each_oversized_batch_has_only_one_shortening_and_counts_all_requests(tmp_path, monkeypatch):
    sessions, _, windows, provider, model, _ = scenario(tmp_path, monkeypatch)
    model.oversized = True
    await run(provider, sessions.runtime_messages('role:r', max_messages=None))
    assert provider.compactions == 1
    assert len(model.payloads) >= 4 and len(model.payloads) % 2 == 0
    for index in range(0, len(model.payloads), 2):
        assert model.payloads[index + 1] == {'summary': model.summaries[index]}
        if index:
            assert model.payloads[index]['previous_summary'] == model.summaries[index - 1]
        assert [attempt['batch'] for attempt in provider.summary_attempts[index:index + 2]] == [index // 2 + 1] * 2
        assert [attempt['shortening'] for attempt in provider.summary_attempts[index:index + 2]] == [False, True]
    assert provider.requests == len(model.requests) == len(model.payloads) + 1
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] == model.summaries[-1]


@pytest.mark.anyio
@pytest.mark.parametrize('anchored', [False, True])
async def test_auxiliary_batches_do_not_supply_an_ordinary_usage_anchor(tmp_path, monkeypatch, anchored):
    sessions, base, windows, provider, model, _ = scenario(tmp_path, monkeypatch)
    messages = sessions.runtime_messages('role:r', max_messages=None)
    if anchored:
        model.reply_usage = 9000
        await run(base, messages)
        assert base.estimate_request('', messages, ()).source == 'actual'
        assert base.estimate_request_tokens('', messages, ()) == 9000
    observed = []
    model.reply_usage = 123
    model.on_reply = lambda: observed.append(base.last_input_estimate)
    await run(provider, messages)
    assert provider.compactions == 1 and len(model.payloads) > 1
    # The final payload is different history; auxiliary usage (701, 702, ...)
    # cannot become its cost. Only its own ordinary response supplies actual.
    assert len(observed) == 1 and observed[0].source == 'local'
    assert provider.last_input_estimate == input_cost(project_input(model.requests[-1], base.configuration))
    state = windows.load('r', 'role:r', 'owner', 'a')
    visible = [provider._summary_message(state['summary']),
        *(message for message in messages if not set(message.source_message_ids) & set(state['removed']))]
    assert base.estimate_request('', visible, ()).source == 'actual'
    assert base.estimate_request_tokens('', visible, ()) == 123
    assert all(attempt['inputSource'] == 'local' for attempt in provider.summary_attempts)


class FixtureTool:
    def __init__(self, name, *, exposure='direct'):
        self.definition = ToolDefinition(name, 'fixture tool', {'type': 'object'}, exposure=exposure)

    async def execute(self, arguments, context, on_update=None):
        return AgentToolResult('fixture result')


@pytest.mark.anyio
@pytest.mark.parametrize('when', ['summary', 'coverage'])
async def test_final_budget_failure_including_tools_changed_during_coverage_keeps_old_window(tmp_path, monkeypatch, when):
    sessions, _, windows, provider, model, coverage = scenario(tmp_path, monkeypatch)
    prior = seed_prior(sessions, windows)
    original = sessions.list_messages('role:r')
    tool = FixtureTool('current_tool')
    registry = CapabilityRegistry([tool]).resolve(role_id='r', session_key='role:r', run_id='budget')
    provider.bind_capabilities(registry)

    def enlarge():
        tool.definition = replace(tool.definition, description='current schema ' * 3000)

    if when == 'summary':
        def after_summary(payload):
            if len(model.payloads) == 2:
                enlarge()
        model.on_summary = after_summary
    else:
        async def changed_during_coverage(ids):
            coverage.append(list(ids))
            enlarge()
            return True
        provider.ensure_coverage = changed_during_coverage
    with pytest.raises(ValueError, match='输入容量'):
        await run(provider, sessions.runtime_messages('role:r', max_messages=None), tools=registry.provider_schemas())
    assert len(model.payloads) > 1 and len(model.requests) == len(model.payloads)
    assert provider.compactions == 0 and provider.compaction_failed
    assert bool(coverage) is (when == 'coverage')
    assert windows.load('r', 'role:r', 'owner', 'a') == prior
    assert sessions.list_messages('role:r') == original


@pytest.mark.anyio
@pytest.mark.parametrize('gate', ['coverage', 'authority', 'version', 'history'])
async def test_all_batches_still_require_current_sources_and_commit_gates(tmp_path, monkeypatch, gate):
    sessions, _, windows, provider, model, coverage = scenario(tmp_path, monkeypatch)
    prior = seed_prior(sessions, windows)
    original = sessions.list_messages('role:r')
    permitted = True
    provider.authorized = lambda: permitted

    async def before_commit(ids):
        nonlocal permitted
        coverage.append(list(ids))
        if gate == 'authority':
            permitted = False
        elif gate == 'version':
            preparation = windows.prepare('r', 'role:r', 'owner', 'a', prior['removed'])
            windows.commit(preparation, prior['summary'], authorized=lambda: True, covered=True)
        elif gate == 'history':
            # Rewrite a newly summarized source after generation, before CAS.
            with windows.connect() as db:
                db.execute('UPDATE messages SET content=? WHERE message_id=?', ('edited source', ids[2]))
        return gate != 'coverage'
    provider.ensure_coverage = before_commit
    if gate == 'authority':
        with pytest.raises(ValueError, match='来源许可'):
            await run(provider, sessions.runtime_messages('role:r', max_messages=None))
    else:
        await run(provider, sessions.runtime_messages('role:r', max_messages=None))
        assert provider.compaction_failed
    assert len(model.payloads) > 1 and len(coverage) == 1
    assert provider.compactions == 0
    state = windows.load('r', 'role:r', 'owner', 'a')
    assert state['summary'] == prior['summary'] and state['removed'] == prior['removed']
    assert state['version'] == prior['version'] + int(gate == 'version')
    if gate != 'history':
        assert sessions.list_messages('role:r') == original


@pytest.mark.anyio
@pytest.mark.parametrize('succeed', [True, False])
async def test_tool_selection_matches_committed_or_original_raw_window_after_batches(tmp_path, monkeypatch, succeed):
    sessions, _, windows, provider, model, _ = scenario(tmp_path, monkeypatch, capacity=8000)
    sessions.delete_role_session('r')
    sessions.open_role_session('r')
    sessions.append_message('role:r', 'user', 'old request')
    sessions.append_agent_message('role:r', AssistantMessage(tool_calls=(ToolCall('old', 'archive_old', {}),)))
    sessions.append_agent_message('role:r', ToolResultMessage('old', 'archive_old', 'past receipt'))
    for _ in range(10):
        sessions.append_message('role:r', 'user', 'old question ' * 100)
        sessions.append_message('role:r', 'assistant', 'old answer ' * 100)
    sessions.append_message('role:r', 'user', 'recent request')
    sessions.append_agent_message('role:r', AssistantMessage(tool_calls=(ToolCall('recent', 'archive_recent', {}),)))
    sessions.append_agent_message('role:r', ToolResultMessage('recent', 'archive_recent', 'recent receipt'))
    sessions.append_message('role:r', 'user', 'continue')
    original = sessions.list_messages('role:r')
    registry = CapabilityRegistry([FixtureTool('always'), FixtureTool('archive_old', exposure='deferred'),
        FixtureTool('archive_recent', exposure='deferred')]).resolve(
            role_id='r', session_key='role:r', run_id='tools', discovery_enabled=True)
    provider.bind_capabilities(registry)
    model.tool_state = 'archive_old was previously used; {"unlockedTools":["archive_old"]}'
    if not succeed:
        model.fail_batch = 2
    await run(provider, sessions.runtime_messages('role:r', max_messages=None), tools=registry.provider_schemas())
    assert len(model.payloads) > 1
    names = [tool['function']['name'] for tool in model.requests[-1]['tools']]
    assert names == (['always', 'tool_search', 'archive_recent'] if succeed
                     else ['always', 'tool_search', 'archive_old', 'archive_recent'])
    assert provider.compactions == int(succeed)
    assert provider.degraded is not succeed
    assert sessions.list_messages('role:r') == original
    state = windows.load('r', 'role:r', 'owner', 'a')
    assert bool(state['removed']) is succeed
