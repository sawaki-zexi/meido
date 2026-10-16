"""Managed image work outlives its accepted request, never its source grant."""
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def test_background_http_returns_accepted_before_provider_finishes_and_recovers_once(novelai_service):
    service = novelai_service
    @asynccontextmanager
    async def lifespan(app):
        service.application.start()
        try:
            yield
        finally:
            service.peer.release.set()
            await service.application.close()
    app = FastAPI(lifespan=lifespan)
    app.include_router(service.application.router())
    service.peer.block = True
    with TestClient(app) as client:
        path = f'/api/roles/{service.role.id}/generations'
        request = {'requestId': '17217217217217217217217217217217',
            'expectedRevision': service.application.settings.public()['revision'],
            'parameters': {**service.request, 'background': True}}
        started = time.monotonic()
        response = client.post(path, json=request)
        assert response.status_code == 202, response.text
        assert time.monotonic() - started < 2
        accepted = response.json()['generation']
        assert accepted['taskId'] and accepted['deliveryMode'] == 'source_completion'
        assert service.peer.accepted.wait(2)
        assert service.sessions.list_messages(f'role:{service.role.id}') == []
        replay = client.post(path, json=request)
        assert replay.status_code == 202 and replay.json()['generation']['attemptId'] == accepted['attemptId']
        service.peer.release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = client.get(path + '/' + accepted['generationId'], params={'attemptId': accepted['attemptId']}).json()['generation']
            if result['deliveryStatus'] == 'sent':
                break
            time.sleep(.02)
        assert result['status'] == 'succeeded' and result['deliveryStatus'] == 'sent'
        assert len(service.peer.requests) == 1
        messages = service.sessions.list_messages(f'role:{service.role.id}')
        assert len(messages) == 1 and messages[0].messageType == 'internal_completion'
        assert messages[0].runId is None and messages[0].role == 'assistant'
        assert messages[0].metadata['attachments'] == result['attachments']


def test_real_messages_background_finishes_parent_while_http_generation_is_owned(novelai_main, monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from backend.app import main
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    from backend.app.models import ModelConfigurationInput
    client, service = novelai_main
    service.peer.block = True
    provider_requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            provider_requests.append(payload)
            results = [item for item in payload['messages'] if item['role'] == 'tool']
            if not results:
                delta = {'tool_calls': [{'index': 0, 'id': 'background-172', 'type': 'function', 'function': {
                    'name': 'generate_image', 'arguments': json.dumps({**service.request, 'background': True})}}]}
            else:
                result = json.loads(results[-1]['content'])
                assert result['status'] == 'accepted' and result['taskId'] and not result['attachments']
                delta = {'content': '已受理，稍后在本会话交付。'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': None}]})
                + '\n\ndata: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop' if results else 'tool_calls'}]})
                + '\n\ndata: [DONE]\n\n').encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    configuration = main.model_configuration_store.create(ModelConfigurationInput(providerId='custom', provider='fixture',
        baseUrl=f'http://127.0.0.1:{server.server_port}/v1', model='fixture-chat', apiKey='fixture-key'))
    service.roles.set_model_configuration(service.role.id, configuration.id)
    registry = ModelAdapterRegistry(timeout=5)
    monkeypatch.setattr(main, 'model_adapter_registry', registry)
    monkeypatch.setattr(main, 'model_adapter', RoutedModelAdapter(registry))
    service.application.lock_for = main._runtime_role_lock
    try:
        with client:
            response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': '请后台画图'})
            assert 'assistant_completed' in response.text, response.text
            assert service.peer.accepted.wait(2) and not service.peer.release.is_set()
            rows = service.sessions.list_messages(f'role:{service.role.id}')
            final = rows[-1]
            assert final.content == '已受理，稍后在本会话交付。'
            assert not final.metadata.get('attachments')
            assert service.sessions.get_run(final.runId).status == 'completed'
            history = service.application.history(service.role.id)['generations']
            assert len(history) == 1 and history[0]['taskId']
            service.peer.release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = service.sessions.list_messages(f'role:{service.role.id}')
                if rows[-1].messageType == 'internal_completion':
                    break
                time.sleep(.02)
            assert rows[-1].messageType == 'internal_completion' and rows[-1].runId is None
            assert rows[-1].metadata['generationAttemptIds'] == [history[0]['attemptId']]
            assert len([row for row in rows if row.role == 'user']) == 1
            assert len(service.peer.requests) == 1 and len(provider_requests) == 2
    finally:
        service.peer.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_received_foreground_image_uses_one_original_completion_when_final_model_reply_fails(novelai_main, monkeypatch):
    from backend.app import main
    from backend.app.model_adapter import ModelToolCall
    client, service = novelai_main
    class FailedFinalModel:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(item['role'] == 'tool' for item in messages):
                yield ModelToolCall('foreground-final-fails', 'generate_image', service.request)
            else:
                raise RuntimeError('controlled final reply failure after paid image received')
    monkeypatch.setattr(main, 'model_adapter', FailedFinalModel())
    service.application.lock_for = main._runtime_role_lock
    with client:
        response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': '请画图'})
        assert 'assistant_failed' in response.text or 'run_failed' in response.text or 'error' in response.text
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            rows = service.sessions.list_messages(f'role:{service.role.id}')
            if rows[-1].messageType == 'internal_completion':
                break
            time.sleep(.02)
        assert rows[-1].messageType == 'internal_completion'
        assert rows[-1].runId is None and rows[-1].metadata['attachments']
        assert len([row for row in rows if row.role == 'user']) == 1
        history = service.application.history(service.role.id)['generations']
        assert len(history) == 1 and history[0]['deliveryMode'] == 'source_completion'
        assert history[0]['deliveryStatus'] == 'sent'
        assert len(service.peer.requests) == 1
