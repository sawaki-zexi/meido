"""Bounded, non-authoritative progress, independent of final receipts."""
import asyncio
import json

import pytest

from backend.app.agent_runtime import (
    AssistantDoneEvent, AssistantMessage, CancellationToken, ToolCall,
    ToolDefinition, ToolExecutionEndEvent, ToolExecutionStartEvent, ToolExecutionUpdateEvent,
    ToolResultMessage, UserMessage, run_agent_loop,
)
from backend.app.agent_runtime.tools import AgentToolResult
from backend.app.agent_runtime.types import ImageContent


class Provider:
    def __init__(self, calls=None):
        self.calls = calls or (ToolCall('one', 'work', {}),)
        self.requests = 0
    async def stream_response(self, **kwargs):
        self.requests += 1
        yield AssistantDoneEvent(AssistantMessage(tool_calls=self.calls, stop_reason='tool_use')
            if self.requests == 1 else AssistantMessage('finished'))


def loop(tool, *, provider=None, signal=None, timeout=None):
    return run_agent_loop(provider=provider or Provider(), model='fake', system='', messages=[UserMessage('work')],
                          tools=[tool], signal=signal, tool_timeout=timeout, run_id='progress-run')


def test_progress_arrives_before_tool_barrier_is_released():
    async def exercise():
        release = asyncio.Event()
        finished = False
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                nonlocal finished
                on_update(AgentToolResult('真实阶段'))
                await release.wait()
                finished = True
                return AgentToolResult('final receipt')
        stream = loop(Tool())
        events = []
        try:
            while True:
                event = await asyncio.wait_for(anext(stream), .5)
                events.append(event)
                if isinstance(event, ToolExecutionUpdateEvent):
                    assert event.result.content == '真实阶段'
                    assert not finished
                    release.set()
                    break
            events.extend([event async for event in stream])
        finally:
            release.set()
            await stream.aclose()
        kinds = [event.type for event in events if event.type.startswith('tool_execution')]
        assert kinds == ['tool_execution_start', 'tool_execution_update', 'tool_execution_end']
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert finished
    asyncio.run(exercise())


def test_progress_buffer_stress_is_bounded_and_keeps_latest_snapshot():
    from backend.app.agent_runtime.progress import ToolProgressBuffer
    async def exercise():
        buffer = ToolProgressBuffer()
        for index in range(100_000):
            buffer.add(AgentToolResult(f'阶段 {index}', details={'phase': 'work'}))
        assert buffer.pending_count <= 32
        assert buffer.pending_bytes <= 256 * 1024
        assert buffer.peak_count <= 32
        assert buffer.peak_bytes <= 256 * 1024
        assert buffer.dropped_updates == 100_000 - buffer.pending_count
        snapshots = []
        while buffer.pending_count:
            snapshots.append(buffer.pop())
        assert snapshots[-1].content == '阶段 99999'
        assert buffer.pending_bytes == 0
        buffer.close()
        for _ in range(100):
            buffer.add(AgentToolResult('late'))
        assert buffer.pending_count == 0
        assert buffer.accepted_updates == 100_000
        print(f'progress stress: peak_count={buffer.peak_count}, peak_bytes={buffer.peak_bytes}, dropped={buffer.dropped_updates}, accepted_after_end=0')
    asyncio.run(exercise())


@pytest.mark.parametrize('details', [ {'nested': ['大' * 1_000_000]}, {'bad': object()}, {'number': 10**10000}])
def test_progress_budget_covers_utf8_details_images_and_mutation(details):
    from backend.app.agent_runtime.progress import ToolProgressBuffer
    async def exercise():
        buffer = ToolProgressBuffer()
        original = AgentToolResult('\x00中😀' * 100_000, details=details,
            images=(ImageContent('image/png', 'A' * 1_000_000),) * 8)
        buffer.add(original)
        snapshot = buffer.pop()
        assert not snapshot.images
        assert len(snapshot.content.encode('utf-8')) <= 16 * 1024
        assert len(json.dumps(snapshot.details, ensure_ascii=False).encode()) <= 8 * 1024
        envelope = {'result': snapshot.content, 'details': snapshot.details, 'isError': snapshot.is_error}
        assert len(json.dumps(envelope, ensure_ascii=False).encode()) <= 32 * 1024
        assert snapshot.details['imagesOmitted'] == 8
        assert snapshot.details['detailsOmitted'] is True
        assert 'A' * 100 not in json.dumps(snapshot.details)
        nested = {'phase': ['first']}
        buffer.add(AgentToolResult('x', details=nested))
        nested['phase'][0] = 'changed'
        assert buffer.pop().details['phase'] == ['first']
    asyncio.run(exercise())


