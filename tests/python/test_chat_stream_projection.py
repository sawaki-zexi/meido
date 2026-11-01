"""Active desktop output is recoverable without a SQLite commit per fragment."""
import asyncio
import threading

import pytest

from backend.app import main
from backend.app.ai.model_types import (ModelAssistantMessage, ModelResponseMetadata, ModelStreamDone,
    ModelStreamError, ModelTextBlock, ModelTextDelta, ModelThinkingBlock, ModelThinkingDelta)
from test_chat_lifecycle import events, read_run_snapshot, wait_for
from test_chat_stop import chat_server


def test_partial_save_failure_ends_owner_without_claiming_saved_thinking(chat_server, monkeypatch):
    client, role, _ = chat_server
    entered = threading.Event()
    original = main.session_store.update_message
    requests = []

    class Model:
        async def stream_rich_messages(self, messages, configuration, tools):
            requests.append(messages)
            if len(requests) == 1:
                yield ModelThinkingDelta('尚未落盘的思考')
                yield ModelTextDelta('已输出部分正文')
                entered.set()
                await asyncio.Event().wait()
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock('下一条成功'),),
                ModelResponseMetadata(stop_reason='stop')))

    def failing_partial(*args, **kwargs):
        if kwargs.get('metadata', {}).get('streamThinking'):
            raise OSError('fixture: partial thinking could not be saved')
        return original(*args, **kwargs)

    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(main.session_store, 'update_message', failing_partial)
    path = f'/api/roles/{role.id}'
    accepted = client.post(path + '/runs', json={'content': '保存失败', 'requestId': 'partial-save-failure'})
    assert accepted.status_code in {200, 202}
    assert entered.wait(3)
    stopped = client.post(path + '/runs/run-partial-save-failure/stop')
    assert stopped.status_code == 500, stopped.text
    assert '可能未保存' in stopped.json()['detail']
    assert client.get(path + '/session').json()['activeRun'] is None
    recovered = client.get(path + '/runs/run-partial-save-failure').json()
    assert recovered['run']['status'] == 'failed' and '可能未保存' in recovered['run']['error']
    message = recovered['messages'][-1]
    assert message['content'] == '已输出部分正文'
    assert not any(item['type'] == 'thinking' for item in message['metadata']['process'])
    turn = main._chat_runs().runs['run-partial-save-failure']
    assert turn.finished and turn.current_message is None
    assert main._chat_runs().active(role.id) is None
    with client.stream('GET', path + '/runs/run-partial-save-failure/events?after=0') as response:
        payloads = list(events(response))
    assert payloads[-1]['status'] == 'failed' and '可能未保存' in payloads[-1]['error']
    stored = main.session_store.get_run('run-partial-save-failure')
    assert stored.status == 'cancelled' and '可能未保存' in stored.error
    main._chat_runs().runs.pop(stored.runId)
    assert client.get(path + f'/runs/{stored.runId}').json()['run']['status'] == 'failed'
    monkeypatch.setattr(main, '_chat_run_service', None)
    restored = client.get(path + f'/runs/{stored.runId}').json()
    assert restored['run']['status'] == 'failed' and '可能未保存' in restored['run']['error']
    assert restored['messages'][-1] == message
    monkeypatch.setattr(main.session_store, 'update_message', original)
    assert 'event: assistant_completed' in client.post(path + '/messages', json={'content': '继续'}).text
    assert len(requests) == 2


