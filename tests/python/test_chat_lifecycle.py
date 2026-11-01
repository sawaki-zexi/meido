"""Accepted desktop runs belong to the service, across real HTTP subscriptions."""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from backend.app import main
from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
from backend.app.models import RoleUpdateInput
from test_chat_stop import chat_server


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('condition did not become true')


def events(response):
    for block in response.iter_lines():
        if block.startswith('data: '):
            yield json.loads(block[6:])


def test_disconnect_resume_and_duplicate_submit_do_not_repeat_execution(chat_server, monkeypatch):
    client, role, other = chat_server
    release, entered = threading.Event(), threading.Event()
    finish_steps = [threading.Event() for _ in range(3)]
    advanced = [threading.Event() for _ in range(3)]
    calls = []

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            calls.append(role.id)
            yield '已收到'
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            for index, text in enumerate(('，', '完', '成')):
                assert await asyncio.to_thread(finish_steps[index].wait, 5)
                yield text
                advanced[index].set()

    monkeypatch.setattr(main, 'model_adapter', Model())
    path = f'/api/roles/{role.id}'
    try:
        accepted = client.post(path + '/runs', json={'content': '同一句', 'requestId': 'lifecycle-first'})
        assert accepted.status_code == 200, accepted.text
        run = accepted.json()['run']
        assert run['runId'] == 'run-lifecycle-first'
        assert entered.wait(3)
        with client.stream('GET', path + f"/runs/{run['runId']}/events") as response:
            assert response.status_code == 200
            snapshot = next(events(response))
            assert snapshot['runId'] == run['runId']
        current = client.get(path + '/session').json()
        assert current['activeRun']['runId'] == run['runId']
        assert current['activeRun']['phase'] == 'model'
        assert current['messages'][-1]['content'] == '已收到'
        duplicate = client.post(path + '/runs', json={'content': '同一句', 'requestId': 'lifecycle-first'})
        assert duplicate.status_code == 200
        assert client.post(path + '/runs', json={'content': '不同输入', 'requestId': 'lifecycle-first'}).status_code == 409
        assert client.post(f'/api/roles/{other.id}/runs', json={'content': '同一句', 'requestId': 'lifecycle-first'}).status_code == 409
        assert calls == [role.id]
        projection = main._read_desktop_projection
        crossed_reads = []

        def cross_final_delta(*args):
            value = projection(*args)
            if len(crossed_reads) < len(finish_steps):
                index = len(crossed_reads)
                crossed_reads.append(index)
                finish_steps[index].set()
                assert advanced[index].wait(3)
            return value

        # Each worker snapshot crosses one real final delta. Three stale reads
        # must exercise the bounded 503 contract, independent of runner speed.
        monkeypatch.setattr(main, '_read_desktop_projection', cross_final_delta)
        retried = []

        def read_terminal():
            response = client.get(path + '/runs/' + run['runId'])
            if response.status_code != 200:
                assert response.status_code == 503, (response.status_code, response.text)
                assert response.headers['Retry-After'] == '1'
                assert response.headers['X-Meido-Run-Id'] == run['runId']
                assert response.json() == {'detail': '回复仍在更新，正在重新同步'}
                retried.append(response.status_code)
                return None
            value = response.json()
            assert value['run']['runId'] == run['runId']
            assert value['run']['status'] in {'queued', 'created', 'running', 'completed'}, value
            return value if value['run']['status'] == 'completed' else None

        release.set()
        terminal = wait_for(read_terminal)
        assert crossed_reads == [0, 1, 2] and retried
        assert calls == [role.id]
        assert [item['content'] for item in terminal['messages']] == ['同一句', '已收到，完成']
        assert client.get(path + '/session').json()['activeRun'] is None
    finally:
        release.set()
        for step in finish_steps:
            step.set()