def test_cyclic_details_are_omitted_without_unbounded_copy():
    from backend.app.agent_runtime.progress import ToolProgressBuffer
    async def exercise():
        value = {}
        value['self'] = value
        buffer = ToolProgressBuffer(output_limit=5)
        buffer.add(AgentToolResult('中' * 100, details=value))
        result = buffer.pop()
        assert len(result.content.encode()) <= 5
        assert result.details['detailsOmitted'] is True
        assert result.details['truncated'] is True
    asyncio.run(exercise())


def test_ended_call_callback_cannot_update_while_next_call_runs():
    async def exercise():
        callback = None
        second_release = asyncio.Event()
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                nonlocal callback
                if context.tool_call_id == 'one':
                    callback = on_update
                    on_update(AgentToolResult('first progress'))
                    return AgentToolResult('first receipt')
                callback(AgentToolResult('late first'))
                on_update(AgentToolResult('second progress'))
                await second_release.wait()
                return AgentToolResult('second receipt')
        events = []
        async for event in loop(Tool(), provider=Provider((ToolCall('one', 'work', {}), ToolCall('two', 'work', {})))):
            events.append(event)
            if isinstance(event, ToolExecutionUpdateEvent) and event.tool_call_id == 'two':
                second_release.set()
        assert not any(isinstance(event, ToolExecutionUpdateEvent) and event.result.content == 'late first' for event in events)
        assert len([event for event in events if isinstance(event, ToolExecutionEndEvent)]) == 2
    asyncio.run(exercise())


@pytest.mark.parametrize('ending', ['cancel', 'timeout', 'error', 'close', 'revoked'])
def test_callback_window_closes_before_cleanup_and_cleanup_is_joined(ending):
    async def exercise():
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release_cleanup = asyncio.Event()
        token = CancellationToken()
        callbacks = []
        provider = Provider((ToolCall('one', 'work', {'description': '读取当前运行状态'}),))
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                assert arguments == {}
                callbacks.append(on_update)
                on_update(AgentToolResult('entered'))
                entered.set()
                if ending == 'error':
                    raise ValueError('secret should not escape')
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    on_update(AgentToolResult('during cleanup'))
                    await release_cleanup.wait()
        allowed = True
        if ending == 'revoked':
            from backend.app.agent_runtime import CapabilityRegistry
            capabilities = CapabilityRegistry([Tool()]).resolve(role_id='r', session_key='s', run_id='run',
                enabled_tools={'work'}, authorize_tool=lambda *args: True if allowed else 'source revoked')
            stream = run_agent_loop(provider=provider, model='fake', system='', messages=[],
                capabilities=capabilities, signal=token)
        else:
            stream = loop(Tool(), provider=provider, signal=token, timeout=.01 if ending == 'timeout' else None)
        events = []
        async def consume():
            nonlocal allowed
            async for event in stream:
                events.append(event)
                if isinstance(event, ToolExecutionUpdateEvent) and event.result.content == 'entered':
                    if ending == 'cancel':
                        token.cancel()
                    elif ending == 'close':
                        break
                    elif ending == 'revoked':
                        allowed = False
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if ending != 'error':
                if ending == 'close':
                    await task
                    task = asyncio.create_task(stream.aclose())
                await asyncio.wait_for(cleaning.wait(), 1)
                assert not task.done()
                release_cleanup.set()
            await asyncio.wait_for(task, 1)
            callbacks[0](AgentToolResult('after settlement'))
            assert next(event.arguments for event in events if isinstance(event, ToolExecutionStartEvent)) == {'description': '读取当前运行状态'}
            assert not any(isinstance(event, ToolExecutionUpdateEvent) and event.result.content in {'during cleanup', 'after settlement'} for event in events)
            if ending != 'close':
                ends = [event for event in events if isinstance(event, ToolExecutionEndEvent)]
                assert len(ends) == 1
                assert ends[0].result.details['status'] == {'cancel': 'cancelled', 'timeout': 'timed_out', 'error': 'failed', 'revoked': 'denied'}[ending]
        finally:
            release_cleanup.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await stream.aclose()
    asyncio.run(exercise())


