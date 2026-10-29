"""Real rich-model HTTP and NovelAI peers, each owned until natural shutdown."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def hold_candidate_writer(service, monkeypatch):
    """Let the real optional completion meet a second SQLite writer."""
    import sqlite3
    from contextlib import closing
    original = service.application.scenes.complete
    observed = []
    def complete_under_write_lock(*args):
        with closing(sqlite3.connect(service.application.ledger.database_path)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            observed.append('held_real_sqlite_writer')
            try:
                return original(*args)
            finally:
                blocker.rollback()
    monkeypatch.setattr(service.application.scenes, 'complete', complete_under_write_lock)
    return observed


@pytest.fixture
def scene_main(novelai_main, monkeypatch, request):
    from backend.app import main
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    client, service = novelai_main
    peer = SimpleNamespace(requests=[], observation={'transition': 'started', 'scene_key': 'garden',
        'visual_key': 'garden-day', 'visual_description': 'A sunny garden', 'prompt': 'blue sky, garden',
        'negative_prompt': 'rain', 'size_preset': 'square'}, aux_accepted=threading.Event(),
        aux_release=threading.Event(), aux_second=threading.Event(), aux_block=False, manual=False,
        aux_text=None, aux_status=200, aux_tool=False, prompt_text=None, prompt_block=False,
        prompt_accepted=threading.Event(), finish_reason='stop')
    class Provider(BaseHTTPRequestHandler):
        # A cancelled HTTP client can connect without sending a request. Keep
        # the original non-daemon reader join finite during fixture shutdown.
        timeout = 1.0
        def log_message(self, *args): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            peer.requests.append(request)
            if request['model'] == 'fixture-scene':
                converting = 'visual_description' in json.loads(request['messages'][1]['content'])
                (peer.prompt_accepted if converting else peer.aux_accepted).set()
                if len([item for item in peer.requests if item['model'] == 'fixture-scene'
                        and 'visual_description' not in json.loads(item['messages'][1]['content'])]) == 2:
                    peer.aux_second.set()
                if peer.aux_block or converting and peer.prompt_block:
                    peer.aux_release.wait(8)
                prompt_fields = {'prompt', 'negative_prompt', 'size_preset'}
                content = json.dumps({key: value for key, value in peer.observation.items()
                    if (key in prompt_fields) == converting})
                if peer.aux_text is not None:
                    content = peer.aux_text
                if converting and peer.prompt_text is not None:
                    content = peer.prompt_text
                if peer.aux_status != 200:
                    self.send_response(peer.aux_status)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"controlled auxiliary failure"}}')
                    return
            else:
                content = 'We enter the sunny garden.'
            manual = peer.manual and request['model'] == 'fixture-chat' and not any(item['role'] == 'tool' for item in request['messages'])
            delta = {'tool_calls': [{'index': 0, 'id': 'manual-scene-173', 'type': 'function', 'function': {
                'name': 'generate_image', 'arguments': json.dumps(service.request)}}]} if manual else {'content': content}
            if peer.aux_tool and request['model'] == 'fixture-scene':
                delta = {'tool_calls': [{'index': 0, 'id': 'forged-aux-tool', 'type': 'function', 'function': {
                    'name': 'generate_image', 'arguments': json.dumps(service.request)}}]}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            try:
                self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': None}]})
                    + '\n\ndata: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if manual else peer.finish_reason if request['model'] == 'fixture-scene' else 'stop'}]})
                    + '\n\ndata: [DONE]\n\n').encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    registry = ModelAdapterRegistry(timeout=5)
    monkeypatch.setattr(main, 'model_adapter_registry', registry)
    monkeypatch.setattr(main, 'model_adapter', RoutedModelAdapter(registry))
    service.application.lock_for = main._runtime_role_lock
    try:
        connections = []
        for model in ('fixture-chat', 'fixture-scene'):
            response = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'fixture',
                'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'model': model, 'api': 'openai-completions'})
            assert response.status_code == 200, response.text
            connections.append(response.json()['configuration']['id'])
        response = client.put(f'/api/roles/{service.role.id}/model-configuration', json={'configurationId': connections[0]})
        assert response.status_code == 200, response.text
        public = service.application.settings.public()
        service.application.settings.save({'enabled': True}, expected_revision=public['revision'])
        response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
            'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'manualEnabled': False,
                'sceneCgEnabled': True, 'scenePromptModelConfigurationId': connections[1],
                'consentRevision': service.application.settings.public()['revision']}}})
        assert response.status_code == 200, response.text
        if getattr(request, 'param', None) == 'no_lifespan':
            yield client, service, peer, connections
        else:
            with client:
                yield client, service, peer, connections
    finally:
        peer.aux_release.set()
        service.peer.release.set()
        server.shutdown()
        server.server_close()
        thread.join()
