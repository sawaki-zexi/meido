import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def test_real_messages_tool_loop_delivers_one_image_and_next_model_gets_only_safe_reference(novelai_main, monkeypatch):
    from backend.app import main
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    from backend.app.models import ModelConfigurationInput
    client, service = novelai_main
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
                delta = {'tool_calls': [{'index': 0, 'id': 'novelai-main-call', 'type': 'function',
                    'function': {'name': 'generate_image', 'arguments': json.dumps(service.request)}}]}
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
                + '\n\ndata: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if not results else 'stop'}]})
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
        response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': '请画 blue sky'})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert 'assistant_completed' in response.text, response.text
    assert len(service.peer.requests) == 1 and len(provider_requests) == 2
    messages = client.get(f'/api/roles/{service.role.id}/session').json()['messages']
    final = messages[-1]
    assert final['content'] == '图片已生成并保存在本次回复。'
    assert len(final['metadata']['attachments']) == 1
    attachment = final['metadata']['attachments'][0]
    assert client.get(f"/api/roles/{service.role.id}/attachments/{attachment['id']}").status_code == 200
    history = client.get(f'/api/roles/{service.role.id}/generations').json()['generations']
    assert len(history) == 1 and history[0]['deliveryStatus'] == 'sent'
    assert history[0]['attachments'] == [attachment]
