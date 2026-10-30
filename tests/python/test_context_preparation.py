"""Preparation must remain bounded, responsive and separate from execution."""
import asyncio
import socket
import threading

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from backend.app.agent_runtime.context_provider import ContextWindowProvider
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.types import UserMessage
from backend.app.context_windows import ContextWindowStore, _stamp
from test_context_windows import setup_window, execute


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_estimation_does_not_read_host_context(tmp_path):
    _, _, provider, _, _ = setup_window(tmp_path)
    reads = []
    provider.context_blocks = lambda: reads.append('host') or ['context']
    provider.namespace_context = lambda signal: reads.append('namespace') or 'catalog'
    tools = [{'type': 'function', 'function': {'name': 'tool_search', 'parameters': {'type': 'object'}}}]
    first = provider.estimate_request('', [UserMessage('hello')], tools)
    second = provider.estimate_request('', [UserMessage('hello')], tools)
    assert first == second
    assert reads == []


@pytest.mark.anyio
async def test_compaction_starts_at_configured_retention(tmp_path):
    sessions, adapter, provider, windows, settings = setup_window(tmp_path)
    settings = settings.model_copy(update={'compactionRetainedTurns': 2, 'contextTargetRatio': .3})
    provider.configuration = provider.configuration.model_copy(update={'contextWindowTokens': 16000, 'modelAutoCompactTokenLimit': 6000})
    await execute(provider, windows, settings, sessions.runtime_messages('role:r'))
    import json
    payload = json.loads(adapter.calls[0][0][1].content[0].text)
    # Eight completed turns precede the current input; keep exactly two of them.
    assert len(payload['messages']) == 12


def test_fingerprint_batches_rows_without_changing_order_or_missing_detection(tmp_path):
    sessions, _, _, windows, _ = setup_window(tmp_path)
    ids = [message.id for message in sessions.list_messages('role:r')]
    ids = [*reversed(ids), ids[0]]
    with windows.connect() as db:
        columns = 'message_id, role, content, status, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, metadata_json'
        expected = _stamp([db.execute(f'SELECT {columns} FROM messages WHERE session_key=? AND message_id=?', ('role:r', identifier)).fetchone() for identifier in ids])
        queries = []
        db.set_trace_callback(queries.append)
        assert windows.fingerprint(db, 'role:r', ids) == expected
        assert len(queries) == 1
        from backend.app.context_windows import WindowConflict
        with pytest.raises(WindowConflict):
            windows.fingerprint(db, 'role:r', [*ids, 'missing'])


@pytest.mark.anyio
async def test_http_requests_and_stop_progress_while_context_io_is_blocked(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def blocks():
        entered.set()
        release.wait(3)  # bounded failure cleanup, never the success condition
        return ['host context']
    base.context_blocks = blocks
    signal = CancellationToken()
    async def coverage(ids):
        return True
    provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r', scope='owner', authority='a',
        authorized=lambda: True, ensure_coverage=coverage)
    app = FastAPI()
    @app.post('/send')
    async def send():
        try:
            return [str(event) async for event in provider.stream_response(model='fixture', system='',
                messages=sessions.runtime_messages('role:r'), tools=[], signal=signal)]
        except RuntimeError:
            return {'cancelled': signal.is_cancelled()}
    @app.get('/probe')
    async def probe():
        return {'preparing': entered.is_set() and not release.is_set()}
    @app.post('/stop')
    async def stop():
        signal.cancel()
        return {'stopped': True}
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}', timeout=8) as client:
        request = None
        try:
            while not server.started:
                await asyncio.sleep(.01)
            request = asyncio.create_task(client.post('/send'))
            assert await asyncio.to_thread(entered.wait, 2)
            probe = await client.get('/probe')
            assert probe.json() == {'preparing': True}
            assert not request.done(), 'The send must still be waiting on host I/O'
            await client.post('/stop')
            release.set()
            assert (await request).json() == {'cancelled': True}
            assert adapter.calls == []
            assert windows.load('r', 'role:r', 'owner', 'a')['removed'] == []
        finally:
            release.set()
            if request is not None:
                await request
            server.should_exit = True
            await server_task