def test_legacy_disconnect_continues_and_explicit_stop_still_cancels(chat_server, monkeypatch):
    client, role, _ = chat_server
    entered, cancelled = threading.Event(), threading.Event()

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            try:
                yield '部分内容'
                entered.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(main, 'model_adapter', Model())
    path = f'/api/roles/{role.id}'
    with client.stream('POST', path + '/messages', json={'content': '继续'}) as response:
        run_id = response.headers['X-Meido-Run-Id']
        for event in events(response):
            if event.get('delta'):
                break
    assert entered.wait(3)
    assert not cancelled.wait(.2)
    assert client.get(path + '/session').json()['activeRun']['runId'] == run_id
    stopped = client.post(path + '/runs/' + run_id + '/stop')
    assert stopped.status_code == 200
    assert cancelled.wait(3)
    assert stopped.json()['messages'][-1]['content'] == '部分内容'
    assert stopped.json()['messages'][-1]['metadata']['runtimeReason'] == 'cancelled'


def test_legacy_fast_reply_replays_final_messages_when_deltas_overflow_before_subscribing(chat_server, monkeypatch):
    from backend.app.chat_runs import ChatRun
    client, role, _ = chat_server
    original = ChatRun.subscribe

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            for _ in range(300):
                yield '字'

    async def late_subscriber(self, sessions, after=None):
        # A slow response consumer is independent from the accepted task.
        await asyncio.shield(self.task)
        async for event in original(self, sessions, after):
            yield event

    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(ChatRun, 'subscribe', late_subscriber)
    response = client.post(f'/api/roles/{role.id}/messages', json={'content': '快速完成'})
    assert response.status_code == 200
    blocks = [(block.splitlines()[0], json.loads(block.split('data: ')[1]))
        for block in response.text.strip().split('\n\n') if 'data: ' in block]
    assert [kind for kind, _ in blocks].count('event: user_message_accepted') == 1
    assert [kind for kind, _ in blocks].count('event: assistant_completed') == 1
    assert blocks[-1][1]['message']['content'] == '字' * 300
    assert len(main.session_store.list_messages(f'role:{role.id}')) == 2


def test_reopened_tool_phase_keeps_durable_thinking_before_the_tool(chat_server, monkeypatch):
    from backend.app.agent_runtime import AgentToolResult, ToolDefinition
    from backend.app.ai.model_types import (ModelAssistantMessage, ModelResponseMetadata, ModelStreamDone,
        ModelThinkingBlock, ModelToolCallBlock, ModelTextBlock)
    client, role, _ = chat_server
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Tool:
        definition = ToolDefinition('list_dir', 'fixture', {'type': 'object', 'properties': {}})
        async def execute(self, arguments, context, on_update=None):
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            return AgentToolResult('真实回执')

    class Model:
        async def stream_rich_messages(self, messages, configuration, tools):
            calls.append(messages)
            blocks = ((ModelThinkingBlock('查看资料'), ModelToolCallBlock('call', 'list_dir', {}))
                if len(calls) == 1 else (ModelThinkingBlock('查看资料'), ModelTextBlock('完成')))
            yield ModelStreamDone(ModelAssistantMessage(blocks,
                ModelResponseMetadata(stop_reason='tool_use' if len(calls) == 1 else 'stop')))

    monkeypatch.setattr(main, '_runtime_tool_candidates', lambda *args, **kwargs: [Tool()])
    monkeypatch.setattr(main, 'model_adapter', Model())
    path = f'/api/roles/{role.id}'
    try:
        assert client.post(path + '/runs', json={'content': '查看', 'requestId': 'thinking-tool'}).status_code == 200
        assert entered.wait(3)
        data = client.get(path + '/session').json()
        assert not any(item['role'] == 'assistant' for item in data['messages'])
        process = data['activeRun']['process']
        assert [item['type'] for item in process] == ['thinking', 'tool']
        assert process[0]['text'] == '查看资料'
        release.set()
        completed = wait_for(lambda: (value if (value := client.get(path + '/runs/run-thinking-tool').json())['run']['status'] == 'completed' else None))
        thinking = [item for item in completed['messages'][-1]['metadata']['process'] if item['type'] == 'thinking']
        assert [item['text'] for item in thinking] == ['查看资料', '查看资料']
        assert thinking[0]['id'] != thinking[1]['id']
    finally:
        release.set()


