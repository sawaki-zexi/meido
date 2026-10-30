import asyncio
import json

import pytest

from test_background_shell_api import api  # noqa: F401
from test_herdr_control import native_cli, cli_files  # noqa: F401


@pytest.mark.parametrize('revoke', [False, True])
def test_channel_report_uses_original_account_chat_once(api, cli_files, tmp_path, monkeypatch, revoke):  # noqa: F811
    import httpx
    from websockets.asyncio.server import serve
    from backend.app import main
    from backend.app.channels.host import ChannelHost
    from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
    from backend.app.channels.transports import NapCatWebSocketTransport
    from backend.app.plugin_configuration import PluginConfigurationStore
    from backend.app.model_adapter import ModelToolCall, ModelTextDelta, ModelResponseDone
    client, roles, sessions, tasks = api
    root, config, agent = cli_files
    # Keep the agent working until the initial real channel turn is delivered.
    (root / 'get.txt').write_text(json.dumps({'ok': True, 'result': {'agent': {**agent, 'agent_status': 'working'}}}))
    monkeypatch.setattr(main, 'data_root', tmp_path / 'data')
    monkeypatch.setattr(main, 'plugin_configuration', PluginConfigurationStore(tmp_path / 'plugins.json'))
    monkeypatch.setattr(main, '_herdr', None)
    monkeypatch.setattr(main, '_coding', None)
    role = client.post('/api/roles', json={'name': '原渠道接手者', 'profile': {'profile': 'local fixture'}}).json()['role']
    channels = main.multichannel_store
    account = channels.register_channel_account(role['id'], 'qq', account_id='herdr-fixture-account', enabled=True)
    code = channels.create_pairing_code(role['id'], 'qq')
    assert channels.consume_pairing_code(code, role_id=role['id'], channel='qq', scope='platform',
        platform_user_id='123', account_id=account.account_id, chat_id='123')
    host = ChannelHost(channels)
    monkeypatch.setattr(main, 'channel_host', host)
    async def no_avatar(*_):
        return None
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            if any(message['role'] == 'user' and 'Herdr agent 回报' in str(message.get('content')) for message in messages):
                yield ModelTextDelta('原渠道代理已完成')
                yield ModelResponseDone('stop')
                return
            results = [message for message in messages if message['role'] == 'tool']
            if not results:
                yield ModelToolCall('herdr-find', 'tool_search', {'query': 'select:herdr_prompt_agent'})
            elif len(results) == 1:
                yield ModelToolCall('herdr-prompt', 'herdr_prompt_agent', {'pane_id': 'pane-manual', 'text': 'channel input'})
            else:
                assert json.loads(results[-1]['content'])['command']['status'] == 'sent'
                yield ModelTextDelta('渠道已接手')
                yield ModelResponseDone('stop')
                return
            yield ModelResponseDone('tool_use')
    monkeypatch.setattr(main, 'model_adapter', Model())
    sends = []
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://local') as settings:
            assert (await settings.put('/api/plugin-management/herdr/configuration', json={
                'values': config.model_dump(), 'expectedRevision': 0})).status_code == 200
            assert (await settings.put('/api/plugin-management/herdr/enabled', json={
                'enabled': True, 'expectedRevision': 1})).status_code == 200
        service = main._herdr_control()
        service.watcher.poll_seconds = .02
        processed = asyncio.Event()
        async def peer(socket):
            await socket.send(json.dumps({'post_type': 'message', 'message_type': 'private', 'self_id': 999,
                'user_id': 123, 'message_id': 284, 'raw_message': '接手 agent',
                'message': [{'type': 'text', 'data': {'text': '接手 agent'}}], 'sender': {'nickname': 'owner'}}))
            async for raw in socket:
                request = json.loads(raw)
                if request['action'] == 'send_private_msg':
                    sends.append(request['params'])
                await socket.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': request['echo'], 'data': {'message_id': 285 + len(sends)}}))
        async def inbound(payload):
            try:
                await main._dispatch_channel_payload(account.account_id, payload, source_adapter=adapter)
            finally:
                processed.set()
        async with serve(peer, '127.0.0.1', 0) as server:
            transport = NapCatWebSocketTransport(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}', request_timeout=2)
            adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
            main.channel_adapters[account.account_id] = adapter
            host.register(adapter)
            try:
                await transport.start(inbound)
                await transport.wait_connected()
                await asyncio.wait_for(processed.wait(), 25)
                assert len(sends) == 1
                handover = service.watcher.handover('default', 'pane-manual')
                assert handover.target.origin.source_account_id == account.account_id
                if revoke:
                    channels.unbind_identity(channels.list_identities()[0]['identityId'])
                (root / 'get.txt').write_text(json.dumps({'ok': True, 'result': {'agent': agent}}))
                for _ in range(500):
                    records = service.log.records('default', 'pane-manual')
                    if records[-1]['report'] != 'pending':
                        break
                    await asyncio.sleep(.01)
                assert records[-1]['report'] == ('failed' if revoke else 'reported'), records
                assert len(sends) == (1 if revoke else 2)
                assert all(item['user_id'] == 123 for item in sends)
                assert not sessions.list_messages('role:' + role['id'])
                if not revoke:
                    receipt = channels.delivery_for_source(role['id'], source_kind='herdr_agent_report',
                        source_tool_call_id='herdr-agent:' + records[-1]['id'])
                    assert receipt[0] == 'sent'
                await asyncio.sleep(.08)
                assert len(sends) == (1 if revoke else 2)
            finally:
                await service.close()
                await transport.stop()
                main.channel_adapters.pop(account.account_id, None)
                if main._coding is not None:
                    await main._coding.close()
    asyncio.run(run())