@pytest.mark.anyio
async def test_candidates_share_metadata_but_dispatch_observes_revocation(tmp_path):
    from backend.app.agent_runtime import CapabilityRegistry, ToolContext
    from test_current_tool_registry import Tool
    tools = [Tool(f'archive.{index}') for index in range(20)]
    revoked, reads, executions = set(), [], []
    def prepare(definitions, context):
        reads.append('host')
        return {item.name: 'revoked' for item in definitions if item.name in revoked}
    def authorize(definition, context):
        executions.append(definition.name)
        return 'revoked' if definition.name in revoked else True
    capabilities = CapabilityRegistry(tools).resolve(role_id='r', session_key='role:r', run_id='run',
        discovery_enabled=True, prepare_tools=prepare, authorize_tool=authorize)
    signal = CancellationToken()
    preparation = await capabilities.prepare_metadata(signal)
    from backend.app.agent_runtime.types import ToolResultMessage
    raw = [ToolResultMessage('loaded', 'tool_search', '{}', unlocked_tools=('archive.0',))]
    for _ in range(30):
        view = capabilities.window_view(raw, preparation=preparation)
        assert [item['function']['name'] for item in view.provider_schemas()] == ['tool_search', 'archive.0']
        assert 'archive.0' in preparation.namespace_context(('tool_search',))
    assert reads == ['host'] and executions == []
    revoked.add('archive.0')
    context = ToolContext('r', 'role:r', 'run', signal)
    assert view.authorization_denial(view.definition('archive.0'), context) == 'revoked'
    refreshed = await capabilities.prepare_metadata(signal)
    assert [item['function']['name'] for item in capabilities.window_view(raw, preparation=refreshed).provider_schemas()] == ['tool_search']


@pytest.mark.anyio
async def test_prepared_input_is_sent_without_host_rereads_or_configuration_drift(tmp_path):
    sessions, adapter, provider, _, _ = setup_window(tmp_path)
    reads = []
    provider.context_blocks = lambda: reads.append('read') or ['captured host context']
    signal = CancellationToken()
    context = await provider.prepare_context(signal)
    messages = sessions.runtime_messages('role:r')[-1:]
    tools = [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]
    request = provider.prepare_request('', messages, tools, context=context)
    estimate = provider.estimate_prepared(request)
    provider.context_blocks = lambda: (_ for _ in ()).throw(AssertionError('late host read'))
    tools.clear()
    provider.configuration.model = 'changed'
    provider.max_response_tokens = 999
    assert provider.estimate_prepared(request) == estimate
    [event async for event in provider.stream_prepared(request, system='', messages=messages, signal=signal)]
    assert reads == ['read']
    assert adapter.calls[-1] == (request.messages, request.tools, request.max_tokens)
    assert request.configuration.model == 'fixture'
    assert request.max_tokens == 1024
    assert 'captured host context' in str(adapter.calls[-1][0])


@pytest.mark.anyio
async def test_context_io_wait_uses_replacement_schema_when_it_resumes(tmp_path):
    from backend.app.agent_runtime import CapabilityRegistry
    from test_current_tool_registry import Tool
    sessions, adapter, base, windows, settings = setup_window(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def blocks():
        entered.set()
        release.wait(3)
        return []
    base.context_blocks = blocks
    registry = CapabilityRegistry([Tool('read', exposure='direct')])
    capabilities = registry.resolve(role_id='r', session_key='role:r', run_id='run')
    async def coverage(ids):
        return True
    wrapper = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r', scope='owner', authority='a',
        authorized=lambda: True, ensure_coverage=coverage)
    wrapper.bind_capabilities(capabilities)
    async def collect():
        return [event async for event in wrapper.stream_response(model='fixture', system='',
            messages=sessions.runtime_messages('role:r')[-1:], tools=capabilities.provider_schemas(), signal=CancellationToken())]
    task = asyncio.create_task(collect())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        registry.register(Tool('read', exposure='direct', schema={'type': 'object', 'properties': {'new': {'type': 'string'}}}))
        release.set()
        await task
        assert 'new' in adapter.calls[-1][1][0]['function']['parameters']['properties']
    finally:
        release.set()
        await task