@pytest.mark.parametrize('persist_error', [True, False])
def test_completed_model_cleanup_error_survives_owner_eviction(chat_server, monkeypatch, persist_error):
    client, role, _ = chat_server
    service = main._chat_runs()
    start = service.start
    writes = []
    record = main.session_store.record_run_error

    def save_error(run_id, error):
        writes.append(run_id)
        if not persist_error:
            raise OSError('fixture error storage unavailable')
        record(run_id, error)

    class Model:
        async def stream_reply(self, role, history, configuration=None):
            yield '真实已保存的完整正文'

    def failing_start(run, execute, finish):
        async def failing_finish(turn):
            await finish(turn)
            raise OSError('fixture host cleanup failed after model completion')
        return start(run, execute, failing_finish)

    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(service, 'start', failing_start)
    monkeypatch.setattr(main.session_store, 'record_run_error', save_error)
    path = f'/api/roles/{role.id}'
    response = client.post(path + '/runs', json={'content': '完整正文后收尾失败', 'requestId': 'late-cleanup'})
    assert response.status_code in {200, 202}
    wait_for(lambda: service.runs['run-late-cleanup'].finished)
    stored = main.session_store.get_run('run-late-cleanup')
    assert stored.status == 'completed' and writes == [stored.runId]
    live = client.get(path + f'/runs/{stored.runId}').json()
    assert live['run']['status'] == 'failed' and '可能未保存' in live['run']['error']
    if not persist_error:
        assert stored.error is None  # The owner must not claim its error was saved.
        return
    assert '可能未保存' in stored.error
    for new_service in (False, True):
        service.runs.pop(stored.runId, None)
        if new_service:
            monkeypatch.setattr(main, '_chat_run_service', None)
        recovered = client.get(path + f'/runs/{stored.runId}')
        assert recovered.status_code == 200, recovered.text
        value = recovered.json()
        assert value['run']['status'] == 'failed' and '可能未保存' in value['run']['error']
        assert value['messages'][-1]['content'] == '真实已保存的完整正文'
        assert value['messages'][-1]['status'] == 'completed'


@pytest.mark.parametrize('terminal', ['complete', 'error', 'stop'])
def test_live_gap_recovery_is_complete_and_writes_only_at_message_boundaries(chat_server, monkeypatch, terminal):
    client, role, other = chat_server
    entered, release = threading.Event(), threading.Event()
    writes, requests = [], []
    text, thought = 'visible text ' * 30, '真实思考'
    original = main.session_store.update_message

    def update(*args, **kwargs):
        writes.append(args[0])
        return original(*args, **kwargs)

    class Model:
        async def stream_rich_messages(self, messages, configuration, tools):
            requests.append(messages)
            for _ in range(100):
                yield ModelThinkingDelta(thought)
                yield ModelTextDelta(text)
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            if terminal == 'error':
                yield ModelStreamError('fixture upstream failure')
            else:
                yield ModelStreamDone(ModelAssistantMessage((ModelThinkingBlock(thought * 100), ModelTextBlock(text * 100)),
                    ModelResponseMetadata(stop_reason='stop')))

    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(main.session_store, 'update_message', update)
    path = f'/api/roles/{role.id}'
    run_id = f'run-projection-{terminal}'
    try:
        accepted = client.post(path + '/runs', json={'content': '查看当前回复', 'requestId': f'projection-{terminal}'})
        assert accepted.status_code in {200, 202}, accepted.text
        assert entered.wait(4)
        assert writes == [], 'stream fragments must not commit individual placeholder updates'
        # More than 128 events: the bounded ring cannot replay the full response.
        with client.stream('GET', path + f'/runs/{run_id}/events?after=0') as stream:
            assert next(events(stream))['sequence'] > 128
        for endpoint in ('/session', f'/runs/{run_id}'):
            recovered = client.get(path + endpoint)
            assert recovered.status_code == 200, recovered.text
            message = recovered.json()['messages'][-1]
            assert message['content'] == text * 100  # also exceeds the thinking display limit
            assert message['metadata']['streamThinking'] == thought * 100
            assert [entry['text'] for entry in message['metadata']['process'] if entry['type'] == 'thinking'] == [thought * 100]
        assert client.get(f'/api/roles/{other.id}/session').json()['messages'] == []
        persisted = main.session_store.list_messages(f'role:{role.id}')[-1]
        assert persisted.content == '' and 'streamThinking' not in persisted.metadata
        if terminal == 'stop':
            final = client.post(path + f'/runs/{run_id}/stop')
            assert final.status_code == 200
            final_message = final.json()['messages'][-1]
        else:
            release.set()
            final = wait_for(lambda: read_run_snapshot(client.get(path + f'/runs/{run_id}'), run_id,
                statuses={'completed', 'failed'}))
            final_message = final['messages'][-1]
        assert final_message['content'] == text * 100
        assert final_message['status'] == ('completed' if terminal == 'complete' else 'failed')
        assert [entry['text'] for entry in final_message['metadata']['process'] if entry['type'] == 'thinking'] == [thought * 100]
        assert len(writes) <= 2 and len(requests) == 1
        # Once the owner finishes, recovery uses the durable row with no live callback.
        assert main._chat_runs().runs[run_id].current_message is None
        reopened = client.get(path + '/session').json()['messages'][-1]
        assert reopened == final_message
    finally:
        release.set()
        client.post(path + f'/runs/{run_id}/stop')
