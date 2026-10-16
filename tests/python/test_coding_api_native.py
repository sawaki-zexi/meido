"""Real public HTTP + provider HTTP + owned Herdr; no real CLI/account/model."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.executor import HerdrExecutor
from backend.app.coding_tasks.git_repository import git_environment
from backend.app.coding_tasks.herdr import HerdrClient
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_coding_herdr_native import native_herdr  # noqa: F401

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_owner_config_qualifies_then_chat_discovery_runs_actual_owned_native_pipeline(api, tmp_path, monkeypatch, native_herdr):
    from backend.app import main
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    from backend.app.models import ModelConfigurationInput
    fixture = os.getenv('MEIDO_TEST_CODING_CLI')
    if not fixture: pytest.skip('explicit compiled no-network CLI fixture required')
    executable = Path(fixture).parent / 'codex.exe'
    client, roles, sessions, tasks = api
    requirement = '公开HTTP原生需求🙂'
    repo = tmp_path / 'r'
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True, env=git_environment())
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base'], check=True, capture_output=True, env=git_environment())
    auth = tmp_path / 'explicit-auth'
    auth.mkdir()
    (auth / 'auth.json').write_text('{"fixture":"not-a-real-account"}')
    def client_factory(*args, **kwargs):
        value = HerdrClient(*args, **kwargs)
        value.env.update({'MEIDO_CODING_FIXTURE_PYTHON': sys.executable,
                          'MEIDO_CODING_FIXTURE_SCRIPT': str(Path(__file__).parent / 'coding_fixtures/interactive_cli.py')})
        return value
    application = CodingApplication(tmp_path / 'c', tasks, sessions,
        role_config=lambda role_id: roles.get(role_id).agentConfig.coding if roles.get(role_id) else None,
        authorized=main._coding_origin_authorized, lock_for=main._runtime_role_lock)
    application.executor_factory = lambda record, ledger: HerdrExecutor(application.root, record, ledger,
        authorized=lambda: application._allowed(record), client_factory=client_factory)
    application.probes.client_factory = client_factory
    monkeypatch.setattr(main, '_coding', application)
    requests, receipts = [], []
    action, arguments = ['herdr_start_task'], [{}]
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(payload)
            messages = payload['messages']
            last_user = max(i for i, item in enumerate(messages) if item['role'] == 'user')
            results = [item for item in messages[last_user+1:] if item['role'] == 'tool']
            names = {item['function']['name'] for item in payload.get('tools', [])}
            if not results:
                assert 'tool_search' in names
                call = ('native-discovery', 'tool_search', {'query': 'select:' + action[0]})
            elif len(results) == 1:
                assert action[0] in names
                call = ('native-action', action[0], arguments[0])
            else:
                receipts.append(json.loads(results[-1]['content']))
                call = None
            delta = {'tool_calls': [{'index': 0, 'id': call[0], 'type': 'function', 'function': {'name': call[1], 'arguments': json.dumps(call[2], ensure_ascii=False)}}]} if call else {'content': '后台任务已接收'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': None}]}) + '\n\ndata: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if call else 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    try:
        role = client.post('/api/roles', json={'name': '原生HTTP夹具', 'profile': {'profile': '测试'}}).json()['role']
        role_id = role['id']
        build_sha = hashlib.sha256(native_herdr.read_bytes()).hexdigest()
        assert client.put('/api/coding/native-build', json={'executable': str(native_herdr), 'sha256': build_sha}).status_code == 200
        profile_response = client.post('/api/coding/profiles', json={'name': 'No-account synthetic Codex', 'kind': 'codex',
            'executable': str(executable), 'sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
            'version': '0.159.3', 'model': 'no-model-synthetic-fixture', 'authDirectory': str(auth), 'allowedRoleIds': [role_id]})
        assert profile_response.status_code == 200, profile_response.text
        profile = profile_response.json()
        assert profile['verification']['status'] == 'unverified'
        approval = {'revision': profile['revision'], 'roleId': role_id, 'buildSha256': build_sha,
                    'approveNativeTurns': 0, 'requestId': 'explicit-no-fee-native-fixture'}
        assert client.post('/api/coding/profiles/' + profile['id'] + '/probe', json=approval).status_code == 422
        approval['approveNativeTurns'] = 2
        response = client.post('/api/coding/profiles/' + profile['id'] + '/probe', json=approval)
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            probe = client.get('/api/coding/probes').json()['probes'][0]
            if probe['status'] not in {'accepted', 'running'} and probe.get('runtimeResult'): break
            time.sleep(.05)
        assert probe['status'] == 'verified' and probe['cleanupState'] == 'complete', probe
        validation = {'id': 'fixture', 'argv': [sys.executable, '-c', 'from pathlib import Path; assert Path("fixture.txt").read_text(encoding="utf-8") == ' + repr(requirement)], 'cwd': '.'}
        grant = client.post('/api/coding/repositories', json={'root': str(repo), 'allowedRoleIds': [role_id], 'validation': [validation]}).json()
        assert client.put('/api/coding/configuration', json={'enabled': True}).status_code == 200
        updated = client.put('/api/roles/' + role_id, json={'name': role['name'], 'profile': role['profile'],
            'agentConfig': {'enabledTools': ['tool_search'], 'coding': {
            'enabled': True, 'repoGrantIds': [grant['id']], 'profileIds': [profile['id']], 'defaultHerdrProfileId': profile['id']}}})
        assert updated.status_code == 200, updated.text
        model = main.model_configuration_store.create(ModelConfigurationInput(providerId='custom', provider='fixture',
            baseUrl='http://127.0.0.1:' + str(server.server_address[1]) + '/v1', model='localhost-controlled-provider', apiKey='fixture'))
        roles.set_model_configuration(role_id, model.id)
        monkeypatch.setattr(main, 'model_adapter', RoutedModelAdapter(ModelAdapterRegistry()))
        def turn(name, values):
            action[0], arguments[0] = name, values
            response = client.post('/api/roles/' + role_id + '/messages', json={'content': 'fixture ' + name})
            assert 'assistant_completed' in response.text, response.text
            return receipts[-1]
        first = turn('herdr_start_task', {'repository': grant['id'], 'requirement': requirement})
        assert first['status'] == 'accepted', first
        def completed():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = client.get('/api/coding/roles/' + role_id + '/tasks').json()['tasks'][0]
                if current['deliveryStatus'] == 'sent': return current
                time.sleep(.05)
            raise AssertionError(current)
        done = completed()
        assert done['status'] == 'validated_diff' and done['cleanupState'] == 'complete', done
        assert turn('herdr_list_tasks', {})['tasks'][0]['id'] == first['id']
        assert turn('herdr_get_task', {'task_id': first['id']})['nativeSession'] == done['nativeSession']
        continued = turn('herdr_continue_task', {'task_id': first['id'], 'requirement': requirement})
        assert continued['id'] == first['id'] and continued['attemptId'] != first['attemptId']
        resumed = completed()
        assert resumed['status'] == 'validated_diff' and resumed['nativeSession'] == done['nativeSession'], resumed
        holding = turn('herdr_continue_task', {'task_id': first['id'], 'requirement': 'hold until cancellation'})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if application.ledger.attempt(holding['attemptId']).get('dispatch'): break
            time.sleep(.05)
        cancelled = turn('herdr_cancel_task', {'task_id': first['id']})
        assert cancelled['cleanupState'] == 'complete' and cancelled['status'] == 'cancelled', cancelled
        final = completed()
        assert final['runtimeAckDirty'] is False
        assert not (repo / 'fixture.txt').exists()
        assert len(requests) == 18  # 6 owner turns, discovery/action/final each; no summary model.
        messages = sessions.list_messages('role:' + role_id)
        assert len([item for item in messages if item.metadata.get('internalEvent') == 'coding_completion']) == 3
        assert len([item for item in messages if item.role == 'user']) == 6
    finally:
        import asyncio
        asyncio.run(application.close())
        server.shutdown(); server.server_close(); server_thread.join(timeout=3)
        assert not server_thread.is_alive()
