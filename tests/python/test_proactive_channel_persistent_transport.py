"""Autonomous sends use the persistent channel owner's loop.

No worker debug mode, artificial RPC result, exception, or per-RPC transport.
The transport subclass only observes loop/thread/original Task, then delegates
unchanged to the real production implementation and localhost OneBot protocol.
This establishes the owned-loop contract; it does not claim a historical hang.
"""
import asyncio
import json
import threading

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.channels.transports import NapCatWebSocketTransport
from backend.app.mcp_runtime import McpHost
from test_proactive_channel_human_priority import owner_http
from test_proactive_drift_delivery import settled
from test_proactive_snapshot import grant_for
from test_proactive_targets_api import api, shell_api, platform, source, save_external  # noqa: F401


pytestmark = pytest.mark.usefixtures('local_platform_only')


class _ObservedPersistentTransport(NapCatWebSocketTransport):
    """Observation only: no bridge, result rewrite, throwing or new permission."""
    def __init__(self, *args, observations, **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = observations

    async def send_authorized(self, action, params, *, authorize=None):
        task, done = asyncio.current_task(), threading.Event()
        assert task is not None
        self.observations.append({'action': action, 'loop': asyncio.get_running_loop(),
            'thread': threading.get_ident(), 'task': task, 'doneSignal': done})
        # Keep the original Task reference as well as the callback signal. A
        # submitted/concurrent Future is not proof its owned coroutine joined.
        task.add_done_callback(lambda completed: done.set())
        return await super().send_authorized(action, params, authorize=authorize)


def test_real_a01_uses_one_persistent_napcat_transport_on_the_original_host_loop(
        api, monkeypatch, platform, source, tmp_path):
    _, _, sessions, tasks = api
    now, observations = [1_782_400_000.0], []
    host, state = McpHost(source[1]), {'heartbeat': 0, 'loopErrors': [], 'transportStarts': 0}
    monkeypatch.setattr(main, 'mcp_host', host)
    monkeypatch.setattr(main, '_proactive', None)
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    transport = _ObservedPersistentTransport(f"ws://127.0.0.1:{platform[0]['port']}",
        request_timeout=3, observations=observations)
    evidence_path = tmp_path / 'persistent-channel-transport-evidence.json'

    def record(stage, **fields):
        state.update(stage=stage, **fields)
        public = {key: value for key, value in state.items() if key not in {
            'hostLoop', 'reader', 'readerDoneSignal', 'heartbeatTask', 'exceptionHandler'}}
        public['rpcObservations'] = [{'action': item['action'], 'loopId': id(item['loop']),
            'thread': item['thread'], 'originalTaskDone': item['task'].done(),
            'originalTaskDoneSignal': item['doneSignal'].is_set()} for item in observations]
        evidence_path.write_text(json.dumps(public, indent=2), encoding='utf-8')

    with owner_http() as (client, invoke):
        role = client.post('/api/roles', json={'name': '持久渠道运行时', 'profile': {'profile': '只用本地持久协议'}}).json()['role']['id']
        channels = main.multichannel_store
        account = channels.register_channel_account(role, 'qq', account_id='persistent-original-qq', enabled=True)
        code = channels.create_pairing_code()
        assert channels.consume_pairing_code(code, role_id=role, channel='qq', scope='platform',
            platform_user_id='101', account_id=account.account_id, chat_id='101')
        channels_host = main.channel_host
        channels_host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=transport))
        application = None

        async def install():
            loop = asyncio.get_running_loop()
            state.update(hostLoop=loop, hostLoopId=id(loop), hostThread=threading.get_ident(), hostLoopDebug=loop.get_debug())
            previous = loop.get_exception_handler()
            state['exceptionHandler'] = previous

            def observed_error(owner_loop, context):
                state['loopErrors'].append({'message': str(context.get('message', '')),
                    'exceptionType': type(context['exception']).__name__ if context.get('exception') else None})
                if previous is None:
                    owner_loop.default_exception_handler(context)
                else:
                    previous(owner_loop, context)
            loop.set_exception_handler(observed_error)

            async def beat():
                while True:
                    state['heartbeat'] += 1
                    await asyncio.sleep(.02)
            state['heartbeatTask'] = asyncio.create_task(beat(), name='persistent-channel-host-heartbeat')

            async def ingress(_):
                raise AssertionError('this synthetic platform emits no inbound user events')
            state['transportStarts'] += 1
            await transport.start(ingress)
            await transport.wait_connected(timeout=3)
            reader = transport._task
            assert reader is not None and reader.get_loop() is loop
            reader_done = threading.Event()
            reader.add_done_callback(lambda completed: reader_done.set())
            state['reader'], state['readerDoneSignal'] = reader, reader_done
            state['originalSocketId'] = id(transport._ws)
            channels_host.bind_runtime_loop()
            return loop

        class Model:
            requests = 0
            loops = []
            threads = []

            async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
                self.requests += 1
                self.loops.append(asyncio.get_running_loop())
                self.threads.append(threading.get_ident())
                if self.requests == 1:
                    name, args = 'get_content_events', {}
                elif self.requests == 2:
                    item = json.loads(messages[-1].content[0].text)[0]['id']
                    name, args = 'message_push', {'message': 'persistent-original-8301', 'mood': '',
                        'thought': '原持久渠道loop', 'evidence': [item]}
                else:
                    assert self.requests == 3
                    name, args = 'finish_turn', {'decision': 'reply'}
                yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'persistent-{self.requests}', name, args),)))

        model = Model()
        monkeypatch.setattr(main, 'model_adapter', model)
        try:
            host_loop = invoke(install())
            monkeypatch.setattr(main, '_channel_runtime_loop', host_loop)
            server = source[1].save(source[2].model_copy(update={'roleIds': [role], 'timeoutSeconds': 10}))
            grant = grant_for(source).model_copy(update={'configRevision': server.revision, 'grantRevision': server.grant_id})
            configuration = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
                'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-only', 'apiKey': 'fixture'}).json()['configuration']
            path = f'/api/roles/{role}/autonomy'
            _, selected = save_external(client, path, account, {'activeDecisionEnabled': True,
                'decisionModelConfigurationId': configuration['id'], 'consentRevision': 1,
                'sourceGrants': [grant.model_dump(mode='json')]})
            application = main._proactive
            directory = [item for item in observations if item['action'] == 'get_friend_list']
            assert len(directory) == 1 and directory[0]['loop'] is host_loop
            before = state['heartbeat']
            now[0] += 1800

            async def run_original():
                assert await application.on_clock() == 1
                async with asyncio.timeout(20):
                    return await settled(application, role, clock=False)
            final = invoke(run_original())
            record('original-runtime-settled', hostHeartbeatBefore=before,
                hostHeartbeatAfter=state['heartbeat'], modelRequests=model.requests,
                modelLoopIds=[id(loop) for loop in model.loops], modelThreads=model.threads,
                delivery=final['deliveries'][0] if final['deliveries'] else None,
                platformCalls=[item['action'] for item in platform[0]['calls']], platformSends=len(platform[0]['sent']))

            assert model.requests == 3 and len({id(loop) for loop in model.loops}) == 1
            assert all(loop is not host_loop for loop in model.loops)
            assert all(thread != state['hostThread'] for thread in model.threads)
            assert state['heartbeat'] > before, 'the host loop must stay responsive while the TaskRuntime worker executes'
            assert len(platform[0]['sent']) == 1 and platform[0]['sent'][0]['user_id'] == 101
            receipt = final['deliveries'][0]
            assert receipt['status'] == 'sent' and receipt['platformMessageId'] == '8301' and receipt['messageId']
            assert receipt['target']['ref'] == selected['targetRef'] and receipt['attemptId']
            assert final['runs'][0]['runtimeAcknowledged'] and not final['cleanupPending']
            assert [json.loads(line)['name'] for line in (source[0] / 'calls.jsonl').read_text(encoding='utf-8').splitlines()] == ['get_events']
            attempts = channels.list_deliveries(role)
            assert len(attempts) == 1 and attempts[0]['attemptId'] == receipt['attemptId']
            assert attempts[0]['platformMessageId'] == '8301'
            threads = channels.list_threads(role)
            assert len(threads) == 1 and len(channels.list_messages(role, threads[0].thread_id)[1]) == 1
            assert sessions.list_messages(f'role:{role}') == []
            assert state['transportStarts'] == 1 and id(transport._ws) == state['originalSocketId']
            assert [item['action'] for item in observations] == ['get_friend_list', 'send_private_msg']
            assert all(item['task'].done() and item['doneSignal'].is_set() for item in observations)
            # The behavioral ownership contract is checked after real receipt
            # and Runtime cleanup, so this RED needs no unsafe debug exception
            # or deliberately stranded Future to establish the wrong loop.
            assert all(item['loop'] is host_loop and item['thread'] == state['hostThread'] for item in observations), (
                'persistent channel RPC escaped the originally started channel loop',
                [(item['action'], id(item['loop']), item['thread']) for item in observations])
            assert state['loopErrors'] == []
            record('persistent-channel-owned-loop-contract-satisfied')
        finally:
            async def cleanup():
                errors = []
                for close in (
                    application.close if application is not None else None,
                    host.close,
                    lambda: channels_host.pause_and_drain(account.account_id),
                    lambda: channels_host.stop(account.account_id),
                    transport.stop,
                    lambda: asyncio.to_thread(tasks.close),
                ):
                    if close is not None:
                        try:
                            await close()
                        except Exception as error:
                            errors.append(error)
                heartbeat = state.get('heartbeatTask')
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                reader = state.get('reader')
                if reader is not None:
                    assert reader.done() and state['readerDoneSignal'].is_set(), 'the original transport reader has not joined'
                if 'hostLoop' in state:
                    state['hostLoop'].set_exception_handler(state['exceptionHandler'])
                # Threading events and loop/task objects are kept in memory,
                # while public evidence stores their actual terminal booleans.
                state.pop('readerDoneSignal', None)
                record(state.get('stage', 'setup'), allOwnersClosed=not errors,
                    originalReaderDone=reader.done() if reader is not None else True,
                    heartbeatDone=heartbeat.done() if heartbeat is not None else True)
                if errors:
                    raise ExceptionGroup('persistent channel fixture cleanup', errors)
            invoke(cleanup())
