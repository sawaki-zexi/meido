"""Active desktop output is recoverable without a SQLite commit per fragment."""
import asyncio
import threading

import pytest

from backend.app import main
from backend.app.ai.model_types import (ModelAssistantMessage, ModelResponseMetadata, ModelStreamDone,
    ModelStreamError, ModelTextBlock, ModelTextDelta, ModelThinkingBlock, ModelThinkingDelta)
from test_chat_lifecycle import events, wait_for
from test_chat_stop import chat_server


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
            final = wait_for(lambda: (data if (data := client.get(path + f'/runs/{run_id}').json())['run']['status']
                in {'completed', 'failed'} else None))
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
