import asyncio
import base64
import json
import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


@pytest.mark.parametrize('receipt_status', ['sent', 'uncertain', 'receipt_write_failure', 'message_write_failure'])
def test_real_onebot_socket_owner_turn_sends_generated_png_once_to_original_source(novelai_main, monkeypatch, receipt_status):
    from backend.app import main
    from backend.app.channels.host import ChannelHost
    from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
    from backend.app.channels.transports import NapCatWebSocketTransport
    from backend.app.chat_attachments import resolve_attachment
    from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
    from websockets.asyncio.server import serve
    _, service = novelai_main
    expected_status = 'uncertain' if receipt_status == 'uncertain' else 'sent'
    fault = None
    if receipt_status == 'receipt_write_failure':
        fault = 'fail_delivery_receipt'
        with service.application.ledger.connect() as connection:
            connection.execute("CREATE TRIGGER fail_delivery_receipt BEFORE UPDATE OF state ON novelai_media_outbox WHEN NEW.state='sent' BEGIN SELECT RAISE(ABORT,'controlled receipt sync failure'); END")
    elif receipt_status == 'message_write_failure':
        fault = 'fail_delivery_message'
        with service.application.ledger.connect() as connection:
            connection.execute("CREATE TRIGGER fail_delivery_message BEFORE UPDATE OF metadata_json ON messages WHEN NEW.metadata_json LIKE '%deliveryStatus%sent%' BEGIN SELECT RAISE(ABORT,'controlled message sync failure'); END")
    channels = main.multichannel_store
    account = channels.register_channel_account(service.role.id, 'qq', account_id='novelai-qq', enabled=True)
    code = channels.create_pairing_code(service.role.id, 'qq')
    assert channels.consume_pairing_code(code, role_id=service.role.id, channel='qq', scope='platform',
        platform_user_id='123', account_id=account.account_id, chat_id='123')
    host = ChannelHost(channels, attachment_resolver=lambda rid, identifier: resolve_attachment(service.roles.workspace_path(rid), identifier))
    monkeypatch.setattr(main, 'channel_host', host)
    async def no_avatar(*args):
        return None
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('channel-image', 'generate_image', service.request)
                yield ModelResponseDone("tool_use")
            else:
                assert json.loads(messages[-1]['content'])['status'] == 'succeeded'
                yield ModelTextDelta('原渠道图片已生成。')
    monkeypatch.setattr(main, 'model_adapter', Model())
    sends = []

    async def scenario():
        completed = asyncio.Event()
        processed = asyncio.Event()
        async def inbound(payload):
            try:
                await main._dispatch_channel_payload(account.account_id, payload, source_adapter=adapter)
            finally:
                processed.set()
        inbound_sent = False
        async def peer(socket):
            nonlocal inbound_sent
            if not inbound_sent:
                inbound_sent = True
                await socket.send(json.dumps({'post_type': 'message', 'message_type': 'private', 'self_id': 999,
                    'user_id': 123, 'message_id': 171, 'raw_message': 'draw blue sky',
                    'message': [{'type': 'text', 'data': {'text': 'draw blue sky'}}], 'sender': {'nickname': 'owner'}}))
            async for raw in socket:
                request = json.loads(raw)
                if request['action'] == 'send_private_msg':
                    sends.append(request['params'])
                    if receipt_status == 'uncertain':
                        completed.set()
                        await socket.close()
                        return
                await socket.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': request['echo'], 'data': {'message_id': 172}}))
                if request['action'] == 'send_private_msg':
                    completed.set()
        async with serve(peer, '127.0.0.1', 0) as server:
            transport = NapCatWebSocketTransport(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}', request_timeout=2)
            adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
            main.channel_adapters[account.account_id] = adapter
            host.register(adapter)
            try:
                await transport.start(inbound)
                await transport.wait_connected()
                await asyncio.wait_for(completed.wait(), 10)
                await asyncio.wait_for(processed.wait(), 10)
                for _ in range(200):
                    rows = service.sessions.list_messages(channels.list_threads(service.role.id)[0].session_key)
                    if rows[-1].metadata.get('deliveryStatus') == expected_status or fault and service.application.history(service.role.id)['generations']:
                        break
                    await asyncio.sleep(.01)
                assert len(service.peer.requests) == 1 and len(sends) == 1
                assert sends[0]['user_id'] == 123
                images = [part for part in sends[0]['message'] if isinstance(part, dict) and part.get('type') == 'image']
                assert len(images) == 1
                assert base64.b64decode(images[0]['data']['file'].removeprefix('base64://')).startswith(b'\x89PNG\r\n\x1a\n')
                history = service.application.history(service.role.id)['generations']
                if fault:
                    with service.application.ledger.connect() as connection:
                        connection.execute('DROP TRIGGER ' + fault)
                    await service.application.recover()
                    history = service.application.history(service.role.id)['generations']
                    rows = service.sessions.list_messages(channels.list_threads(service.role.id)[0].session_key)
                assert len(history) == 1 and history[0]['deliveryStatus'] == expected_status
                assert rows[-1].metadata.get('deliveryStatus') == expected_status
                assert len(rows[-1].metadata['attachments']) == 1
                assert not service.sessions.list_messages(f'role:{service.role.id}')
                await service.application.recover()
                assert len(service.peer.requests) == 1 and len(sends) == 1
            finally:
                await transport.stop()
                await channels.avatars.stop_account(account.account_id)
    asyncio.run(scenario())