def test_byte_pressure_coalesces_without_dropping_final_receipt():
    async def exercise():
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                for index in range(1000):
                    on_update(AgentToolResult(str(index) + '中' * 6000, details={'blob': 'd' * 6000}))
                return AgentToolResult('authoritative receipt', details={'receipt': 'kept'}, images=(ImageContent('image/png', 'AA=='),))
        events = [event async for event in loop(Tool())]
        updates = [event for event in events if isinstance(event, ToolExecutionUpdateEvent)]
        assert 0 < len(updates) < 32
        assert updates[-1].result.content.startswith('999')
        end = next(event for event in events if isinstance(event, ToolExecutionEndEvent))
        assert end.dropped_updates == 1000 - len(updates)
        assert end.result.content == 'authoritative receipt'
        assert end.result.details['receipt'] == 'kept'
        assert end.result.images == (ImageContent('image/png', 'AA=='),)
        persisted = [message for message in events[-1].messages if isinstance(message, ToolResultMessage)]
        assert len(persisted) == 1
        assert persisted[0].content == 'authoritative receipt'
        assert not persisted[0].images  # expired only after the next provider request
    asyncio.run(exercise())


@pytest.mark.parametrize('receipt_status,is_error', [('succeeded', False), ('unknown', True)])
def test_settled_receipt_survives_simultaneous_host_cancel_without_new_actions(receipt_status, is_error):
    async def exercise():
        token = CancellationToken()
        provider = Provider((ToolCall('one', 'work', {}), ToolCall('two', 'work', {})))
        calls = []
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                calls.append(context.tool_call_id)
                token.cancel()
                return AgentToolResult('actual external receipt', is_error=is_error, details={'status': receipt_status})
        events = [event async for event in loop(Tool(), provider=provider, signal=token)]
        end = next(event for event in events if isinstance(event, ToolExecutionEndEvent))
        assert end.result.content == 'actual external receipt'
        assert end.result.details['status'] == receipt_status
        assert end.result.is_error is is_error
        assert calls == ['one'] and provider.requests == 1
        assert len([event for event in events if isinstance(event, ToolExecutionStartEvent)]) == 1
        assert events[-1].reason == 'cancelled'
    asyncio.run(exercise())


def test_settled_receipt_does_not_grant_permission_for_later_revoked_call():
    from backend.app.agent_runtime import CapabilityRegistry
    async def exercise():
        allowed = True
        calls = []
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                nonlocal allowed
                calls.append(context.tool_call_id)
                allowed = False
                return AgentToolResult('committed receipt')
        tool = Tool()
        capabilities = CapabilityRegistry([tool]).resolve(role_id='r', session_key='s', run_id='run', enabled_tools={'work'},
            authorize_tool=lambda *args: True if allowed else 'source revoked')
        provider = Provider((ToolCall('one', 'work', {}), ToolCall('two', 'work', {})))
        events = [event async for event in run_agent_loop(provider=provider, model='fake', system='', messages=[], capabilities=capabilities)]
        ends = [event for event in events if isinstance(event, ToolExecutionEndEvent)]
        assert ends[0].result.content == 'committed receipt'
        assert ends[1].result.details['status'] == 'denied'
        assert calls == ['one']
    asyncio.run(exercise())


# Reuse the actual listening HTTP fixture; ASGI buffering cannot demonstrate
# that the client sees progress while execute is still waiting.
from test_chat_stop import chat_server


