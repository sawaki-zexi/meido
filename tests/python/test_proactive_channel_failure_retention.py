"""Real OneBot failures release only finished Runtime headroom."""
import asyncio
from contextlib import closing
import json
import sqlite3
import threading

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.channels.transports import NapCatWebSocketTransport
from test_mcp_http import process_alive
from test_proactive_channel_guards import decision, run
from test_proactive_channel_human_priority import owner_http
from test_proactive_targets_api import api, shell_api, source  # noqa: F401


pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.fixture(params=['failed', 'uncertain'])
def failure_platform(request):
    """Fault only the real localhost OneBot protocol, never the adapter/host."""
    state = {'outcome': request.param, 'calls': [], 'sendRequests': [], 'accepted': [],
        'rejections': [], 'disconnects': 0, 'rpcStopped': 0, 'port': None, 'errors': []}
    ready, stop = threading.Event(), threading.Event()

    async def peer(socket):
        try:
            async for raw in socket:
                packet = json.loads(raw)
                state['calls'].append(packet)
                if packet['action'] == 'get_friend_list':
                    await socket.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': packet['echo'],
                        'data': [{'user_id': 101, 'nickname': 'Original owner'}]}))
                elif packet['action'] == 'send_private_msg':
                    state['sendRequests'].append(packet)
                    if request.param == 'failed':
                        rejection = {'status': 'failed', 'retcode': 1401, 'echo': packet['echo'],
                            'wording': 'explicit synthetic platform rejection', 'data': None}
                        state['rejections'].append(rejection)
                        await socket.send(json.dumps(rejection))
                    else:
                        # The protocol peer records acceptance, then withholds
                        # every delivery receipt. Never synthesize local sent.
                        state['accepted'].append(packet)
                        state['disconnects'] += 1
                        await socket.close(code=1011, reason='accepted request; receipt unavailable')
                        return
                else:
                    raise AssertionError(packet['action'])
        except ConnectionClosed:
            pass
        except BaseException as error:
            state['errors'].append(error)
            raise

    async def serve_platform():
        async with serve(peer, '127.0.0.1', 0) as server:
            state['port'] = server.sockets[0].getsockname()[1]
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(.01)

    def host():
        try:
            asyncio.run(serve_platform())
        except BaseException as error:
            state['errors'].append(error)
            ready.set()

    thread = threading.Thread(target=host, name='proactive-failure-onebot')
    thread.start()

    async def rpc(action, params):
        transport = NapCatWebSocketTransport(f"ws://127.0.0.1:{state['port']}", request_timeout=3)

        async def ingress(_):
            raise AssertionError('this protocol fixture has no inbound account events')

        try:
            await transport.start(ingress)
            await transport.wait_connected()
            return await transport(action, params)
        finally:
            await transport.stop()
            state['rpcStopped'] += 1

    try:
        assert ready.wait(10) and state['port'] and not state['errors'], state['errors']
        yield state, rpc
    finally:
        stop.set()
        thread.join(10)
        assert not thread.is_alive(), 'localhost OneBot peer still owns its loop'
        assert not state['errors'], state['errors']


def observe_ledgers(sessions, tasks, role, tick_id):
    """Read actual durable duties without invoking or rewriting retention."""
    with closing(sqlite3.connect(sessions.database_path)) as db:
        db.row_factory = sqlite3.Row
        runs = [dict(row) for row in db.execute('SELECT * FROM autonomy_runs WHERE role_id=?', (role,))]
        claims = [dict(row) for row in db.execute('SELECT * FROM autonomy_deliveries WHERE tick_id=?', (tick_id,))]
        recovery = dict(db.execute('SELECT * FROM autonomy_delivery_recovery WHERE tick_id=?', (tick_id,)).fetchone())
        reservation = dict(db.execute('SELECT * FROM autonomy_storage_reservations WHERE operation_id=?', (tick_id,)).fetchone())
    assert len(runs) == 1 and runs[0]['tick_id'] == tick_id
    assert runs[0]['runtime_acknowledged'] == 1 and runs[0]['result_ack_pending'] == 0
    assert runs[0]['runtime_result'] is not None and runs[0]['launch_pending'] == 0
    assert len(claims) == 1 and claims[0]['dispatched'] == 1
    with closing(sqlite3.connect(tasks.root / 'tasks.db')) as db:
        retained = db.execute('SELECT retain_until_ack,ended_at FROM tasks WHERE task_id=?', (runs[0]['task_id'],)).fetchone()
        assert retained is not None and retained[0] == 0 and retained[1] is not None
        assert db.execute('SELECT COUNT(*) FROM tasks WHERE role_id=? AND retain_until_ack<>0', (role,)).fetchone()[0] == 0
    return {'run': runs[0], 'claim': claims[0], 'recovery': recovery, 'reservation': reservation}


