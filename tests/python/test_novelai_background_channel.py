"""Original-source media is sent by the real OneBot adapter with real receipts."""
import asyncio
import base64
import json

import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


@pytest.mark.parametrize('receipt_case', ['sent', 'lost', 'receipt_sqlite', 'message_sqlite', 'sending_restart', 'unsupported_media', 'source_rebound'])
def test_background_owner_channel_returns_text_then_one_original_media_receipt(novelai_main, monkeypatch, receipt_case):
    from backend.app import main
    from backend.app.channels.host import ChannelHost
    from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
    from backend.app.channels.transports import NapCatWebSocketTransport
    from backend.app.chat_attachments import resolve_attachment
    from backend.app.model_adapter import ModelTextDelta, ModelToolCall
    from websockets.asyncio.server import serve
    _, service = novelai_main
    lose_media_receipt = receipt_case == 'lost'
    fault = None
    if receipt_case == 'receipt_sqlite':
        fault = 'fail_background_receipt'
        with service.application.ledger.connect() as connection:
            connection.execute("CREATE TRIGGER fail_background_receipt BEFORE UPDATE OF state ON novelai_media_outbox WHEN NEW.state='sent' BEGIN SELECT RAISE(ABORT,'controlled background receipt failure'); END")
    elif receipt_case == 'message_sqlite':
        fault = 'fail_background_message'
        with service.application.ledger.connect() as connection:
            connection.execute("CREATE TRIGGER fail_background_message BEFORE UPDATE OF metadata_json ON messages WHEN NEW.metadata_json LIKE '%deliveryStatus%sent%' BEGIN SELECT RAISE(ABORT,'controlled background message failure'); END")
    service.peer.block = True
    channels = main.multichannel_store
    account = channels.register_channel_account(service.role.id, 'qq', account_id='background-172', enabled=True, allow_group=True)
    code = channels.create_pairing_code(service.role.id, 'qq')
    assert channels.consume_pairing_code(code, role_id=service.role.id, channel='qq', scope='platform',
        platform_user_id='123', account_id=account.account_id, chat_id='123')
    host = ChannelHost(channels, attachment_resolver=lambda role_id, identifier:
        resolve_attachment(service.roles.workspace_path(role_id), identifier))
    monkeypatch.setattr(main, 'channel_host', host)
    service.application.send_channel = host.account_send
    service.application.lock_for = main._runtime_role_lock
    async def no_avatar(*args):
        return None
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('channel-background', 'generate_image', {**service.request, 'background': True})
            else:
                result = json.loads(messages[-1]['content']) if messages[-1]['content'].startswith('{') else None
                if receipt_case == 'unsupported_media':
                    assert result is None or result['status'] == 'rejected'
                    yield ModelTextDelta('当前渠道不支持图片交付。')
                else:
                    assert result['status'] == 'accepted'
                    yield ModelTextDelta('已受理后台生成。')
    monkeypatch.setattr(main, 'model_adapter', Model())
    sends = []

    async def scenario():
        parent_done, media_done = asyncio.Event(), asyncio.Event()
        async def inbound(payload):
            await main._dispatch_channel_payload(account.account_id, payload, source_adapter=adapter)
            parent_done.set()
        async def peer(socket):
            await socket.send(json.dumps({'post_type': 'message', 'message_type': 'group', 'self_id': 999,
                'user_id': 123, 'group_id': 456, 'message_id': 171, 'raw_message': 'draw in background',
                'message': [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': 'draw in background'}}], 'sender': {'nickname': 'owner'}}))
            async for raw in socket:
                request = json.loads(raw)
                media = False
                if request['action'] == 'send_group_msg':
                    sends.append(request['params'])
                    media = isinstance(request['params']['message'], list) and any(part.get('type') == 'image' for part in request['params']['message'])
                    if media and lose_media_receipt:
                        media_done.set()
                        await socket.close()
                        return
                await socket.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': request['echo'], 'data': {'message_id': 172}}))
                if media:
                    media_done.set()
        async with serve(peer, '127.0.0.1', 0) as server:
            transport = NapCatWebSocketTransport(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}', request_timeout=1)
            adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
            if receipt_case == 'unsupported_media':
                from dataclasses import replace
                monkeypatch.setattr(adapter, 'capabilities', replace(adapter.capabilities, media=False))
            main.channel_adapters[account.account_id] = adapter
            host.register(adapter)
            if receipt_case != 'sending_restart':
                service.application.start()
            try:
                await transport.start(inbound)
                await transport.wait_connected()
                await asyncio.wait_for(parent_done.wait(), 5)
                assert len(sends) == 1 and not any(part.get('type') == 'image' for part in sends[0]['message'])
                thread = channels.list_threads(service.role.id)[0]
                before = service.sessions.list_messages(thread.session_key)
                assert before[-1].messageType == 'text' and not before[-1].metadata.get('attachments')
                if receipt_case == 'unsupported_media':
                    assert not service.peer.requests
                    assert not service.application.history(service.role.id)['generations']
                    assert len(sends) == 1
                    return
                if receipt_case == 'source_rebound':
                    from backend.app.novelai_generation.context import GenerationContext
                    assert await asyncio.to_thread(service.peer.accepted.wait, 2)
                    saved = service.application.history(service.role.id)['generations'][0]
                    source = GenerationContext.from_dict(json.loads(service.application.ledger.attempt(saved['attemptId'])['origin']))
                    identity = channels.list_identities()[0]
                    channels.unbind_identity(identity['identityId'])
                    code = channels.create_pairing_code(service.role.id, 'qq')
                    assert channels.consume_pairing_code(code, role_id=service.role.id, channel='qq', scope='platform',
                        platform_user_id='123', account_id=account.account_id, chat_id='123')
                    task = await asyncio.to_thread(service.tasks.query, saved['taskId'], source.origin, wait_seconds=2)
                    assert task.ended_at is not None and task.status == 'cancelled'
                    service.peer.release.set()
                    await service.application.recover()
                    assert len(service.peer.requests) == 1 and len(sends) == 1
                    assert not any(row.metadata.get('attachments') for row in service.sessions.list_messages(thread.session_key))
                    assert not service.sessions.list_messages(f'role:{service.role.id}')
                    return
                service.peer.release.set()
                if receipt_case == 'sending_restart':
                    from backend.app.novelai_generation.context import GenerationContext
                    for _ in range(400):
                        saved = service.application.history(service.role.id)['generations'][0]
                        if saved['status'] == 'succeeded':
                            break
                        await asyncio.sleep(.01)
                    row = service.application.ledger.attempt(saved['attemptId'])
                    source = GenerationContext.from_dict(json.loads(row['origin']))
                    reply = service.application._claim_channel_completion(source, saved['attemptId'])
                    assert reply is not None and len(sends) == 1
                else:
                    await asyncio.wait_for(media_done.wait(), 5)
                if fault or receipt_case == 'sending_restart':
                    from backend.app.novelai_generation.application import NovelaiApplication
                    old = service.application
                    if fault:
                        for _ in range(300):
                            rows = old.history(service.role.id)['generations']
                            journal = old.root / service.role.id / (rows[0]['attemptId'] + '.receipt.json')
                            if journal.is_file():
                                break
                            await asyncio.sleep(.01)
                        assert journal.is_file()
                    await old.close()
                    if fault:
                        with old.ledger.connect() as connection:
                            connection.execute('DROP TRIGGER ' + fault)
                    service.application = NovelaiApplication(root=old.root, tasks=service.tasks,
                        sessions=service.sessions, roles=service.roles, channels=channels,
                        credentials=old.settings.credentials, http=old.http, send_channel=host.account_send,
                        lock_for=main._runtime_role_lock, channel_media_supported=old.channel_media_supported)
                    monkeypatch.setattr(main, '_novelai', service.application)
                    await service.application.recover()
                    if receipt_case == 'sending_restart':
                        result = service.application.history(service.role.id)['generations'][0]
                        assert result['deliveryStatus'] == 'uncertain'
                        rows = service.sessions.list_messages(thread.session_key)
                        assert rows[-1].metadata['deliveryStatus'] == 'uncertain'
                        await service.application.recover()
                        assert len(sends) == 1 and len(service.peer.requests) == 1
                        return
                expected = 'uncertain' if lose_media_receipt else 'sent'
                for _ in range(300):
                    history = service.application.history(service.role.id)['generations']
                    if history[0]['deliveryStatus'] == expected:
                        break
                    await asyncio.sleep(.01)
                assert history[0]['status'] == 'succeeded' and history[0]['deliveryStatus'] == expected
                assert len(service.peer.requests) == 1 and len(sends) == 2
                image = next(part for part in sends[1]['message'] if part.get('type') == 'image')
                assert sends[1]['group_id'] == 456
                assert base64.b64decode(image['data']['file'].removeprefix('base64://')).startswith(b'\x89PNG\r\n\x1a\n')
                assert any(part.get('type') == 'reply' and str(part['data']['id']) == '171' for part in sends[1]['message'])
                assert any(part.get('type') == 'at' and str(part['data']['qq']) == '123' for part in sends[1]['message'])
                rows = service.sessions.list_messages(thread.session_key)
                assert rows[-1].messageType == 'internal_completion' and rows[-1].runId is None
                assert rows[-1].metadata['deliveryStatus'] == expected
                assert rows[-1].metadata['generationAttemptIds'] == [history[0]['attemptId']]
                assert not service.sessions.list_messages(f'role:{service.role.id}')
                await service.application.recover()
                assert len(sends) == 2 and len(service.peer.requests) == 1
                if receipt_case == 'sent':
                    import httpx
                    original = history[0]
                    selected = original['attachments'][0]
                    message = rows[-1]
                    other = {'id': 'keep-channel-file', 'name': 'notes.txt', 'mediaType': 'text/plain', 'size': 10, 'kind': 'file'}
                    service.sessions.update_message(message.id, message.content, message.status,
                        metadata={**message.metadata, 'attachments': [selected, other], 'custom': {'keep': 172}})
                    service.peer.block = False
                    body = {'requestId': '37237237237237237237237237237237',
                        'expectedRevision': service.application.settings.public()['revision'],
                        'sourceAttemptId': original['attemptId'], 'expectedAttachmentId': selected['id'],
                        'parameters': {key: value for key, value in service.request.items() if key != 'seed'}}
                    endpoint = f'/api/roles/{service.role.id}/generations/{original["generationId"]}/regenerate'
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://local.test') as client:
                        response = await client.post(endpoint, json=body)
                        assert response.status_code == 200, response.text
                        regenerated = response.json()['generation']
                        assert regenerated['status'] == 'succeeded' and regenerated['deliveryStatus'] == 'sent'
                        assert regenerated['parentAttemptId'] == original['attemptId']
                        replay = await client.post(endpoint, json=body)
                        assert replay.status_code == 200 and replay.json()['generation']['attemptId'] == regenerated['attemptId']
                    assert len(sends) == 3 and len(service.peer.requests) == 2
                    current = service.sessions.list_messages(thread.session_key)
                    assert len(current) == len(rows) and current[-1].id == message.id
                    assert current[-1].metadata['attachments'] == [regenerated['attachments'][0], other]
                    assert current[-1].metadata['custom'] == {'keep': 172}
                    assert sends[2]['group_id'] == 456
                    assert any(part.get('type') == 'reply' and str(part['data']['id']) == '171' for part in sends[2]['message'])
                    assert service.application.owner_generation(service.role.id, original['generationId'],
                        attempt_id=original['attemptId'])['deliveryStatus'] == 'sent'
            finally:
                service.peer.release.set()
                await service.application.close()
                await transport.stop()
                await channels.avatars.stop_account(account.account_id)
    asyncio.run(scenario())
