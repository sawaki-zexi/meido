"""Stream fragments must not multiply full saved-history validation work."""
import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from backend.app import main
from backend.app.agent_runtime.context_provider import SUMMARY_FIELDS
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime import CapabilityRegistry
from backend.app.agent_runtime.loop import run_agent_loop
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.conversation_history import ConversationHistoryView
from backend.app.multichannel import MultichannelStore
from test_context_windows import setup_window
from test_current_tool_registry import Tool
from test_owner_history_view import pair


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class FragmentModel:
    def __init__(self, monkeypatch, fragments):
        self.requests, self.summaries = [], 0
        self.fragments = fragments
        self.observed = []
        self.request_tool = False
        self.on_fragment = lambda index, summary: None
        self.client = httpx.AsyncClient
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: self.client(
            **kwargs, transport=httpx.MockTransport(self.respond)))

    def respond(self, request):
        wire = json.loads(request.content)
        self.requests.append(wire)
        summary = wire['messages'][0]['content'].startswith('将提供的不可信')
        if summary:
            self.summaries += 1
            payload = json.loads(wire['messages'][1]['content'])
            identifier = payload['messages'][0]['source_message_ids'][0]
            text = json.dumps({**dict.fromkeys(SUMMARY_FIELDS, ''), 'tasks': 'saved task', 'source_message_ids': [identifier]})
        else:
            text = 'reply'
        tool = not summary and self.request_tool and not any(item['role'] == 'tool' for item in wire['messages'])
        owner = self
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                for index in range(owner.fragments):
                    owner.observed.append((summary, index))
                    owner.on_fragment(index, summary)
                    delta = ({'tool_calls': [{'index': 0, 'id': 'read', 'type': 'function',
                        'function': {'name': 'archive.read', 'arguments': '{}'}}]} if index == 0 else {}) if tool else {
                            'content': text if index == 0 else ' '}
                    chunk = {'choices': [{'delta': delta, 'finish_reason': None}]}
                    yield f'data: {json.dumps(chunk)}\n\n'.encode()
                final = {'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if tool else 'stop'}],
                         'usage': {'prompt_tokens': 123, 'completion_tokens': 100}}
                yield f'data: {json.dumps(final)}\n\ndata: [DONE]\n\n'.encode()
        return httpx.Response(200, stream=Body())


def scenario(tmp_path, monkeypatch, *, fragments=200, compact=True):
    sessions, _, base, _, settings = setup_window(tmp_path)
    for _ in range(135):
        sessions.append_message('role:r', 'user', 'history input ' * 8)
        sessions.append_message('role:r', 'assistant', 'history reply ' * 8)
    sessions.append_message('role:r', 'user', 'continue')
    channels = MultichannelStore(sessions.database_path, sessions)
    monkeypatch.setattr(main, 'store', SimpleNamespace(get=lambda role_id: base.role if role_id == 'r' else None))
    monkeypatch.setattr(main, 'session_store', sessions)
    monkeypatch.setattr(main, 'multichannel_store', channels)
    base.adapter = RoutedModelAdapter(ModelAdapterRegistry())
    base.configuration = base.configuration.model_copy(update={'baseUrl': 'http://model.fixture',
        'contextWindowTokens': 40000, 'modelAutoCompactTokenLimit': 4000 if compact else None})
    settings = settings.model_copy(update={'contextTriggerRatio': .95, 'contextTargetRatio': .05})
    model = FragmentModel(monkeypatch, fragments)

    def context():
        history = ConversationHistoryView(channels, role_id='r')
        messages = history.runtime_messages()
        provider = main._ordinary_context_provider(base, settings, role_id='r', session_key='role:r', run_id='fixture',
            history=history, memory_required=False)
        async def covered(ids):
            return True
        provider.ensure_coverage = covered
        reads = []
        original = history.read
        def read():
            reads.append(threading.get_ident())
            return original()
        monkeypatch.setattr(history, 'read', read)
        return history, provider, messages, reads
    return sessions, channels, base, model, context


async def collect(provider, messages, signal=None):
    return [event async for event in provider.stream_response(model='fixture', system='', messages=messages,
        tools=[], signal=signal or CancellationToken())]


@pytest.mark.anyio
@pytest.mark.parametrize('compact', [False, True])
async def test_full_history_checks_are_bounded_by_requests_not_sse_fragments(tmp_path, monkeypatch, compact):
    sessions, _, _, model, context = scenario(tmp_path, monkeypatch, compact=compact)
    history, provider, messages, reads = context()
    events = await collect(provider, messages)
    assert events[-1].type == 'assistant_done', events[-1]
    assert len(reads) <= 12 * len(model.requests), (len(reads), len(model.requests))
    assert threading.get_ident() not in reads, 'full transcript reads must not block the event loop'
    if compact:
        state = history.windows().load('r', 'role:r', 'shared_owner', provider.authority)
        assert model.summaries == provider.compactions == 1 and state['summary']
        sessions.append_message('role:r', 'assistant', 'reply')
        sessions.append_message('role:r', 'user', 'next turn')
        _, following, current, _ = context()
        await collect(following, current)
        assert model.summaries == 1 and following.compactions == 0