@pytest.mark.parametrize('terminal_status', ['succeeded', 'failed', 'cancelled'])
def test_http_sse_client_receives_progress_before_tool_finishes(chat_server, monkeypatch, terminal_status):
    import threading
    from backend.app import main
    from backend.app.agent_runtime import CapabilityRegistry
    from backend.app.ai.model_types import ModelAssistantMessage, ModelResponseMetadata, ModelStreamDone, ModelTextBlock, ModelToolCallBlock
    client, role, _ = chat_server
    release = threading.Event()
    finished = threading.Event()
    intent = '读取本次运行配置'
    arguments = {'secret': 'never expose raw arguments', 'query': '很长的业务参数' * 100, 'description': intent}
    received = []
    class Tool:
        definition = ToolDefinition('work', 'Work', {'type': 'object', 'properties': {
            'secret': {'type': 'string'}, 'query': {'type': 'string'}}, 'additionalProperties': False})
        async def execute(self, arguments, context, on_update=None):
            received.append(dict(arguments))
            on_update(AgentToolResult('连接阶段 access_token=secret-value', details={'phase': 'connecting'}))
            try:
                await asyncio.to_thread(release.wait, 3)
                return AgentToolResult('receipt', is_error=terminal_status == 'failed')
            finally:
                finished.set()
    class Adapter:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools, **kwargs):
            self.requests += 1
            schema = next(item['function']['parameters'] for item in tools if item['function']['name'] == 'work')
            assert schema['properties']['description']['type'] == 'string'
            assert '5-12' in schema['properties']['description']['description']
            assert 'description' in schema['required']
            yield ModelStreamDone(ModelAssistantMessage(
                (ModelToolCallBlock('http-call', 'work', arguments),),
                ModelResponseMetadata(stop_reason='tool_use')) if self.requests == 1 else ModelAssistantMessage(
                (ModelTextBlock('finished'),), ModelResponseMetadata(stop_reason='stop')))
    async def capabilities(owner, *, session_key, run_id, **kwargs):
        return CapabilityRegistry([Tool()]).resolve(role_id=owner.id, session_key=session_key, run_id=run_id, enabled_tools={'work'})
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    monkeypatch.setattr(main, '_runtime_capabilities', capabilities)
    events = []
    stopped = None
    try:
        with client.stream('POST', f'/api/roles/{role.id}/messages', json={'content': 'test progress'}) as stream:
            assert stream.status_code == 200
            kind = None
            for line in stream.iter_lines():
                if line.startswith('event: '):
                    kind = line[7:]
                if not line.startswith('data: '):
                    continue
                payload = json.loads(line[6:])
                events.append((kind, payload))
                if kind == 'tool_execution_start':
                    assert not finished.is_set()
                    assert intent in payload['argumentsPreview']
                    assert len(payload['argumentsPreview']) <= 120
                    assert 'never expose raw arguments' not in payload['argumentsPreview']
                if kind == 'tool_execution_update':
                    assert not finished.is_set()
                    assert payload['result'] == '连接阶段 access_token=[redacted]'
                    assert payload['status'] == 'running'
                    assert payload['droppedUpdates'] == 0
                    assert payload['roleId'] == role.id
                    assert payload['sessionKey'] == f'role:{role.id}'
                    assert payload['toolCallId'] == 'http-call'
                    if terminal_status == 'cancelled':
                        response = client.post(f'/api/roles/{role.id}/runs/{payload["runId"]}/stop')
                        assert response.status_code == 200
                        stopped = response.json()
                        release.set()
                        # Stop closes this transport; the stop receipt and
                        # persisted history carry the authoritative terminal.
                        break
                    release.set()
        assert release.is_set() and finished.is_set()
        tool_events = [(kind, value) for kind, value in events if kind.startswith('tool_execution')]
        expected_events = ['tool_execution_start', 'tool_execution_update']
        if terminal_status != 'cancelled':
            expected_events.append('tool_execution_end')
        assert [kind for kind, _ in tool_events] == expected_events
        preview = tool_events[0][1]['argumentsPreview']
        assert intent in preview and '[redacted]' in preview
        assert 'arguments' not in tool_events[0][1]
        if stopped is None:
            assert tool_events[-1][1]['status'] == terminal_status
        else:
            stopped_process = stopped['messages'][-1]['metadata']['process']
            assert next(item['status'] for item in stopped_process if item['type'] == 'tool') == 'unknown'
            assert stopped['messages'][-1]['metadata']['runtimeReason'] == 'cancelled'
        assert len({value['runId'] for _, value in tool_events}) == 1
        assert [value['sequence'] for _, value in events] == sorted({value['sequence'] for _, value in events})
        saved = main.session_store.list_messages(f'role:{role.id}')
        results = [item for item in saved if item.messageType == 'tool_result']
        if terminal_status == 'cancelled':
            assert results == []  # No fabricated final receipt on forced close.
        else:
            assert len(results) == 1
            assert results[0].toolResult['status'] == terminal_status
        assert received == [{key: value for key, value in arguments.items() if key != 'description'}]
        assert '连接阶段' not in ''.join(item.content for item in saved)
        # Reopen the durable store and then fetch the same public projection a
        # refreshed chat uses. Progress is not needed to reconstruct its intent.
        from backend.app.session_store import SessionStore
        reopened = SessionStore(main.session_store.database_path)
        call = next(item for item in reopened.list_messages(f'role:{role.id}') if item.messageType == 'tool_call')
        assert call.toolArguments['description'] == intent
        history = client.get(f'/api/roles/{role.id}/session').json()['messages']
        process = next(item['metadata']['process'] for item in reversed(history) if item['role'] == 'assistant')
        projected = next(item for item in process if item['type'] == 'tool')
        assert projected['argumentsPreview'] == preview
        assert projected['status'] == ('unknown' if terminal_status == 'cancelled' else terminal_status)
    finally:
        release.set()


