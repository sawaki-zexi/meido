import asyncio
import json
import threading

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
from backend.app.task_runtime import TaskOrigin
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_coding_api import Configuration
from test_coding_application import ControlledExecutor

pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.mark.parametrize('revoke', [False, True])
@pytest.mark.parametrize('executor', ['herdr', 'codex_exec'])
@pytest.mark.parametrize('owner_operations', [False, True])
def test_real_onebot_source_receives_one_background_completion_or_suppresses_after_unpair(api, tmp_path, monkeypatch, revoke, executor, owner_operations):
    from backend.app import main
    from backend.app.channels.host import ChannelHost
    from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
    from backend.app.channels.transports import NapCatWebSocketTransport
    from websockets.asyncio.server import serve
    client, roles, sessions, tasks = api
    class ChannelConfiguration(Configuration):
        def resolve(self, *args, **kwargs):
            grant, profile, build = super().resolve(*args, **kwargs)
            return grant, profile | {'executor': executor}, build
    configuration = ChannelConfiguration()
    configuration.role_id = ''
    release = threading.Event()
    class Work(ControlledExecutor):
        def run(self, execution):
            assert release.wait(10)
            return super().run(execution)
        def cancel(self): release.set()
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda role_id: roles.get(role_id).agentConfig.coding if roles.get(role_id) else None,
        authorized=main._coding_origin_authorized, config=configuration, executor_factory=Work,
        delegation_authorized=lambda origin: main._coding_origin_authorized(origin) and main._spawn_origin_authorized(origin),
        deliver=main._deliver_coding_completion, receipt=main._coding_receipt, lock_for=main._runtime_role_lock)
    monkeypatch.setattr(main, '_coding', application)
    role = client.post('/api/roles', json={'name': '原渠道编程', 'profile': {'profile': 'local fixture'},
        'agentConfig': {'enabledTools': ['tool_search'], 'spawn': {'enabled': executor == 'codex_exec'}, 'coding': {'enabled': True,
            'repoGrantIds': ['repo-a'], 'profileIds': ['profile-a'], 'defaultHerdrProfileId': 'profile-a',
            'defaultCodexProfileId': 'profile-a' if executor == 'codex_exec' else None}}}).json()['role']
    configuration.role_id = role['id']
    channels = main.multichannel_store
    account = channels.register_channel_account(role['id'], 'qq', account_id='coding-onebot', enabled=True)
    code = channels.create_pairing_code(role['id'], 'qq')
    assert channels.consume_pairing_code(code, role_id=role['id'], channel='qq', scope='platform',
        platform_user_id='123', account_id=account.account_id, chat_id='123')
    host = ChannelHost(channels)
    monkeypatch.setattr(main, 'channel_host', host)
    async def no_avatar(*args): return None
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            results = [item for item in messages if item['role'] == 'tool']
            if not results:
                yield ModelToolCall('coding-discovery', 'tool_search', {'query': 'select:' + ('spawn' if executor == 'codex_exec' else 'herdr_start_task')})
                yield ModelResponseDone("tool_use")
            elif len(results) == 1:
                yield ModelToolCall('coding-start', 'spawn' if executor == 'codex_exec' else 'herdr_start_task',
                    {'repository': 'repo-a', 'task': 'channel original work', 'profile': 'coding', 'run_in_background': True, 'retry_count': 0}
                    if executor == 'codex_exec' else {'repository': 'repo-a', 'requirement': 'channel original work'})
                yield ModelResponseDone("tool_use")
            else:
                assert json.loads(results[-1]['content'])['status'] == 'accepted'
                yield ModelTextDelta('已登记原渠道后台任务。')
    monkeypatch.setattr(main, 'model_adapter', Model())
    sends = []
    async def scenario():
        processed = asyncio.Event()
        inbound_sent = False
        async def peer(socket):
            nonlocal inbound_sent
            if not inbound_sent:
                inbound_sent = True
                await socket.send(json.dumps({'post_type': 'message', 'message_type': 'private', 'self_id': 999,
                    'user_id': 123, 'message_id': 174, 'raw_message': 'start coding',
                    'message': [{'type': 'text', 'data': {'text': 'start coding'}}], 'sender': {'nickname': 'owner'}}))
            async for raw in socket:
                request = json.loads(raw)
                if request['action'] == 'send_private_msg': sends.append(request['params'])
                await socket.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': request['echo'], 'data': {'message_id': 175}}))
        async def inbound(payload):
            try: await main._dispatch_channel_payload(account.account_id, payload, source_adapter=adapter)
            finally: processed.set()
        async with serve(peer, '127.0.0.1', 0) as server:
            transport = NapCatWebSocketTransport(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}', request_timeout=2)
            adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
            main.channel_adapters[account.account_id] = adapter
            host.register(adapter)
            try:
                await transport.start(inbound)
                await transport.wait_connected()
                await asyncio.wait_for(processed.wait(), 10)
                records = application.ledger.role_records(role['id'])
                assert len(records) == 1
                record = records[0]
                origin = TaskOrigin(**record['origin'])
                assert origin.source_authority_json and origin.source_account_id == account.account_id
                assert sessions.get_run(origin.parent_run_id).status == 'completed'
                if owner_operations:
                    # These are durable terminal outbox recovery fixtures;
                    # actual commit/PR creation is proven in publication tests.
                    from backend.app.coding_tasks.ledger import _json
                    for action in ('commit', 'publish'):
                        operation_id = 'codingop_original_' + action
                        operation = {'id': operation_id, 'roleId': role['id'], 'taskId': record['id'],
                            'attemptId': record['attemptId'], 'action': action, 'candidate': {'record': record},
                            'origin': record['origin'], 'status': 'unknown', 'runtimeStatus': 'interrupted',
                            'runtimeTaskId': None, 'runtimeAckDirty': False, 'cleanupState': 'complete',
                            'deliveryStatus': 'pending', 'messageSynced': False, 'resourceJobs': [], 'sealed': False}
                        with application.ledger.transaction() as db:
                            db.execute('INSERT INTO coding_owner_operations VALUES(?,?,?,?,?,?)',
                                (operation_id, role['id'], record['id'], operation_id, operation_id, _json(operation)))
                if revoke:
                    channels.unbind_identity(channels.list_identities()[0]['identityId'])
                release.set()
                for _ in range(200):
                    await application.tick()
                    current = application.ledger.attempt(record['attemptId'])
                    if current.get('messageSynced'): break
                    await asyncio.sleep(.01)
                assert current['runtimeAckDirty'] is False
                assert current['cleanupState'] == 'complete'
                messages = sessions.list_messages(origin.session_key)
                completed = [item for item in messages if item.metadata.get('internalEvent') == 'coding_completion']
                operation_messages = [item for item in messages if item.metadata.get('internalEvent') in {'coding_commit', 'coding_publication'}]
                if revoke:
                    assert current['deliveryStatus'] == 'suppressed' and not completed
                    assert not operation_messages
                    assert len(sends) == 1
                else:
                    assert current['deliveryStatus'] == 'sent' and len(completed) == 1
                    assert completed[0].runId is None and completed[0].metadata['deliveryStatus'] == 'sent'
                    assert len(sends) == (4 if owner_operations else 2)
                    assert all(item['user_id'] == 123 for item in sends)
                    assert channels.delivery_for_source(role['id'], source_kind='coding_completion', source_tool_call_id=record['attemptId'])[0] == 'sent'
                    if owner_operations:
                        assert len(operation_messages) == 2
                        assert all(item.runId is None and item.metadata['channel'] == 'qq' for item in operation_messages)
                        for action, kind in (('commit', 'coding_commit'), ('publish', 'coding_publication')):
                            assert channels.delivery_for_source(role['id'], source_kind=kind,
                                source_tool_call_id='codingop_original_' + action)[0] == 'sent'
                await application.tick()
                assert len(sends) == (1 if revoke else 4 if owner_operations else 2)
                assert not sessions.list_messages('role:' + role['id'])
            finally:
                await transport.stop()
                await channels.avatars.stop_account(account.account_id)
                await application.close()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        if not application._resources_closed: asyncio.run(application.close())