def test_real_a01_protocol_failure_keeps_one_claim_and_only_bounded_business_headroom(api, monkeypatch, failure_platform, source):
    outcome = failure_platform[0]['outcome']
    with decision(api, monkeypatch, failure_platform, source) as ctx:
        app, role = ctx['app'], ctx['role']

        class Model:
            requests = 0

            async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
                self.requests += 1
                if self.requests == 1:
                    name, arguments = 'get_content_events', {}
                elif self.requests == 2:
                    item = json.loads(messages[-1].content[0].text)[0]['id']
                    name, arguments = 'message_push', {'message': 'one-original-negative-channel-attempt',
                        'mood': '', 'thought': '', 'evidence': [item]}
                else:
                    assert self.requests == 3, 'recovery must not request another model turn'
                    name, arguments = 'finish_turn', {'decision': 'reply'}
                yield ModelStreamDone(ModelAssistantMessage(content=(
                    ModelToolCallBlock(f'negative-platform-{self.requests}', name, arguments),)))

        model = Model()
        monkeypatch.setattr(main, 'model_adapter', model)
        with owner_http() as (_, invoke):
            try:
                final = invoke(run(ctx))
                assert len(final['runs']) == 1 and final['runs'][0]['status'] == outcome
                assert final['runs'][0]['runtimeAcknowledged'] is True and not final['cleanupPending']
                tick_id = final['runs'][0]['id']
                delivery = final['deliveries'][0]
                assert len(final['deliveries']) == 1 and delivery['status'] == outcome
                assert delivery['target']['ref'] == ctx['selected']['targetRef']
                assert delivery['attemptId'] and delivery['platformMessageId'] is None and delivery['messageId'] is None
                assert delivery['projectionPending'] is (outcome == 'uncertain')
                assert final['acknowledgements'] == [] and model.requests == 3
                state = failure_platform[0]
                assert [call['action'] for call in state['calls']] == ['get_friend_list', 'send_private_msg']
                assert len(state['sendRequests']) == 1 and state['sendRequests'][0]['params']['user_id'] == 101
                if outcome == 'failed':
                    assert len(state['rejections']) == 1 and state['rejections'][0]['retcode'] != 0
                    assert state['rejections'][0]['status'] == 'failed' and not state['accepted']
                else:
                    assert len(state['accepted']) == 1 and state['disconnects'] == 1 and not state['rejections']
                assert state['rpcStopped'] == 2
                attempts = ctx['channels'].list_deliveries(role)
                assert len(attempts) == 1 and attempts[0]['status'] == outcome
                assert attempts[0]['attemptId'] == delivery['attemptId']
                assert attempts[0]['sourceRunId'] == tick_id and attempts[0]['sourceToolCallId'] == delivery['intentId']
                before = observe_ledgers(api[2], api[3], role, tick_id)
                calls = [json.loads(line) for line in (source[0] / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
                assert [call['name'] for call in calls] == ['get_events']
                assert not process_alive(int((source[0] / 'pid').read_text()))

                # Exercise only the original local recovery identity, including
                # application reconstruction. Never call account_send directly.
                invoke(app.recover())
                invoke(app.close())
                monkeypatch.setattr(main, '_proactive', None)
                app = main._proactive_application()
                invoke(app.recover())
                invoke(app.recover())
                restored = app.query(role)
                assert len(restored['runs']) == 1 and restored['runs'][0]['id'] == tick_id
                assert restored['runs'][0]['runtimeAcknowledged'] is True
                assert restored['deliveries'][0]['intentId'] == delivery['intentId']
                assert restored['deliveries'][0]['status'] == outcome
                after = observe_ledgers(api[2], api[3], role, tick_id)
                assert after['claim'] == before['claim'] and after['recovery'] == before['recovery']
                assert len(ctx['channels'].list_deliveries(role)) == 1
                assert len(state['sendRequests']) == 1 and state['rpcStopped'] == 2 and model.requests == 3
                assert [json.loads(line) for line in (source[0] / 'calls.jsonl').read_text(encoding='utf-8').splitlines()] == calls
                for channel_thread in ctx['channels'].list_threads(role):
                    assert ctx['channels'].list_messages(role, channel_thread.thread_id)[1] == []

                # These requirements come from the documented finished-runtime
                # contract, not from calling _finished/_resolved/settle helpers.
                reservation = after['reservation']
                assert reservation['settled'] == 1, reservation
                headroom = (reservation['remaining_bytes'], reservation['rows_reserved'], reservation['metadata_reserved'])
                if outcome == 'failed':
                    assert headroom == (0, 0, 0), reservation
                else:
                    assert 0 < headroom[0] <= 64 * 1024, reservation
                    assert 0 < headroom[1] <= 8 and 0 < headroom[2] <= 8 * 1024, reservation
                    assert after['recovery']['projection_pending'] == 1
            finally:
                try:
                    invoke(app.close())
                finally:
                    try:
                        invoke(main.channel_host.stop(ctx['account'].account_id))
                    finally:
                        invoke(main.mcp_host.close())