def test_call_soon_update_after_execute_settles_is_not_accepted():
    async def exercise():
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                on_update(AgentToolResult('in window'))
                asyncio.get_running_loop().call_soon(on_update, AgentToolResult('after return'))
                return AgentToolResult('receipt')
        events = [event async for event in loop(Tool())]
        assert [event.result.content for event in events if isinstance(event, ToolExecutionUpdateEvent)] == ['in window']
    asyncio.run(exercise())


def test_byte_budget_peak_is_measured_with_large_snapshots():
    from backend.app.agent_runtime.progress import ToolProgressBuffer
    async def exercise():
        buffer = ToolProgressBuffer()
        for index in range(1000):
            buffer.add(AgentToolResult(str(index) + '中' * 6000, details={'large': 'd' * 6000}))
        assert buffer.peak_count < 32
        assert buffer.peak_bytes <= 256 * 1024
        assert buffer.dropped_updates > 0
        print(f'large snapshot stress: peak_count={buffer.peak_count}, peak_bytes={buffer.peak_bytes}, dropped={buffer.dropped_updates}')
    asyncio.run(exercise())


def test_progress_replaces_invalid_unicode_and_omits_embedded_image_details():
    from backend.app.agent_runtime.progress import ToolProgressBuffer
    async def exercise():
        buffer = ToolProgressBuffer()
        buffer.add(AgentToolResult('safe\ud800', details={'bad': '\ud800'}))
        result = buffer.pop()
        assert result.content.encode('utf-8') == b'safe?'
        assert result.details['detailsOmitted'] is True
        buffer.add(AgentToolResult('safe', details={'image_base64': 'raw-image-bytes', 'phase': 'work'}))
        result = buffer.pop()
        assert result.details['image_base64'] == '[omitted]'
        assert result.details['detailsOmitted'] is True
        assert result.details['phase'] == 'work'
    asyncio.run(exercise())


@pytest.mark.parametrize('receipt_status,is_error', [('succeeded', False), ('unknown', True)])
def test_runtime_close_while_progress_paused_preserves_settled_receipt(tmp_path, receipt_status, is_error):
    import sqlite3
    from datetime import datetime, timezone
    from backend.app.agent_runtime import RuntimeManager
    from backend.app.models import AgentRun
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    async def exercise():
        initialize_databases(tmp_path / 'data')
        sessions = SessionStore(tmp_path / 'data' / 'sessions.db')
        session = sessions.open_role_session('r')
        settled = asyncio.Event()
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                on_update(AgentToolResult('progress'))
                settled.set()
                return AgentToolResult('actual receipt', is_error=is_error, details={'status': receipt_status})
        manager = RuntimeManager(sessions)
        run = AgentRun(runId='settled-close', roleId='r', sessionKey=session.sessionKey, status='created', startedAt=datetime.now(timezone.utc))
        def persist(event):
            sessions.append_agent_message(session.sessionKey, event.message, run_id=run.runId)
        stream = manager.run(run, provider=Provider(), model='fake', system='', messages=[], tools=[Tool()], on_message_end=persist)
        async for event in stream:
            if isinstance(event, ToolExecutionUpdateEvent):
                await settled.wait()
                break
        await stream.aclose()
        saved = sessions.list_messages(session.sessionKey)
        results = [item for item in saved if item.messageType == 'tool_result']
        assert len(results) == 1
        assert results[0].content == 'actual receipt'
        assert results[0].toolResult['status'] == receipt_status
        assert sessions.get_run(run.runId).status == 'cancelled'
        assert not manager.lock_for('r').locked()
        with sqlite3.connect(sessions.database_path) as db:
            category = db.execute('SELECT result_category FROM tool_audits WHERE run_id = ?', (run.runId,)).fetchone()
        assert category == (receipt_status,)
    asyncio.run(exercise())


