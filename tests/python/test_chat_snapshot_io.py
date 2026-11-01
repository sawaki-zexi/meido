"""Recovery reads must not block unrelated HTTP work or skip concurrent events."""
import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.app import main
from test_chat_stop import chat_server
from test_chat_lifecycle import wait_for


def test_acceptance_does_not_render_saved_history(tmp_path, monkeypatch, local_platform_only):
    from test_api import configure_session_api
    from backend.app.models import SendMessageInput
    from backend.app.agent_runtime.runtime import RuntimeManager

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            yield '回复'

    role, sessions, _ = configure_session_api(tmp_path, monkeypatch, Model())
    monkeypatch.setattr(main, 'runtime_manager', RuntimeManager(sessions))
    sessions.open_role_session(role.id)
    for index in range(160):
        sessions.append_message(f'role:{role.id}', 'user' if index % 2 == 0 else 'assistant',
            'saved history ' * 100, 'completed')
    presented = []
    original = main.session_manager.present_message

    def present(*args, **kwargs):
        presented.append(args[0].id)
        return original(*args, **kwargs)

    monkeypatch.setattr(main.session_manager, 'present_message', present)

    async def scenario():
        run = await main._accept_role_message(role.id, SendMessageInput(content='接受输入'))
        try:
            assert presented == [], 'admission must not project the whole conversation for display'
            assert sessions.get_run(run.runId) is not None
        finally:
            await main._chat_runs().stop(run.runId)

    asyncio.run(scenario())


def test_recovery_uses_one_raw_history_batch(chat_server, monkeypatch):
    client, role, _ = chat_server
    entered = threading.Event()

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            yield '当前片段'
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(main, 'model_adapter', Model())
    path = f'/api/roles/{role.id}'
    accepted = client.post(path + '/runs', json={'content': '恢复', 'requestId': 'one-history'})
    assert accepted.status_code in {200, 202}
    assert entered.wait(3)
    reads = []
    original = main.session_store.list_messages

    def read(session_key):
        reads.append(session_key)
        return original(session_key)

    monkeypatch.setattr(main.session_store, 'list_messages', read)
    try:
        for endpoint in ('/session', '/runs/run-one-history'):
            reads.clear()
            recovered = client.get(path + endpoint)
            assert recovered.status_code == 200, recovered.text
            assert [item['content'] for item in recovered.json()['messages']] == ['恢复', '当前片段']
            assert reads == [f'role:{role.id}'], 'one consistent raw batch must feed messages and process'
    finally:
        client.post(path + '/runs/run-one-history/stop')


@pytest.mark.parametrize('endpoint', ['session', 'run', 'gap'])
def test_slow_sqlite_recovery_keeps_other_pages_available(chat_server, monkeypatch, endpoint):
    client, role, _ = chat_server
    entered = threading.Event()
    release_read = threading.Event()

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            yield '部分内容'
            await asyncio.Event().wait()

    monkeypatch.setattr(main, 'model_adapter', Model())
    path = f'/api/roles/{role.id}'
    for index in range(160):
        main.session_store.append_message(f'role:{role.id}', 'user' if index % 2 == 0 else 'assistant',
            'word ' * 512, 'completed')
    accepted = client.post(path + '/runs', json={'content': '本地检查', 'requestId': 'slow-sqlite'})
    assert accepted.status_code in {200, 202}, accepted.text
    original = main._desktop_run if endpoint == 'gap' else main.session_store.open_role_session

    def observed(*args, **kwargs):
        entered.set()
        if endpoint == 'gap':
            assert release_read.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(main if endpoint == 'gap' else main.session_store,
        '_desktop_run' if endpoint == 'gap' else 'open_role_session', observed)

    def read():
        if endpoint != 'gap':
            return client.get(path + ('/session' if endpoint == 'session' else '/runs/run-slow-sqlite')).status_code
        with client.stream('GET', path + '/runs/run-slow-sqlite/events?after=-1') as response:
            assert response.status_code == 200
            assert next(line for line in response.iter_lines() if line.startswith('event: ')) == 'event: run_snapshot'
            return response.status_code

    try:
        with sqlite3.connect(main.session_store.database_path) as writer, ThreadPoolExecutor(max_workers=2) as pool:
            # Keep the review's 0.8s real writer lock and 160-row/82k-token input.
            # The events endpoint is read-only, so its probe uses EXCLUSIVE.
            writer.execute('BEGIN EXCLUSIVE' if endpoint == 'gap' else 'BEGIN IMMEDIATE')
            reading = pool.submit(read)
            assert entered.wait(3)
            health = pool.submit(client.get, '/api/health')
            threading.Event().wait(.8)
            health_before_release = health.done()
            writer.rollback()
            release_read.set()
            assert reading.result(3) == 200
            assert health.result(3).status_code == 200
            assert health_before_release, 'SQLite recovery blocked an unrelated health request'
    finally:
        release_read.set()
        client.post(path + '/runs/run-slow-sqlite/stop')


