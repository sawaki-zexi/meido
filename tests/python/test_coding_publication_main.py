"""Real owner routes/Skill/Git/owned CLI and localhost publication, no accounts."""
import asyncio
import hashlib
from pathlib import Path
import sys
import time

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.codex_exec import CodexExecutor
from backend.app.coding_tasks.config import CodingConfigStore
from backend.app.coding_tasks.git_repository import git
from backend.app.coding_tasks.github_publication import GithubPlatform, GitTransport
from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
from test_background_shell_api import MemoryCredentials, api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_codex_executor import fixture_process
from test_coding_publication import platform

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_actual_owner_skill_to_reviewed_commit_publish_and_exact_continue(api, tmp_path, monkeypatch):
    from backend.app import main
    client, roles, sessions, tasks = api
    application = CodingApplication(tmp_path / 'normal-appdata-depth/coding', tasks, sessions,
        config=CodingConfigStore(tmp_path / 'coding-config.db', native_supported=lambda: True),
        role_config=lambda role_id: roles.get(role_id).agentConfig.coding if roles.get(role_id) else None,
        authorized=main._coding_origin_authorized,
        delegation_authorized=lambda origin: main._coding_origin_authorized(origin) and main._spawn_origin_authorized(origin),
        lock_for=main._runtime_role_lock, publication_credentials=MemoryCredentials())
    application.executor_factory = lambda record, ledger: CodexExecutor(application.root, record, ledger,
        authorized=lambda: application._execution_allowed(record), process_factory=fixture_process)
    application.probes.process_factory = fixture_process
    monkeypatch.setattr(main, '_coding', application)
    repository, bare, auth = tmp_path / 'repo', tmp_path / 'remote.git', tmp_path / 'dedicated-auth'
    for directory in (repository, bare, auth): directory.mkdir()
    git(repository, 'init', '-b', 'main')
    git(repository, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base')
    git(bare, 'init', '--bare', '-b', 'main')
    git(repository, 'push', str(bare), 'HEAD:refs/heads/main')
    git(repository, 'remote', 'add', 'origin', 'https://github.com/meido-fixture/publication.git')
    (auth / 'auth.json').write_text('{"fixture":"no-real-authentication"}', encoding='utf-8')
    class Model:
        action = 'spawn'
        arguments = {}
        calls = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            user_index = max(i for i, item in enumerate(messages) if item['role'] == 'user')
            results = [item for item in messages[user_index + 1:] if item['role'] == 'tool']
            if not results:
                assert self.action in {item['function']['name'] for item in tools}
                yield ModelToolCall('explicit-action-' + str(self.calls), self.action, self.arguments)
                yield ModelResponseDone("tool_use")
            else:
                yield ModelTextDelta('原来源后台任务已登记。')
    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    def wait_for(function, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = function()
            if value: return value
            time.sleep(.02)
        raise AssertionError('owned public operation did not settle')
    try:
        role = client.post('/api/roles', json={'name': '批准成果的角色', 'profile': {'profile': 'local-only'}}).json()['role']
        role_id = role['id']
        executable = Path(sys.executable).resolve()
        sha = hashlib.sha256(executable.read_bytes()).hexdigest()
        saved = client.post('/api/coding/profiles', json={'name': 'Synthetic exec only', 'kind': 'codex',
            'executor': 'codex_exec', 'executable': str(executable), 'sha256': sha, 'version': '0.159.3',
            'model': 'synthetic-no-fee', 'authDirectory': str(auth), 'allowedRoleIds': [role_id]})
        assert saved.status_code == 200, saved.text
        profile = saved.json()
        response = client.post('/api/coding/profiles/' + profile['id'] + '/probe', json={'revision': profile['revision'],
            'roleId': role_id, 'buildSha256': sha, 'approveNativeTurns': 2, 'requestId': 'explicit-local-fixture-two-turns'})
        assert response.status_code == 200, response.text
        probe = wait_for(lambda: next((value for value in client.get('/api/coding/probes').json()['probes'] if value.get('runtimeResult')), None))
        assert probe['status'] == 'verified', probe
        saved = client.post('/api/coding/repositories', json={'root': str(repository), 'allowedRoleIds': [role_id],
            'localCommit': True, 'push': True, 'draftPr': True,
            'validation': [{'id': 'real-check', 'argv': [sys.executable, '-I', '-c',
                'from pathlib import Path; assert Path("fixture.txt").read_text(encoding="utf-8") == "work"'], 'cwd': '.'}]})
        assert saved.status_code == 200, saved.text
        grant = saved.json()
        assert client.put('/api/coding/configuration', json={'enabled': True}).status_code == 200
        response = client.put('/api/roles/' + role_id, json={'name': role['name'], 'profile': role['profile'], 'agentConfig': {'spawn': {'enabled': True},
            'coding': {'enabled': True, 'repoGrantIds': [grant['id']], 'profileIds': [profile['id']], 'defaultCodexProfileId': profile['id']}}})
        assert response.status_code == 200, response.text
        model.arguments = {'profile': 'coding', 'repository': grant['id'], 'task': 'work', 'run_in_background': True, 'retry_count': 0}
        response = client.post('/api/roles/' + role_id + '/messages', json={'content': '请明确执行这项仓库任务', 'skillIds': ['codex-delegate']})
        assert 'assistant_completed' in response.text, response.text
        def native_done(previous_attempt=None):
            values = client.get('/api/coding/roles/' + role_id + '/tasks').json()['tasks']
            return next((value for value in values if value['attemptId'] != previous_attempt
                and value['deliveryStatus'] == 'sent' and value['runtimeStatus'] in {'succeeded', 'failed'}), None)
        native = wait_for(native_done)
        assert native['status'] == 'validated_diff' and native['ownerActions']['hasEvidence'], native
        prefix = '/api/coding/roles/' + role_id
        response = client.post(prefix + '/tasks/' + native['id'] + '/reviews', json={'action': 'commit',
            'message': '主人核对的确切修改', 'authorName': 'Fixture Owner', 'authorEmail': 'owner@example.invalid'})
        assert response.status_code == 200, response.text
        review = response.json()
        assert '+work' in review['diff'] and review['validation'][0]['exitCode'] == 0
        response = client.post(prefix + '/reviews/' + review['id'] + '/approve', json={'approved': True, 'requestId': 'explicit-commit'})
        assert response.status_code == 202, response.text
        operation_id = response.json()['id']
        def operation_done(identifier):
            values = client.get(prefix + '/operations').json()['operations']
            return next((value for value in values if value['id'] == identifier and value['runtimeStatus'] in {'succeeded', 'failed'} and not value['runtimeAckDirty']), None)
        committed = wait_for(lambda: operation_done(operation_id))
        assert committed['status'] == 'validated_commit', committed
        assert client.put('/api/coding/publication/credential', json={'token': 'fixture-broker-secret'}).status_code == 200
        with platform(tmp_path, bare) as (endpoint, state):
            application.publication.platform_factory = lambda token, remote: GithubPlatform(token, remote, base_url=endpoint)
            application.publication.transport_factory = lambda *args: GitTransport(*args, destination=str(bare))
            response = client.post(prefix + '/tasks/' + native['id'] + '/reviews', json={'action': 'publish', 'title': 'Exact approved change', 'body': 'Reviewed body'})
            assert response.status_code == 200, response.text
            publication_review = response.json()
            response = client.post(prefix + '/reviews/' + publication_review['id'] + '/approve', json={'approved': True, 'requestId': 'explicit-publication'})
            assert response.status_code == 202, response.text
            delivered = wait_for(lambda: operation_done(response.json()['id']))
            assert delivered['status'] == 'delivered_draft', delivered
            assert state['posts'] == 1
            assert git(bare, 'rev-parse', 'refs/heads/' + publication_review['publishBranch']) == committed['commitSha']
        model.action = 'spawn_manage'
        model.arguments = {'action': 'continue', 'job_id': native['id'], 'task': 'work'}
        response = client.post('/api/roles/' + role_id + '/messages', json={'content': '在原生会话中明确继续', 'skillIds': ['codex-delegate']})
        assert 'assistant_completed' in response.text, response.text
        resumed = wait_for(lambda: native_done(native['attemptId']))
        assert resumed['attemptId'] != native['attemptId'] and resumed['status'] == 'validated_diff', resumed
        assert resumed['nativeSession'] == native['nativeSession']
        assert resumed['startHead'] == committed['commitSha'] and resumed['baseHead'] == native['baseHead']
        messages = sessions.list_messages('role:' + role_id)
        assert len([value for value in messages if value.metadata.get('internalEvent') == 'coding_commit']) == 1
        assert len([value for value in messages if value.metadata.get('internalEvent') == 'coding_publication']) == 1
        assert all(value.runId is None for value in messages if value.metadata.get('internalEvent', '').startswith('coding_'))
        assert model.calls == 4, 'only two real owner model turns; no completion or commit model call'
        response = client.post(prefix + '/tasks/' + native['id'] + '/reviews', json={'action': 'commit',
            'message': 'Must not survive revoke and re-enable', 'authorName': 'Owner', 'authorEmail': 'owner@example.invalid'})
        assert response.status_code == 200, response.text
        revoked_review = response.json()
        assert client.put('/api/coding/configuration', json={'enabled': False}).status_code == 200
        assert client.put('/api/coding/configuration', json={'enabled': True}).status_code == 200
        rejected = client.post(prefix + '/reviews/' + revoked_review['id'] + '/approve',
            json={'approved': True, 'requestId': 'old-approval-after-off-on'})
        assert rejected.status_code == 403 and model.calls == 4
    finally:
        asyncio.run(application.close())