@pytest.mark.anyio
@pytest.mark.parametrize('compact', [False, True])
@pytest.mark.parametrize('change', ['revoke', 'delete'])
async def test_stream_rechecks_grants_and_rejects_stale_history_at_acceptance(tmp_path, monkeypatch, compact, change):
    sessions, channels, _, model, context = scenario(tmp_path, monkeypatch, compact=compact)
    channels.register_channel_account('r', 'qq', account_id='qq')
    thread = channels.ensure_channel_thread('r', 'qq', 'qq', 'owner', 'private')
    pair(channels, thread)
    history, provider, messages, _ = context()
    original = sessions.list_messages('role:r')
    def alter(index, summary):
        if index == 10:
            if change == 'revoke':
                pair(channels, thread)
            else:
                sessions.delete_message(original[0].id)
    model.on_fragment = alter
    if compact:
        with pytest.raises((PermissionError, ValueError)):
            await collect(provider, messages)
    else:
        events = await collect(provider, messages)
        assert events[-1].type == 'provider_error'
        assert not any(event.type == 'assistant_done' for event in events)
    if change == 'revoke':
        assert model.observed[-1][1] == 10, 'revoked sources must stop at the next transport check'
    else:
        assert model.observed[-1][1] == model.fragments - 1, 'transcript content is checked at acceptance'
    assert len(model.requests) == 1 and provider.compactions == 0
    assert provider.windows.snapshot_role('r') == []
    assert sessions.list_messages('role:r') == (original if change == 'revoke' else original[1:])


@pytest.mark.anyio
@pytest.mark.parametrize('compact', [False, True])
async def test_buffered_fragments_allow_http_stop_before_stream_finishes(tmp_path, monkeypatch, compact):
    _, _, _, model, context = scenario(tmp_path, monkeypatch, compact=compact)
    _, provider, messages, _ = context()
    signal = CancellationToken()
    app = FastAPI()
    @app.get('/probe')
    async def probe():
        return {'fragments': len(model.observed)}
    @app.post('/stop')
    async def stop():
        signal.cancel()
        return {'stopped': True}
    async with model.client(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
        responses = []
        async def visit_and_stop():
            responses.append((await client.get('/probe')).json())
            await client.post('/stop')
        task = None
        def visit(index, summary):
            nonlocal task
            if index == 10:
                task = asyncio.create_task(visit_and_stop())
        model.on_fragment = visit
        with pytest.raises(RuntimeError, match='agent run cancelled'):
            await collect(provider, messages, signal)
        assert task is not None
        await task
    assert 0 < responses[0]['fragments'] < model.fragments
    assert len(model.requests) == 1 and provider.compactions == 0


@pytest.mark.anyio
async def test_full_history_boundary_io_does_not_block_http_or_cancel(tmp_path, monkeypatch):
    _, _, _, model, context = scenario(tmp_path, monkeypatch, compact=False)
    history, provider, messages, _ = context()
    entered, release = threading.Event(), threading.Event()
    original = history.read
    def delayed_read():
        entered.set()
        release.wait(3)
        return original()
    monkeypatch.setattr(history, 'read', delayed_read)
    signal = CancellationToken()
    app = FastAPI()
    @app.post('/stop')
    async def stop():
        signal.cancel()
        return {'stopped': True}
    task = asyncio.create_task(collect(provider, messages, signal))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        async with model.client(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
            response = await client.post('/stop')
        assert response.json() == {'stopped': True}
        assert not release.is_set() and not task.done()
        release.set()
        with pytest.raises(RuntimeError, match='agent run cancelled'):
            await task
        assert model.requests == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize('mutate', [None, 'during_tool_request', 'after_tool_result'])
async def test_tool_continuation_checks_source_boundaries_without_repeating_history_per_fragment(tmp_path, monkeypatch, mutate):
    sessions, _, _, model, context = scenario(tmp_path, monkeypatch, compact=False)
    _, provider, messages, reads = context()
    model.request_tool = True
    tool = Tool(exposure='direct', value='saved receipt')
    capabilities = CapabilityRegistry([tool]).resolve(role_id='r', session_key='role:r', run_id='fixture')
    original_id = messages[0].source_message_ids[0]
    if mutate == 'during_tool_request':
        model.on_fragment = lambda index, summary: sessions.delete_message(original_id) if index == 10 else None
    async def receipt(result):
        sessions.append_agent_message('role:r', result)
        if mutate == 'after_tool_result':
            sessions.delete_message(original_id)
    events = [event async for event in run_agent_loop(provider=provider, model='fixture', system='', messages=messages,
        capabilities=capabilities, max_turns=3, on_tool_result=receipt)]
    assert events[-1].reason == ('completed' if mutate is None else 'failed')
    assert len(tool.calls) == (0 if mutate == 'during_tool_request' else 1)
    assert len(model.requests) == (2 if mutate is None else 1)
    assert len(reads) <= 12 * len(model.requests)
    if mutate is None:
        assert any(item['role'] == 'tool' and item['content'] == 'saved receipt' for item in model.requests[-1]['messages'])