def test_recovery_retries_when_delta_arrives_after_the_worker_read(chat_server, monkeypatch):
    client, role, _ = chat_server
    add_delta, delta_ready, finish = threading.Event(), threading.Event(), threading.Event()
    read_ready, release_read = threading.Event(), threading.Event()

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            yield 'A'
            await asyncio.to_thread(add_delta.wait, 5)
            yield 'B'
            delta_ready.set()
            await asyncio.to_thread(finish.wait, 5)
            yield 'C'

    monkeypatch.setattr(main, 'model_adapter', Model())
    path = f'/api/roles/{role.id}'
    assert client.post(path + '/runs', json={'content': '恢复', 'requestId': 'read-race'}).status_code in {200, 202}
    original = main.session_manager.present_session
    held = False

    def hold_one_read(*args):
        nonlocal held
        data = original(*args)
        if not held:
            held = True
            read_ready.set()
            assert release_read.wait(5)
        return data

    monkeypatch.setattr(main.session_manager, 'present_session', hold_one_read)
    try:
        with ThreadPoolExecutor() as pool:
            reading = pool.submit(client.get, path + '/runs/run-read-race')
            assert read_ready.wait(3)
            add_delta.set()
            concurrent_delta = delta_ready.wait(1)
            release_read.set()
            data = reading.result(3).json()
            assert concurrent_delta, 'the snapshot reader prevented model progress'
            assert data['messages'][-1]['content'] == 'AB'
            cursor = data['run']['sequence']
            with client.stream('GET', path + f'/runs/run-read-race/events?after={cursor}') as stream:
                finish.set()
                payloads = [json.loads(line[6:]) for line in stream.iter_lines() if line.startswith('data: ')]
            assert [item['delta'] for item in payloads if 'delta' in item] == ['C']
            assert payloads[-1]['message']['content'] == 'ABC'
    finally:
        add_delta.set()
        release_read.set()
        finish.set()
        client.post(path + '/runs/run-read-race/stop')


def test_busy_snapshots_are_bounded_and_confirm_the_already_accepted_run(chat_server, monkeypatch):
    client, role, _ = chat_server
    pause, paused, model_entered = threading.Event(), threading.Event(), threading.Event()
    model_calls = []

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            model_calls.append(role.id)
            model_entered.set()
            while not pause.is_set():
                yield 'x'
                await asyncio.sleep(.002)
            paused.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(main, 'model_adapter', Model())
    original = main._read_desktop_projection

    def slow_projection(*args):
        value = original(*args)
        assert model_entered.wait(3)
        time.sleep(.03)
        return value

    monkeypatch.setattr(main, '_read_desktop_projection', slow_projection)
    path = f'/api/roles/{role.id}'
    try:
        accepted = client.post(path + '/runs', json={'content': 'accepted', 'requestId': 'busy-view'})
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()['snapshotPending'] is True
        assert accepted.json()['run']['runId'] == 'run-busy-view'
        started = time.monotonic()
        unavailable = client.get(path + '/runs/run-busy-view')
        assert unavailable.status_code == 503
        assert time.monotonic() - started < 2
        assert unavailable.headers['X-Meido-Run-Id'] == 'run-busy-view'
        assert unavailable.headers['Retry-After'] == '1'
        # A gap is a nonblocking signal; the full history is fetched separately.
        with client.stream('GET', path + '/runs/run-busy-view/events?after=-1') as stream:
            assert next(line for line in stream.iter_lines() if line.startswith('event: ')) == 'event: run_snapshot'
        pause.set()
        assert paused.wait(3)
        recovered = client.get(path + '/runs/run-busy-view')
        assert recovered.status_code == 200
        rows = recovered.json()['messages']
        assert [row['content'] for row in rows if row['role'] == 'user'] == ['accepted']
        assert rows[-1]['content'] and set(rows[-1]['content']) == {'x'}
        assert model_calls == [role.id]
    finally:
        pause.set()
        client.post(path + '/runs/run-busy-view/stop')


def test_other_role_acceptance_does_not_invalidate_a_saved_snapshot(chat_server, monkeypatch):
    client, role, other = chat_server
    entered, release = threading.Event(), threading.Event()
    original = main._read_desktop_projection
    reads = []

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            yield '另一角色的回复'

    def held(*args):
        result = original(*args)
        if args[0] == role.id:
            reads.append(role.id)
            if len(reads) == 1:
                entered.set()
                assert release.wait(5)
        return result

    monkeypatch.setattr(main, 'model_adapter', Model())
    accepted = client.post(f'/api/roles/{role.id}/runs', json={'content': '已知身份', 'requestId': 'known-owner'})
    assert accepted.status_code in {200, 202}
    wait_for(lambda: main._chat_runs().runs['run-known-owner'].finished)
    monkeypatch.setattr(main, '_read_desktop_projection', held)
    try:
        with ThreadPoolExecutor() as pool:
            reading = pool.submit(client.get, f'/api/roles/{role.id}/session')
            assert entered.wait(3)
            accepted = client.post(f'/api/roles/{other.id}/runs', json={'content': '不相关', 'requestId': 'unrelated'})
            assert accepted.status_code in {200, 202}
            wait_for(lambda: main._chat_runs().runs['run-unrelated'].finished)
            release.set()
            response = reading.result(3)
        assert response.status_code == 200
        assert [item['content'] for item in response.json()['messages']] == ['已知身份', '另一角色的回复']
        assert response.json()['activeRun'] is None
        assert reads == [role.id], 'unrelated roles must not consume this snapshot retry allowance'
    finally:
        release.set()