@pytest.mark.parametrize('cancel', ['stop', 'permission'])
def test_natural_queued_supplement_survives_disconnect_and_stop_preserves_previous_tool(chat_server, monkeypatch, cancel):
    from backend.app.agent_runtime import AgentToolResult, ToolDefinition
    client, role, _ = chat_server
    main.store.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
        agentConfig=role.agentConfig.model_copy(update={'naturalChatEnabled': True})))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class Tool:
        definition = ToolDefinition('list_dir', 'fixture', {'type': 'object', 'properties': {}})
        async def execute(self, arguments, context, on_update=None):
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            finished.set()
            return AgentToolResult('真实回执')

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append(messages)
            if len(calls) == 1:
                yield ModelToolCall('slow', 'list_dir', {})
            else:
                yield ModelToolCall('finish', 'finish_reply', {'messages': ['补充后的回复']})
            yield ModelResponseDone('tool_use')

    monkeypatch.setattr(main, '_runtime_tool_candidates', lambda *args, **kwargs: [Tool()])
    monkeypatch.setattr(main, 'model_adapter', Model())
    path = f'/api/roles/{role.id}'
    try:
        first = client.post(path + '/runs', json={'content': '第一条', 'requestId': 'natural-first'})
        assert first.status_code == 200, first.text
        assert entered.wait(3)
        with client.stream('POST', path + '/messages', json={'content': '补充', 'requestId': 'natural-queued'}) as response:
            assert response.status_code == 200
            assert response.headers['X-Meido-Run-Id'] == 'run-natural-queued'
        queued = client.get(path + '/runs/run-natural-queued').json()
        assert queued['run']['phase'] == 'queued'
        assert [item['content'] for item in queued['messages'] if item['role'] == 'user'] == ['第一条', '补充']
        if cancel == 'stop':
            with ThreadPoolExecutor() as pool:
                stopping = pool.submit(client.post, path + '/runs/run-natural-queued/stop')
                with pytest.raises(TimeoutError):
                    stopping.result(.15)
                assert not finished.is_set()
                release.set()
                assert stopping.result(3).status_code == 200
        else:
            main.store.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
                agentConfig=role.agentConfig.model_copy(update={'naturalChatEnabled': False})))
            release.set()
            wait_for(lambda: main.session_store.get_run('run-natural-queued').status == 'cancelled')
        assert finished.is_set()
        assert len(calls) == 1
        records = main.session_store.list_messages(f'role:{role.id}')
        assert [item.content for item in records if item.messageType == 'tool_result'] == ['真实回执']
        assert main.session_store.get_run('run-natural-queued').status == 'cancelled'
    finally:
        release.set()


def test_terminal_waits_for_host_cleanup_and_old_stop_cannot_cancel_next_run(chat_server, monkeypatch):
    client, role, _ = chat_server
    cleaning, release, second_started = threading.Event(), threading.Event(), threading.Event()
    original = main.browser_use_sessions.close_run
    count = 0

    async def close(role_id, run_id):
        if run_id == 'run-cleanup-first':
            cleaning.set()
            await asyncio.to_thread(release.wait, 5)
        await original(role_id, run_id)

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            nonlocal count
            count += 1
            yield '完整回复' if count == 1 else '第二条'
            if count == 2:
                second_started.set()
                await asyncio.Event().wait()

    monkeypatch.setattr(main.browser_use_sessions, 'close_run', close)
    monkeypatch.setattr(main, 'model_adapter', Model())
    path = f'/api/roles/{role.id}'
    try:
        first = client.post(path + '/runs', json={'content': '第一条', 'requestId': 'cleanup-first'})
        assert first.status_code == 200
        assert cleaning.wait(3)
        assert main.session_store.get_run('run-cleanup-first').status == 'completed'
        assert client.get(path + '/session').json()['activeRun']['status'] == 'running'
        assert client.delete(path).status_code == 409
        with ThreadPoolExecutor() as pool:
            reading = pool.submit(client.get, path + '/runs/run-cleanup-first/events?after=0')
            stopping = pool.submit(client.post, path + '/runs/run-cleanup-first/stop')
            with pytest.raises(TimeoutError):
                reading.result(.15)
            with pytest.raises(TimeoutError):
                stopping.result(.15)
            release.set()
            assert 'event: assistant_completed' in reading.result(3).text
            assert stopping.result(3).status_code == 200
        second = client.post(path + '/runs', json={'content': '第二条', 'requestId': 'cleanup-second'})
        assert second.status_code == 200, second.text
        assert second_started.wait(3)
        late = client.post(path + '/runs/run-cleanup-first/stop')
        assert late.json()['activeRun']['runId'] == 'run-cleanup-second'
        assert main.session_store.get_run('run-cleanup-second').status == 'running'
        assert client.post(path + '/runs/run-cleanup-second/stop').status_code == 200
    finally:
        release.set()


