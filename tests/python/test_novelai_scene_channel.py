"""Real owner OneBot turns use one auxiliary HTTP call and original media receipt."""
import asyncio
import json
import time

import pytest

from novelai_scene_fixtures import scene_main, hold_candidate_writer
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


@pytest.mark.parametrize('scene_main', ['no_lifespan'], indirect=True)
@pytest.mark.parametrize('case', ['sent', 'lost_receipt', 'source_rebound', 'unsupported', 'late_receipt', 'candidate_busy'])
def test_cg_uses_only_original_owner_group_and_actual_media_receipt(scene_main, monkeypatch, case):
    from backend.app import main
    from backend.app.channels.host import ChannelHost
    from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
    from backend.app.channels.transports import NapCatWebSocketTransport
    from backend.app.chat_attachments import resolve_attachment
    from websockets.asyncio.server import serve
    _, service, model, _ = scene_main
    candidate_locks = hold_candidate_writer(service, monkeypatch) if case == 'candidate_busy' else []
    model.aux_block = case == 'source_rebound'
    channels = main.multichannel_store
    account = channels.register_channel_account(service.role.id, 'qq', account_id='scene-channel-173', enabled=True, allow_group=True)
    code = channels.create_pairing_code(service.role.id, 'qq')
    assert channels.consume_pairing_code(code, role_id=service.role.id, channel='qq', scope='platform',
        platform_user_id='123', account_id=account.account_id, chat_id='123')
    host = ChannelHost(channels, attachment_resolver=lambda role_id, identifier:
        resolve_attachment(service.roles.workspace_path(role_id), identifier))
    monkeypatch.setattr(main, 'channel_host', host)
    service.application.send_channel = host.account_send
    async def no_avatar(*args): return None
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    sends = []
    sockets = []
    if case == 'late_receipt':
        with service.application.ledger.connect() as db:
            db.execute("CREATE TRIGGER fail_cg_receipt BEFORE UPDATE OF state ON novelai_media_outbox WHEN NEW.state='sent' BEGIN SELECT RAISE(ABORT,'controlled CG receipt failure'); END")
    async def scenario():
        parent_done = asyncio.Event()
        publications = []
        async def observe_messages():
            async for event in channels.subscribe(service.role.id):
                publications.append(event)
        async def inbound(payload):
            await main._dispatch_channel_payload(account.account_id, payload, source_adapter=adapter)
            parent_done.set()
        async def peer(socket):
            sockets.append(socket)
            await socket.send(json.dumps({'post_type': 'message', 'message_type': 'group', 'self_id': 999,
                'user_id': 123, 'group_id': 456, 'message_id': 173, 'raw_message': 'Enter the garden.',
                'message': [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': 'Enter the garden.'}}],
                'sender': {'nickname': 'owner'}}))
            async for raw in socket:
                command = json.loads(raw)
                media = False
                if command['action'] == 'send_group_msg':
                    sends.append(command['params'])
                    media = any(part.get('type') == 'image' for part in command['params']['message'])
                if media and case == 'lost_receipt':
                    await socket.close()
                    return
                await socket.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': command['echo'], 'data': {'message_id': 174 + len(sends)}}))
        async with serve(peer, '127.0.0.1', 0) as server:
            transport = NapCatWebSocketTransport(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}', request_timeout=1)
            adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
            if case == 'unsupported':
                from dataclasses import replace
                monkeypatch.setattr(adapter, 'capabilities', replace(adapter.capabilities, media=False))
            main.channel_adapters[account.account_id] = adapter
            host.register(adapter)
            service.application.start()
            observer = asyncio.create_task(observe_messages())
            try:
                await asyncio.sleep(0)
                await transport.start(inbound)
                await transport.wait_connected()
                await asyncio.wait_for(parent_done.wait(), 5)
                if case == 'candidate_busy':
                    assert candidate_locks == ['held_real_sqlite_writer']
                    assert len(sends) == 1 and not service.peer.requests
                    assert not [item for item in model.requests if item['model'] == 'fixture-scene']
                    assert any(event.get('thread') and event.get('message', {}).get('role') == 'assistant'
                        and event['message']['status'] == 'completed'
                        and event['message']['metadata'].get('deliveryStatus') == 'sent'
                        for event in publications), publications
                    return
                if case == 'source_rebound':
                    assert await asyncio.to_thread(model.aux_accepted.wait, 2)
                    channels.unbind_identity(channels.list_identities()[0]['identityId'])
                    code = channels.create_pairing_code(service.role.id, 'qq')
                    assert channels.consume_pairing_code(code, role_id=service.role.id, channel='qq', scope='platform',
                        platform_user_id='123', account_id=account.account_id, chat_id='123')
                    model.aux_release.set()
                deadline = time.monotonic() + (1 if case == 'unsupported' else 5)
                while time.monotonic() < deadline:
                    page = service.application.history(service.role.id)
                    if page['generations'] and (page['generations'][0]['deliveryStatus'] in {'sent', 'uncertain'}
                            or case == 'late_receipt' and len(sends) == 2
                            or case == 'source_rebound' and page['generations'][0]['status'] not in {'prepared', 'dispatching'}):
                        break
                    await asyncio.sleep(.02)
                media_sends = [send for send in sends if any(part.get('type') == 'image' for part in send['message'])]
                if case == 'late_receipt':
                    first_attempt = page['generations'][0]['attemptId']
                    model.observation.update(transition='none', scene_key='', visual_key='', visual_description='', prompt='', negative_prompt='')
                    parent_done.clear()
                    await sockets[0].send(json.dumps({'post_type': 'message', 'message_type': 'group', 'self_id': 999,
                        'user_id': 123, 'group_id': 456, 'message_id': 175, 'raw_message': 'Another real owner turn.',
                        'message': [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': 'Another real owner turn.'}}],
                        'sender': {'nickname': 'owner'}}))
                    await asyncio.wait_for(parent_done.wait(), 5)
                    with service.application.ledger.connect() as db:
                        db.execute('DROP TRIGGER fail_cg_receipt')
                    for _ in range(3):
                        await service.application.recover()
                    thread = channels.list_threads(service.role.id)[0]
                    completion = next(row for row in service.sessions.list_messages(thread.session_key)
                        if first_attempt in row.metadata.get('generationAttemptIds', []))
                    assert completion.metadata['deliveryStatus'] == 'sent', completion.metadata
                    assert len(service.peer.requests) == 1
                    assert len([send for send in sends if any(part.get('type') == 'image' for part in send['message'])]) == 1
                    return
                if case in {'source_rebound', 'unsupported'}:
                    assert len(sends) == 1 and not media_sends and not service.peer.requests
                    assert len([item for item in model.requests if item['model'] == 'fixture-scene']) == (1 if case == 'source_rebound' else 0)
                    return
                assert len(sends) == 2 and len(media_sends) == 1 and len(service.peer.requests) == 1
                assert all(send['group_id'] == 456 for send in sends)
                assert page['generations'][0]['deliveryStatus'] == ('uncertain' if case == 'lost_receipt' else 'sent')
                thread = channels.list_threads(service.role.id)[0]
                rows = service.sessions.list_messages(thread.session_key)
                assert len([row for row in rows if row.role == 'user']) == 1
                assert len([row for row in rows if row.messageType == 'internal_completion']) == 1
                assert not service.sessions.list_messages(f'role:{service.role.id}')
                for _ in range(3):
                    await service.application.recover()
                assert len(sends) == 2 and len(service.peer.requests) == 1
            finally:
                model.aux_release.set()
                service.peer.release.set()
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
                await service.application.close()
                await transport.stop()
                await channels.avatars.stop_account(account.account_id)
    asyncio.run(scenario())