def test_idle_snapshot_detects_same_role_run_even_after_owner_eviction(chat_server, monkeypatch):
    from backend.app import chat_runs
    client, role, other = chat_server
    entered, release = threading.Event(), threading.Event()
    original = main._read_desktop_projection
    held = False

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            yield '已完成'

    def hold_first(*args):
        nonlocal held
        value = original(*args)
        if args[0] == role.id and not held:
            held = True
            entered.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(main, '_read_desktop_projection', hold_first)
    monkeypatch.setattr(chat_runs, 'MAX_FINISHED_RUNS', 1)
    try:
        with ThreadPoolExecutor() as pool:
            reading = pool.submit(client.get, f'/api/roles/{role.id}/session')
            assert entered.wait(3)
            for target, request_id in ((role, 'evicted-owner'), (other, 'evicting-owner')):
                accepted = client.post(f'/api/roles/{target.id}/runs', json={'content': '新输入', 'requestId': request_id})
                assert accepted.status_code in {200, 202}
                wait_for(lambda: main._chat_runs().runs[f'run-{request_id}'].finished)
            assert 'run-evicted-owner' not in main._chat_runs().runs
            release.set()
            response = reading.result(3)
        assert response.status_code == 200
        assert [row['content'] for row in response.json()['messages']] == ['新输入', '已完成']
        assert response.json()['activeRun'] is None
    finally:
        release.set()


def test_idle_snapshot_detects_a_new_run_even_if_it_already_finished(chat_server, monkeypatch):
    client, role, _ = chat_server
    read_ready, release = threading.Event(), threading.Event()
    original = main._read_desktop_projection
    held = False

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            yield '新回复'

    def hold_idle_read(*args):
        nonlocal held
        value = original(*args)
        if not held:
            held = True
            read_ready.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(main, '_read_desktop_projection', hold_idle_read)
    path = f'/api/roles/{role.id}'
    try:
        with ThreadPoolExecutor() as pool:
            reading = pool.submit(client.get, path + '/session')
            assert read_ready.wait(3)
            accepted = client.post(path + '/runs', json={'content': '新输入', 'requestId': 'idle-new'})
            assert accepted.status_code in {200, 202}
            wait_for(lambda: main._chat_runs().runs['run-idle-new'].finished)
            release.set()
            data = reading.result(3).json()
        assert data['activeRun'] is None
        assert [row['content'] for row in data['messages']] == ['新输入', '新回复']
    finally:
        release.set()


def test_legacy_gap_does_not_repeat_deltas_included_by_a_concurrent_snapshot(chat_server, monkeypatch):
    from backend.app.chat_runs import ChatRun
    client, role, _ = chat_server
    initial, add_delta, delta_ready, finish = (threading.Event() for _ in range(4))
    read_ready, release_read, visible = (threading.Event() for _ in range(3))
    subscribe = ChatRun.subscribe
    project = main._read_desktop_projection
    held = False

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            for _ in range(150):
                yield 'A'
            initial.set()
            await asyncio.to_thread(add_delta.wait, 5)
            yield 'B'
            delta_ready.set()
            await asyncio.to_thread(finish.wait, 5)
            yield 'C'

    async def late_subscriber(self, sessions, after=None):
        await asyncio.to_thread(initial.wait, 5)
        async for item in subscribe(self, sessions, after):
            yield item

    def hold_read(*args):
        nonlocal held
        value = project(*args)
        if not held:
            held = True
            read_ready.set()
            assert release_read.wait(5)
        return value

    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(ChatRun, 'subscribe', late_subscriber)
    monkeypatch.setattr(main, '_read_desktop_projection', hold_read)

    def consume():
        payloads = []
        with client.stream('POST', f'/api/roles/{role.id}/messages', json={'content': '旧客户端恢复'}) as stream:
            assert stream.status_code == 200
            for line in stream.iter_lines():
                if line.startswith('data: '):
                    payload = json.loads(line[6:])
                    payloads.append(payload)
                    if payload.get('delta'):
                        visible.set()
        return payloads

    with ThreadPoolExecutor() as pool:
        reading = pool.submit(consume)
        try:
            assert read_ready.wait(5)
            add_delta.set()
            assert delta_ready.wait(3)
            release_read.set()
            assert visible.wait(3)
        finally:
            add_delta.set()
            release_read.set()
            finish.set()
        payloads = reading.result(5)
    assert ''.join(item['delta'] for item in payloads if 'delta' in item) == 'A' * 150 + 'BC'
    assert payloads[-1]['message']['content'] == 'A' * 150 + 'BC'
