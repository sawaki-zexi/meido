"""Public owner HTTP + actual provider HTTP + owned local CLI fixture, no paid services."""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.codex_exec import CodexExecutor
from backend.app.coding_tasks.config import CodingConfigStore
from backend.app.coding_tasks.git_repository import git
from test_codex_executor import fixture_process
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_public_skill_qualification_background_resume_cancel_and_ack_use_no_chat_child(api, tmp_path, monkeypatch):
    from backend.app import main
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    from backend.app.models import ModelConfigurationInput
    client, roles, sessions, tasks = api
    # This owned synthetic root is at least as deep as a normal Windows
    # AppData/Meido/data/coding root, without touching actual user data.
    owned_root = tmp_path / 'normal-windows-appdata-depth' / 'coding'
    # The injected host platform and process seams deliberately run the same
    # public protocol fixture on CI; no request can enable native platforms.
    application = CodingApplication(owned_root, tasks, sessions,
        config=CodingConfigStore(tmp_path / 'synthetic-config.db', native_supported=lambda: True),
        role_config=lambda role_id: roles.get(role_id).agentConfig.coding if roles.get(role_id) else None,
        authorized=main._coding_origin_authorized,
        delegation_authorized=lambda origin: main._coding_origin_authorized(origin) and main._spawn_origin_authorized(origin),
        lock_for=main._runtime_role_lock)
    application.executor_factory = lambda record, ledger: CodexExecutor(application.root, record, ledger,
        authorized=lambda: application._execution_allowed(record), process_factory=fixture_process)
    application.probes.process_factory = fixture_process
    monkeypatch.setattr(main, '_coding', application)
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base')
    auth = tmp_path / 'explicit-auth'
    auth.mkdir()
    (auth / 'auth.json').write_text('{"fixture":"not-a-real-account"}')
    requests, receipts = [], []
    action, arguments = ['spawn'], [{}]
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(payload)
            system = str([item for item in payload['messages'] if item['role'] == 'system'])
            assert '受控 coding profile' in system, 'explicit Skill body did not reach the provider'
            assert 'independent delegated task' not in system and 'internal spawn completion' not in system
            messages = payload['messages']
            user_index = max(i for i, item in enumerate(messages) if item['role'] == 'user')
            results = [item for item in messages[user_index + 1:] if item['role'] == 'tool']
            names = {item['function']['name'] for item in payload.get('tools', [])}
            if not results:
                assert action[0] in names
                call = ('owner-action', action[0], arguments[0])
            else:
                content = results[-1]['content']
                receipts.append(json.loads(content) if content.startswith(('{', '[')) else content)
                call = None
            delta = {'tool_calls': [{'index': 0, 'id': call[0], 'type': 'function', 'function': {
                'name': call[1], 'arguments': json.dumps(call[2], ensure_ascii=False)}}]} if call else {'content': '父回合已完成'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': None}]}) +
                '\n\ndata: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if call else 'stop'}]}) +
                '\n\ndata: [DONE]\n\n').encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        role = client.post('/api/roles', json={'name': 'Codex fixture', 'profile': {'profile': '测试'}}).json()['role']
        rid = role['id']
        exe = Path(sys.executable).resolve()
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()
        response = client.post('/api/coding/profiles', json={'name': 'Synthetic local exec', 'kind': 'codex', 'executor': 'codex_exec',
            'executable': str(exe), 'sha256': digest, 'version': '0.159.3', 'model': 'synthetic-only',
            'authDirectory': str(auth), 'allowedRoleIds': [rid]})
        assert response.status_code == 200, response.text
        profile = response.json()
        assert profile['verification']['status'] == 'unverified'
        approval = {'revision': profile['revision'], 'roleId': rid, 'buildSha256': digest,
                    'approveNativeTurns': 2, 'requestId': 'explicit-synthetic-no-fee-probe'}
        response = client.post('/api/coding/profiles/' + profile['id'] + '/probe', json=approval)
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            probe = client.get('/api/coding/probes').json()['probes'][0]
            if probe.get('runtimeResult'): break
            time.sleep(.02)
        assert probe['status'] == 'verified' and probe['cleanupState'] == 'complete', probe
        assert application.config.native_build() is None
        first_repo = tmp_path / 'revoked-first-repo'
        first_repo.mkdir()
        git(first_repo, 'init', '-b', 'main')
        git(first_repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base')
        revoked = client.post('/api/coding/repositories', json={'root': str(first_repo), 'allowedRoleIds': [rid]}).json()
        grant = client.post('/api/coding/repositories', json={'root': str(repo), 'allowedRoleIds': [rid],
            'validation': [{'id': 'real-check', 'argv': [sys.executable, '-I', '-c',
                'from pathlib import Path;assert Path("fixture.txt").read_text(encoding="utf-8")'], 'cwd': '.'}]}).json()
        assert client.put('/api/coding/configuration', json={'enabled': True}).status_code == 200
        response = client.put('/api/roles/' + rid, json={'name': role['name'], 'profile': role['profile'], 'agentConfig': {
            'spawn': {'enabled': True}, 'enabledTools': [], 'coding': {'enabled': True, 'repoGrantIds': [revoked['id'], grant['id']],
                'profileIds': [profile['id']], 'defaultCodexProfileId': profile['id']}}})
        assert response.status_code == 200, response.text
        response = client.post('/api/coding/repositories', json={
            key: ([] if key == 'allowedRoleIds' else revoked[key])
            for key in ('id', 'root', 'baseRef', 'remote', 'validation', 'allowedRoleIds')})
        assert response.status_code == 200, response.text
        model = main.model_configuration_store.create(ModelConfigurationInput(providerId='custom', provider='fixture',
            baseUrl='http://127.0.0.1:' + str(server.server_address[1]) + '/v1', model='local-only', apiKey='fixture'))
        roles.set_model_configuration(rid, model.id)
        monkeypatch.setattr(main, 'model_adapter', RoutedModelAdapter(ModelAdapterRegistry()))
        def turn(name, values):
            action[0], arguments[0] = name, values
            response = client.post('/api/roles/' + rid + '/messages', json={'content': name, 'skillIds': ['codex-delegate']})
            assert 'assistant_completed' in response.text, response.text
            return receipts[-1]
        denied = turn('spawn', {'task': '已撤销仓库不可执行', 'profile': 'coding', 'repository': revoked['id'],
                                'run_in_background': True, 'retry_count': 0})
        assert denied == 'Task unavailable in this role and original source, or delegation is disabled.'
        assert client.get('/api/coding/roles/' + rid + '/tasks').json()['tasks'] == []
        assert not (first_repo / 'fixture.txt').exists()
        first = turn('spawn', {'task': '公开完整需求🙂', 'profile': 'coding', 'repository': grant['id'],
                               'run_in_background': True, 'retry_count': 0})
        assert first['status'] == 'accepted' and first['executor'] == 'codex_exec', first
        def completed():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = client.get('/api/coding/roles/' + rid + '/tasks').json()['tasks'][0]
                if current['deliveryStatus'] == 'sent': return current
                time.sleep(.02)
            raise AssertionError(current)
        done = completed()
        assert done['status'] == 'validated_diff' and not done['runtimeAckDirty'], done
        assert turn('spawn_manage', {'action': 'list'})[0]['job_id'] == first['job_id']
        assert turn('spawn_manage', {'action': 'get', 'job_id': first['job_id']})['nativeSession'] == done['nativeSession']
        second = turn('spawn_manage', {'action': 'continue', 'job_id': first['job_id'], 'task': '精确继续🙂'})
        assert second['job_id'] == first['job_id'] and second['attemptId'] != first['attemptId']
        resumed = completed()
        assert resumed['status'] == 'validated_diff' and resumed['nativeSession'] == done['nativeSession'], resumed
        holding = turn('spawn_manage', {'action': 'continue', 'job_id': first['job_id'], 'task': 'hold until cancellation'})
        deadline = time.monotonic() + 5
        while not application.ledger.attempt(holding['attemptId']).get('dispatch'):
            assert time.monotonic() < deadline
            time.sleep(.01)
        cancelled = turn('spawn_manage', {'action': 'cancel', 'job_id': first['job_id']})
        assert cancelled['cleanupState'] == 'complete' and cancelled['status'] == 'cancelled', cancelled
        assert completed()['runtimeAckDirty'] is False
        assert len(requests) == 14, 'seven owner turns only; no child or completion model request'
        assert not (repo / 'fixture.txt').exists()
        messages = sessions.list_messages('role:' + rid)
        assert len([item for item in messages if item.metadata.get('internalEvent') == 'coding_completion']) == 3
        assert not tasks.pending_completions()
    finally:
        asyncio.run(application.close())
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()