def test_unconsumed_and_duplicate_subscriptions_are_bounded_and_recover_gaps(tmp_path, monkeypatch, local_platform_only):
    from types import SimpleNamespace
    from test_api import configure_session_api
    from backend.app.chat_runs import MAX_EVENTS, MAX_EVENT_BYTES, MAX_TOOL_CALLS

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            for _ in range(300):
                yield '字'

    role, sessions, _ = configure_session_api(tmp_path, monkeypatch, Model())

    async def scenario():
        response = await main.send_role_message(role.id, main.SendMessageInput(content='多订阅'), SimpleNamespace())
        turn = main._chat_runs().runs[response.headers['X-Meido-Run-Id']]
        slow = turn.subscribe(sessions, 0)
        duplicate = turn.subscribe(sessions, 0)
        await asyncio.wait_for(turn.task, 5)
        assert turn.finished
        assert len(turn.events) <= MAX_EVENTS and turn.event_bytes <= MAX_EVENT_BYTES
        assert len(turn.calls) <= MAX_TOOL_CALLS
        first = await anext(slow)
        assert 'run_snapshot' in first
        recovered = json.loads(first.split('data: ')[1])
        assert recovered['sequence'] == turn.sequence and recovered['status'] == 'completed'
        assert await anext(duplicate) == first
        assert sessions.list_messages(f'role:{role.id}')[-1].content == '字' * 300
        await slow.aclose()
        await duplicate.aclose()
    asyncio.run(scenario())


def test_stop_before_owner_first_step_and_shutdown_drain_accepted_runs(tmp_path, monkeypatch, local_platform_only):
    from test_api import configure_session_api
    from backend.app.models import SendMessageInput

    entered = asyncio.Event()
    calls = []

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            calls.append('called')
            entered.set()
            yield '已经开始'
            await asyncio.Event().wait()

    role, sessions, _ = configure_session_api(tmp_path, monkeypatch, Model())

    async def scenario():
        first = await main._accept_role_message(role.id, SendMessageInput(content='开始前', requestId='before-start'))
        await main._chat_runs().stop(first.runId)
        assert not calls
        assert sessions.get_run(first.runId).status == 'cancelled'
        second = await main._accept_role_message(role.id, SendMessageInput(content='退出', requestId='shutdown'))
        await asyncio.wait_for(entered.wait(), 3)
        await main._chat_runs().close()
        assert sessions.get_run(second.runId).status == 'cancelled'
        assert not main.runtime_manager.lock_for(role.id).locked()
        assert all(turn.finished for turn in main._chat_runs().runs.values())
        assert all(row.status != 'streaming' for row in sessions.list_messages(f'role:{role.id}'))
    asyncio.run(scenario())


def test_restart_fails_queued_and_running_records_without_replaying(tmp_path):
    from datetime import datetime, timezone
    from backend.app.models import AgentRun
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('r')
    for index, status in enumerate(('running', 'queued')):
        sessions.create_run_with_messages(AgentRun(runId=f'run-{index}', roleId='r', sessionKey='role:r',
            status=status, startedAt=datetime.now(timezone.utc)), '已接受')
    assert sessions.active_run('r').runId == 'run-0'
    recovered = SessionStore(tmp_path / 'sessions.db')
    assert [recovered.get_run(f'run-{index}').status for index in range(2)] == ['failed', 'failed']
    assert [row.status for row in recovered.list_messages('role:r')] == ['completed', 'failed', 'completed', 'failed']
    assert recovered.active_run('r') is None