@pytest.mark.anyio
async def test_cancel_during_revision_keeps_tool_receipt_and_ends_cancelled():
    from backend.app.agent_runtime import CapabilityRegistry, AssistantDoneEvent, AssistantMessage, ToolCall
    from backend.app.agent_runtime.loop import run_agent_loop
    from backend.app.agent_runtime.types import ToolResultMessage
    from test_current_tool_registry import Tool
    signal = CancellationToken()
    entered, release = threading.Event(), threading.Event()
    reads = []
    def prepare(definitions, context):
        reads.append(1)
        # Initial visibility, then search, then the revision after receipt.
        if len(reads) == 3:
            entered.set()
            release.wait(3)
        return {}
    capabilities = CapabilityRegistry([Tool()]).resolve(role_id='r', session_key='s', run_id='run',
        discovery_enabled=True, prepare_tools=prepare)
    class Provider:
        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('search', 'tool_search', {'query': 'select:archive.read'}),)))
    receipts = []
    async def collect():
        return [event async for event in run_agent_loop(provider=Provider(), model='fixture', system='',
            messages=[UserMessage('search')], capabilities=capabilities, signal=signal, on_tool_result=receipts.append)]
    task = asyncio.create_task(collect())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert len(receipts) == 1 and isinstance(receipts[0], ToolResultMessage) and not receipts[0].is_error
        signal.cancel()
        release.set()
        events = await task
        assert events[-1].type == 'agent_end' and events[-1].reason == 'cancelled'
        assert len(receipts) == 1
    finally:
        release.set()
        await task


@pytest.mark.anyio
@pytest.mark.parametrize('successful_summary', [False, True])
async def test_summary_wait_reloads_metadata_and_context_before_success_or_fallback(tmp_path, successful_summary):
    from backend.app.agent_runtime import CapabilityRegistry
    from test_current_tool_registry import Tool
    sessions, adapter, base, windows, settings = setup_window(tmp_path, 'valid' if successful_summary else 'invalid')
    base.configuration = base.configuration.model_copy(update={'contextWindowTokens': 16000, 'modelAutoCompactTokenLimit': 6000})
    old, new = Tool('current', exposure='direct', value='old'), Tool('current', exposure='direct', value='new', schema={
        'type': 'object', 'properties': {'current': {'type': 'string'}}})
    registry = CapabilityRegistry([old])
    capabilities = registry.resolve(role_id='r', session_key='role:r', run_id='run')
    host_context = ['before await']
    base.context_blocks = lambda: host_context
    original = adapter.stream_rich_messages
    async def changed(messages, configuration, tools, **kwargs):
        if messages[0].content[0].text.startswith('将提供的不可信'):
            await asyncio.sleep(0)
            registry.register(new)
            host_context[:] = ['after await']
        async for event in original(messages, configuration, tools, **kwargs):
            yield event
    adapter.stream_rich_messages = changed
    async def coverage(ids):
        return True
    provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r', scope='owner', authority='a',
        authorized=lambda: True, ensure_coverage=coverage)
    provider.bind_capabilities(capabilities)
    [event async for event in provider.stream_response(model='fixture', system='', messages=sessions.runtime_messages('role:r'),
        tools=capabilities.provider_schemas(), signal=CancellationToken())]
    sent, tools, _ = adapter.calls[-1]
    assert 'after await' in str(sent) and 'before await' not in str(sent)
    assert 'current' in tools[0]['function']['parameters']['properties']
    assert provider.compactions == int(successful_summary)


@pytest.mark.anyio
@pytest.mark.parametrize('cancel_task', [False, True])
async def test_cancel_while_commit_reads_fingerprint_cannot_publish_a_late_cut(tmp_path, monkeypatch, cancel_task):
    sessions, adapter, provider, windows, settings = setup_window(tmp_path)
    original = windows.fingerprint
    entered, release = threading.Event(), threading.Event()
    def fingerprint(db, session, ids):
        # Only the commit transaction owns a write lock.
        if db.in_transaction:
            entered.set()
            release.wait(3)
        return original(db, session, ids)
    monkeypatch.setattr(windows, 'fingerprint', fingerprint)
    signal = CancellationToken()
    async def coverage(ids):
        return True
    wrapper = ContextWindowProvider(provider, settings, windows, role_id='r', session_key='role:r', scope='owner', authority='a',
        authorized=lambda: True, ensure_coverage=coverage)
    async def collect():
        return [event async for event in wrapper.stream_response(model='fixture', system='',
            messages=sessions.runtime_messages('role:r'), tools=[], signal=signal)]
    task = asyncio.create_task(collect())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if cancel_task:
            task.cancel()
            await asyncio.sleep(0)
            assert signal.is_cancelled()
            assert not task.done(), 'A cancelled owner must drain its pending write'
        else:
            signal.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel_task else RuntimeError):
            await task
        assert windows.load('r', 'role:r', 'owner', 'a')['removed'] == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