def test_settled_result_storage_failure_stops_run_without_replaying_tool(tmp_path):
    import sqlite3
    from datetime import datetime, timezone
    from backend.app.agent_runtime import RuntimeManager
    from backend.app.models import AgentRun
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    async def exercise():
        initialize_databases(tmp_path / 'data')
        sessions = SessionStore(tmp_path / 'data' / 'sessions.db')
        session = sessions.open_role_session('r')
        provider = Provider()
        executed = []
        attempts = []
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                executed.append('committed external effect')
                on_update(AgentToolResult('progress'))
                return AgentToolResult('actual receipt')
        run = AgentRun(runId='storage-failure', roleId='r', sessionKey=session.sessionKey, status='created', startedAt=datetime.now(timezone.utc))
        def persist(event):
            if isinstance(event.message, ToolResultMessage):
                attempts.append(event.message)
                raise RuntimeError('storage unavailable')
            sessions.append_agent_message(session.sessionKey, event.message, run_id=run.runId)
        with pytest.raises(RuntimeError, match='storage unavailable'):
            async for _ in RuntimeManager(sessions).run(run, provider=provider, model='fake', system='', messages=[], tools=[Tool()], on_message_end=persist):
                pass
        assert len(executed) == 1 and len(attempts) == 1 and provider.requests == 1
        assert sessions.get_run(run.runId).status == 'failed'
        with sqlite3.connect(sessions.database_path) as db:
            assert db.execute('SELECT result_category FROM tool_audits WHERE run_id=?', (run.runId,)).fetchone() == ('succeeded',)
    asyncio.run(exercise())


def test_consumer_close_joins_inflight_result_persistence_exactly_once(tmp_path):
    from datetime import datetime, timezone
    from backend.app.agent_runtime import RuntimeManager
    from backend.app.models import AgentRun
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    async def exercise():
        initialize_databases(tmp_path / 'data')
        sessions = SessionStore(tmp_path / 'data' / 'sessions.db')
        session = sessions.open_role_session('r')
        persisting = asyncio.Event()
        release = asyncio.Event()
        close_requested = asyncio.Event()
        attempts = []
        class Tool:
            definition = ToolDefinition('work', 'Work')
            async def execute(self, arguments, context, on_update=None):
                on_update(AgentToolResult('progress'))
                return AgentToolResult('actual receipt')
        run = AgentRun(runId='persist-close', roleId='r', sessionKey=session.sessionKey, status='created', startedAt=datetime.now(timezone.utc))
        async def persist(event):
            if isinstance(event.message, ToolResultMessage):
                attempts.append(event.message)
                persisting.set()
                await release.wait()
            sessions.append_agent_message(session.sessionKey, event.message, run_id=run.runId)
        manager = RuntimeManager(sessions)
        stream = manager.run(run, provider=Provider(), model='fake', system='', messages=[], tools=[Tool()], on_message_end=persist)
        async for event in stream:
            if isinstance(event, ToolExecutionUpdateEvent):
                break
        await asyncio.wait_for(persisting.wait(), 1)
        async def close():
            close_requested.set()
            await stream.aclose()
        closing = asyncio.create_task(close())
        await close_requested.wait()
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 1)
        assert len(attempts) == 1
        assert len([message for message in sessions.list_messages(session.sessionKey) if message.messageType == 'tool_result']) == 1
        assert not manager.lock_for('r').locked()
    asyncio.run(exercise())
