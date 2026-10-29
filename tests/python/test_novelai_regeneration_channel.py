"""An owner UI action cannot grant a channel access to desktop-private images."""
import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


@pytest.mark.parametrize('base_source', ['desktop_private', 'original_input', 'deleted_original_input', 'role_private', 'role_snapshot'])
def test_channel_regeneration_uses_only_currently_available_original_source_images(novelai_main, monkeypatch, base_source):
    from backend.app import main
    from backend.app.channels.host import ChannelHost
    from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
    from backend.app.channels.transports import NapCatWebSocketTransport
    from backend.app.chat_attachments import resolve_attachment
    from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
    from websockets.asyncio.server import serve

    _, service = novelai_main
    channels = main.multichannel_store
    account = channels.register_channel_account(service.role.id, 'qq', account_id='spec172-account', enabled=True, allow_group=True)
    code = channels.create_pairing_code(service.role.id, 'qq')
    assert channels.consume_pairing_code(code, role_id=service.role.id, channel='qq', scope='platform',
        platform_user_id='123', account_id=account.account_id, chat_id='123')
    host = ChannelHost(channels, attachment_resolver=lambda role_id, identifier:
        resolve_attachment(service.roles.workspace_path(role_id), identifier))
    monkeypatch.setattr(main, 'channel_host', host)
    service.application.send_channel = host.account_send
    service.application.lock_for = main._runtime_role_lock
    async def no_avatar(*args): return None
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    original_arguments = dict(service.request)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('spec172-original', 'generate_image', original_arguments)
                yield ModelResponseDone("tool_use")
            else:
                assert json.loads(messages[-1]['content'])['status'] == 'succeeded'
                yield ModelTextDelta('group image')
    monkeypatch.setattr(main, 'model_adapter', Model())
    sends = []
    original_input = io.BytesIO()
    Image.new('RGB', (64, 64), '#aabbcc').save(original_input, format='PNG')
    if base_source == 'role_snapshot':
        service.roles.set_avatar(service.role.id, original_input.getvalue(), 'image/png')
        original_arguments.update(mode='img2img', base_role_image_id='avatar')
    channel_upload = base_source in {'original_input', 'deleted_original_input'}

    async def scenario():
        parent_done = asyncio.Event()
        async def inbound(payload):
            await main._dispatch_channel_payload(account.account_id, payload, source_adapter=adapter)
            parent_done.set()
        async def peer(socket):
            await socket.send(json.dumps({'post_type': 'message', 'message_type': 'group', 'self_id': 999,
                'user_id': 123, 'group_id': 456, 'message_id': 71, 'raw_message': 'draw',
                'message': [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': 'draw'}},
                    *([{'type': 'image', 'data': {'url': 'https://fixture.qpic.cn/source.png', 'file': 'source.png'}}]
                        if channel_upload else [])],
                'sender': {'nickname': 'owner'}}))
            async for raw in socket:
                request = json.loads(raw)
                if request['action'] == 'send_group_msg': sends.append(request['params'])
                await socket.send(json.dumps({'status': 'ok', 'retcode': 0, 'echo': request['echo'], 'data': {'message_id': 72 + len(sends)}}))
        async with serve(peer, '127.0.0.1', 0) as server:
            transport = NapCatWebSocketTransport(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}', request_timeout=1)
            adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
            if channel_upload:
                from backend.app.channels.models import DownloadedMedia
                async def download_source_media(event, **kwargs):
                    assert len(event.attachments) == 1
                    return (DownloadedMedia('image', original_input.getvalue(), 'source.png', 'image/png'),)
                monkeypatch.setattr(adapter, 'download_media', download_source_media)
            main.channel_adapters[account.account_id] = adapter
            host.register(adapter)
            try:
                await transport.start(inbound)
                await transport.wait_connected()
                await asyncio.wait_for(parent_done.wait(), 5)
                assert len(service.peer.requests) == 1 and len(sends) == 1
                original = service.application.history(service.role.id)['generations'][0]
                assert original['deliveryStatus'] == 'sent'
                thread = channels.list_threads(service.role.id)[0]
                original_messages = service.sessions.list_messages(thread.session_key)
                assert len([m for m in original_messages if m.role == 'user']) == 1
                user = next(m for m in original_messages if m.role == 'user')
                if not channel_upload:
                    assert not user.metadata.get('attachments')
                image = io.BytesIO()
                Image.new('RGB', (64, 64), '#cc1234').save(image, format='PNG')
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://local.test') as client:
                    upload = await client.post(f'/api/roles/{service.role.id}/attachments', content=image.getvalue(),
                        headers={'Content-Type': 'image/png', 'x-attachment-filename': 'desktop-private.png'})
                    assert upload.status_code == 200, upload.text
                    private_identifier = upload.json()['id']
                    assert all(private_identifier not in str(m.metadata) for m in original_messages)
                    identifier = user.metadata['attachments'][0]['id'] if channel_upload else private_identifier
                    if base_source == 'role_snapshot':
                        identifier = original['parameters']['base_attachment_id']
                        service.roles.set_avatar(service.role.id, image.getvalue(), 'image/png')
                    if base_source == 'deleted_original_input':
                        from pathlib import Path
                        Path(resolve_attachment(service.roles.workspace_path(service.role.id), identifier)['path']).unlink()
                    endpoint = f'/api/roles/{service.role.id}/generations/{original["generationId"]}/regenerate'
                    body = {'requestId': 'ffffffffffffffffffffffffffffff12',
                        'expectedRevision': service.application.settings.public()['revision'],
                        'sourceAttemptId': original['attemptId'], 'expectedAttachmentId': original['attachments'][0]['id'],
                        'parameters': {**service.request, 'seed': 172, 'mode': 'img2img',
                            'base_attachment_id': identifier, 'strength': .7, 'noise': .2}}
                    if base_source == 'role_private':
                        role = service.roles.add_illustration(service.role.id, image.getvalue(), 'image/png')
                        body['parameters'].pop('base_attachment_id')
                        body['parameters']['base_role_image_id'] = role.illustrations[-1].id
                    response = await client.post(endpoint, json=body)
                    history = await client.get(f'/api/roles/{service.role.id}/generations')
                    assert history.status_code == 200, history.text
                    if base_source not in {'original_input', 'role_snapshot'}:
                        assert response.status_code == 403, response.text
                        assert len(service.peer.requests) == 1 and len(sends) == 1
                        assert len(history.json()['generations']) == 1
                        assert history.json()['usage']['manualAttempts'] == 1
                        return
                    assert response.status_code == 200, response.text
                    assert len(service.peer.requests) == 2 and len(sends) == 2
                    assert [send['group_id'] for send in sends] == [456, 456]
                    assert base64.b64decode(service.peer.requests[-1]['body']['parameters']['image']) == original_input.getvalue()
                    assert history.json()['usage']['manualAttempts'] == 2
                    replay = await client.post(endpoint, json=body)
                    assert replay.status_code == 200, replay.text
                    assert replay.json()['generation']['attemptId'] == response.json()['generation']['attemptId']
                    stale = await client.post(endpoint, json={**body, 'requestId': 'ffffffffffffffffffffffffffffff13'})
                    assert stale.status_code == 409, stale.text
                    assert len(service.peer.requests) == 2 and len(sends) == 2
                    replacement = response.json()['generation']
                    chained = await client.post(endpoint, json={**body, 'requestId': 'ffffffffffffffffffffffffffffff14',
                        'sourceAttemptId': replacement['attemptId'], 'expectedAttachmentId': replacement['attachments'][0]['id'],
                        'parameters': {**body['parameters'], 'seed': 173}})
                    assert chained.status_code == 200, chained.text
                    assert len(service.peer.requests) == 3 and len(sends) == 3
                    assert base64.b64decode(service.peer.requests[-1]['body']['parameters']['image']) == original_input.getvalue()
            finally:
                service.peer.release.set()
                await service.application.close()
                await transport.stop()
                await channels.avatars.stop_account(account.account_id)
    asyncio.run(scenario())
