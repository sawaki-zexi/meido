import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


@pytest.mark.parametrize('mode', ['txt2img', 'img2img', 'self_portrait', 'role_img2img', 'card_img2img', 'illustration_img2img'])
def test_real_messages_tool_loop_delivers_one_image_and_next_model_gets_only_safe_reference(novelai_main, monkeypatch, mode):
    from backend.app import main
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    from backend.app.models import ModelConfigurationInput
    client, service = novelai_main
    updated = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(),
        'agentConfig': {'novelai': {'manualEnabled': False, 'consentRevision': 0}}})
    assert updated.status_code == 200, updated.text
    assert client.get('/api/services/novelai').json()['manualConsentRevision'] == 0
    arguments = dict(service.request)
    attachment_ids = []
    base_image = None
    portrait = None
    asset_id = 'avatar'
    role_input = mode in {'role_img2img', 'card_img2img', 'illustration_img2img'}
    if mode == 'self_portrait' or role_input:
        import io
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (64, 64), '#aabbcc').save(image, format='PNG')
        portrait = image.getvalue()
        if mode == 'illustration_img2img':
            role = service.roles.add_illustration(service.role.id, portrait, 'image/png', '日常立绘')
            asset_id = role.illustrations[-1].id
        else:
            service.roles.set_avatar(service.role.id, portrait, 'image/png', card=mode == 'card_img2img')
            asset_id = 'card' if mode == 'card_img2img' else 'avatar'
        arguments['prompt'] = '1girl, silver hair, blue eyes, maid dress, smile, portrait'
        if role_input:
            arguments.update(mode='img2img', base_role_image_id=asset_id, strength=.45, noise=.1)
    if mode == 'img2img':
        import io
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (64, 64), '#17aa11').save(image, format='PNG')
        base_image = image.getvalue()
        uploaded = client.post(f'/api/roles/{service.role.id}/attachments', content=base_image,
            headers={'Content-Type': 'image/png', 'x-attachment-filename': 'current-input.png'})
        assert uploaded.status_code == 200, uploaded.text
        attachment_ids = [uploaded.json()['id']]
        arguments.update(mode='img2img', base_attachment_id=attachment_ids[0], strength=.7, noise=.2)
    provider_requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            messages, tools = payload['messages'], payload.get('tools', [])
            provider_requests.append((messages, tools))
            results = [item for item in messages if item['role'] == 'tool']
            if not results:
                assert 'generate_image' in {item['function']['name'] for item in tools}
                call_name = 'inspect_role_image' if portrait else 'generate_image'
                call_args = {'asset_id': asset_id} if portrait else arguments
                if portrait:
                    assert call_name in {item['function']['name'] for item in tools}
                delta = {'tool_calls': [{'index': 0, 'id': 'novelai-main-call', 'type': 'function',
                    'function': {'name': call_name, 'arguments': json.dumps(call_args)}}]}
            elif portrait and len(results) == 1:
                import base64
                images = [block['image_url']['url'] for message in messages if isinstance(message.get('content'), list)
                    for block in message['content'] if block.get('type') == 'image_url']
                assert images == ['data:image/png;base64,' + base64.b64encode(portrait).decode()]
                delta = {'tool_calls': [{'index': 0, 'id': 'novelai-portrait-call', 'type': 'function',
                    'function': {'name': 'generate_image', 'arguments': json.dumps(arguments)}}]}
            else:
                result = json.loads(results[-1]['content'])
                assert result['status'] == 'succeeded' and result['attachments']
                assert 'fixture-secret-171' not in str(messages)
                assert 'base64' not in results[-1]['content'] and '"path"' not in results[-1]['content']
                delta = {'content': '图片已生成并保存在本次回复。'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': None}]})
                + '\n\ndata: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if 'tool_calls' in delta else 'stop'}]})
                + '\n\ndata: [DONE]\n\n').encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    configuration = main.model_configuration_store.create(ModelConfigurationInput(providerId='custom', provider='fixture',
        baseUrl=f'http://127.0.0.1:{server.server_port}/v1', model='fixture-chat', apiKey='fixture-provider-key'))
    service.roles.set_model_configuration(service.role.id, configuration.id)
    registry = ModelAdapterRegistry(timeout=5)
    monkeypatch.setattr(main, 'model_adapter_registry', registry)
    monkeypatch.setattr(main, 'model_adapter', RoutedModelAdapter(registry))
    catalog = client.get(f'/api/roles/{service.role.id}/tools').json()['tools']
    assert next(item for item in catalog if item['name'] == 'generate_image')['modelVisible']
    try:
        response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': '请画 blue sky', 'attachments': attachment_ids})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert 'assistant_completed' in response.text, response.text
    assert len(service.peer.requests) == 1 and len(provider_requests) == (3 if portrait else 2)
    dispatched = service.peer.requests[0]['body']
    if portrait:
        assert dispatched['input'] == arguments['prompt']
        assert dispatched['parameters']['v4_prompt']['caption']['base_caption'] == arguments['prompt']
    assert service.peer.requests[0]['authorization'] == 'Bearer fixture-secret-171'
    if mode == 'img2img':
        import base64
        assert dispatched['action'] == 'img2img'
        assert base64.b64decode(dispatched['parameters']['image']) == base_image
        for messages, _ in provider_requests:
            images = [block['image_url']['url'] for message in messages if isinstance(message.get('content'), list)
                for block in message['content'] if block.get('type') == 'image_url']
            assert images == ['data:image/png;base64,' + base64.b64encode(base_image).decode()]
    elif role_input:
        import base64
        assert dispatched['action'] == 'img2img'
        assert base64.b64decode(dispatched['parameters']['image']) == portrait
        assert dispatched['parameters']['strength'] == .45
    else:
        assert dispatched['action'] == 'generate' and 'image' not in dispatched['parameters']
    messages = client.get(f'/api/roles/{service.role.id}/session').json()['messages']
    final = messages[-1]
    assert final['content'] == '图片已生成并保存在本次回复。'
    assert len(final['metadata']['attachments']) == 1
    attachment = final['metadata']['attachments'][0]
    assert client.get(f"/api/roles/{service.role.id}/attachments/{attachment['id']}").status_code == 200
    history = client.get(f'/api/roles/{service.role.id}/generations').json()['generations']
    assert len(history) == 1 and history[0]['deliveryStatus'] == 'sent'
    assert history[0]['attachments'] == [attachment]
