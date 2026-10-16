import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

from backend.app.coding_tasks.herdr import HerdrClient
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.task_runtime import TaskOrigin, TaskRuntime
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.git_repository import git, repository_identity

pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.mark.parametrize('kind', ['codex', 'claude'])
def test_owner_qualification_uses_real_owned_server_and_exact_resume_with_synthetic_cli_evidence(tmp_path, native_herdr, kind, monkeypatch):
    import asyncio
    from backend.app.coding_tasks.config import CodingConfigStore
    fixture = os.getenv('MEIDO_TEST_CODING_CLI')
    if not fixture:
        pytest.skip('explicit compiled no-network CLI fixture required')
    if kind == 'codex':
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            monkeypatch.setenv(key, 'http://127.0.0.1:1')
        monkeypatch.delenv('NO_PROXY', raising=False)
        monkeypatch.delenv('no_proxy', raising=False)
    executable = Path(fixture).parent / (kind + '.exe')
    auth = tmp_path / 'explicit-auth'
    auth.mkdir()
    (auth / ('auth.json' if kind == 'codex' else '.credentials.json')).write_text('{"fixture":"not-a-real-account"}')
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    tasks = TaskRuntime(tmp_path / 'tasks')
    config = CodingConfigStore(tmp_path / 'config.db')
    build = config.set_native_build(str(native_herdr), hashlib.sha256(native_herdr.read_bytes()).hexdigest())
    profile = config.save_profile({'name': 'Synthetic ' + kind, 'kind': kind, 'executable': str(executable),
        'sha256': hashlib.sha256(executable.read_bytes()).hexdigest(), 'version': '0.159.3' if kind == 'codex' else '2.1.283',
        'model': 'no-model-synthetic-fixture', 'authDirectory': str(auth), 'allowedRoleIds': ['role-fixture']})
    application = CodingApplication(tmp_path / 'c', tasks, sessions, role_config=lambda _: CodingRoleConfig(),
        authorized=lambda _: False, config=config)
    def client_factory(*args, **kwargs):
        client = HerdrClient(*args, **kwargs)
        client.env.update({'MEIDO_CODING_FIXTURE_PYTHON': sys.executable,
                           'MEIDO_CODING_FIXTURE_SCRIPT': str(Path(__file__).parent / 'coding_fixtures/interactive_cli.py')})
        return client
    application.probes.client_factory = client_factory
    try:
        with pytest.raises(ValueError, match='two native'):
            application.probes.start(profile['id'], revision=profile['revision'], role_id='role-fixture',
                build_sha256=build['sha256'], approve_native_turns=0, request_id='native-fixture-probe')
        accepted = application.probes.start(profile['id'], revision=profile['revision'], role_id='role-fixture',
            build_sha256=build['sha256'], approve_native_turns=2, request_id='native-fixture-probe')
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            asyncio.run(application.tick())
            observed = application.probes.get(accepted['id'])
            if observed['status'] not in {'accepted', 'running'} and observed.get('runtimeResult'):
                break
            time.sleep(.05)
        assert observed['status'] == 'verified', observed
        assert observed['runtimeAckDirty'] is False
        assert observed['cleanupState'] == 'complete'
        assert observed.get('resourceJobs') == []
        turns = observed['evidence']['turns']
        assert len(turns) == 2 and turns[0]['nativeSession'] == turns[1]['nativeSession']
        assert config.profile(profile['id'])['verification']['status'] == 'verified'
        # This proves native transport and the host qualification workflow,
        # with explicitly synthetic transcript events. No actual CLI account,
        # model task quality or real CLI sandbox refusal has been verified.
        assert application.probes.start(profile['id'], revision=profile['revision'], role_id='role-fixture',
            build_sha256=build['sha256'], approve_native_turns=2, request_id='native-fixture-probe')['id'] == accepted['id']
    finally:
        asyncio.run(application.close())
        tasks.close()


@pytest.fixture
def native_herdr():
    path = os.getenv('MEIDO_TEST_HERDR')
    if os.name != 'nt' or not path:
        pytest.skip('explicit built owned Herdr required for Windows native acceptance')
    return Path(path)


def test_owned_native_server_reports_its_actual_generation_and_creates_real_worktree(tmp_path, native_herdr):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    worktree = tmp_path / 'task' / 'worktree'
    client = HerdrClient(native_herdr, tmp_path / 'native', generation='fixture-generation-1',
                         session='meido-fixture-one', agent_kind='codex', agent_executable=Path(sys.executable),
                         agent_arguments=['--fixture-only'], deadline=time.monotonic() + 30)
    try:
        status = client.start()
        assert status['generation'] == 'fixture-generation-1'
        assert status['pid'] == client.server_pid
        assert status['protocol'] == 1
        result = client.call(['worktree', 'create', '--cwd', str(root), '--branch', 'codex/meido-fixture',
                              '--base', base, '--path', str(worktree), '--label', 'Fixture', '--no-focus'])
        assert worktree.is_dir()
        assert git(worktree, 'branch', '--show-current') == 'codex/meido-fixture'
        assert git(worktree, 'rev-parse', 'HEAD') == base
        assert result
    finally:
        client.close()
    assert client.cleanup_state == 'complete'


def test_application_finishes_actual_herdr_worktree_and_validates_host_command(tmp_path, native_herdr):
    from backend.app.coding_tasks.executor import HerdrExecutor
    import asyncio
    fixture = os.getenv('MEIDO_TEST_CODING_CLI')
    if not fixture:
        pytest.skip('explicit compiled no-network CLI fixture required')
    fixture = Path(fixture).parent / 'codex.exe'
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base')
    identity = repository_identity(str(root))
    auth = tmp_path / 'explicit-fixture-auth'
    auth.mkdir()
    (auth / 'auth.json').write_text('{"fixture":"not-a-real-account"}')
    grant = identity | {'id': 'repo-fixture', 'revision': 'fixture-revision', 'baseRef': 'main',
                        'validation': [{'id': 'fixture', 'argv': [sys.executable, '-c', 'from pathlib import Path; assert Path("fixture.txt").read_text(encoding="utf-8") == "完整原生需求🙂"'], 'cwd': '.'}]}
    profile = {'id': 'profile-fixture', 'revision': 'fixture-profile-revision', 'executor': 'herdr', 'kind': 'codex',
               'executable': str(fixture), 'model': 'local-fixture', 'authDirectory': str(auth)}
    class Configuration:
        def resolve(self, *args, **kwargs):
            return grant, profile, {'executable': str(native_herdr)}
        def still_authorized(self, *args):
            return True
        def close(self):
            pass
    def client_factory(*args, **kwargs):
        client = HerdrClient(*args, **kwargs)
        client.env.update({'MEIDO_CODING_FIXTURE_PYTHON': sys.executable,
                           'MEIDO_CODING_FIXTURE_SCRIPT': str(Path(__file__).parent / 'coding_fixtures/interactive_cli.py')})
        return client
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-native')
    tasks = TaskRuntime(tmp_path / 'runtime')
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True, config=Configuration(),
        executor_factory=lambda record, ledger: HerdrExecutor(tmp_path / 'coding', record, ledger,
            authorized=lambda: True, client_factory=client_factory))
    origin = TaskOrigin('role-native', 'role:role-native', 'run-native', 'call-native')
    try:
        accepted = application.start_task(origin, repository=str(root), requirement='完整原生需求🙂')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            asyncio.run(application.tick())
            result = application.inspect(accepted['id'], origin)
            if result['deliveryStatus'] == 'sent':
                break
            time.sleep(.05)
        assert result['status'] == 'validated_diff', result
        assert result['cleanupState'] == 'complete'
        assert result['runtimeAckDirty'] is False
        assert result['nativeSession']
        assert result['validation'][0]['status'] == 'passed'
        assert sessions.list_messages(origin.session_key)[0].metadata['internalEvent'] == 'coding_completion'
        assert not (root / 'fixture.txt').exists()
        first_native = result['nativeSession']
        next_origin = TaskOrigin('role-native', 'role:role-native', 'next-run', 'continue-native')
        continued = application.continue_task(accepted['id'], next_origin, requirement='完整原生需求🙂')
        assert continued['id'] == accepted['id'] and continued['attemptId'] != accepted['attemptId']
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            asyncio.run(application.tick())
            result = application.inspect(accepted['id'], next_origin)
            if result['deliveryStatus'] == 'sent':
                break
            time.sleep(.05)
        assert result['status'] == 'validated_diff', result
        assert result['nativeSession'] == first_native
        assert result['cleanupState'] == 'complete'
        cancel_origin = TaskOrigin('role-native', 'role:role-native', 'cancel-run', 'continue-cancel')
        holding = application.continue_task(accepted['id'], cancel_origin, requirement='hold until cancellation')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            pending = application.ledger.attempt(holding['attemptId'])
            if pending.get('dispatch'):
                break
            time.sleep(.05)
        assert pending.get('dispatch'), pending
        application.cancel_task(accepted['id'], cancel_origin)
        asyncio.run(application.tick())
        stopped = application.inspect(accepted['id'], cancel_origin)
        assert stopped['cleanupState'] == 'complete', stopped
        assert stopped['status'] == 'cancelled', stopped
    finally:
        asyncio.run(application.close())
        tasks.close()


@pytest.mark.parametrize('kind', ['codex', 'claude'])
def test_real_herdr_keeps_native_fixture_session_and_entire_utf8_requirement(tmp_path, native_herdr, kind):
    fixture = os.getenv('MEIDO_TEST_CODING_CLI')
    if not fixture:
        pytest.skip('explicit compiled no-network CLI fixture required')
    fixture = str(Path(fixture).parent / (kind + '.exe'))
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    worktree = tmp_path / 'task' / 'worktree'
    arguments = ['--model', 'local-fixture']
    client = HerdrClient(native_herdr, tmp_path / 'native', generation='fixture-agent-' + kind,
                         session='meido-fixture-' + kind, agent_kind=kind, agent_executable=Path(fixture),
                         agent_arguments=arguments, deadline=time.monotonic() + 45)
    client.env.update({'MEIDO_CODING_FIXTURE_PYTHON': sys.executable,
                       'MEIDO_CODING_FIXTURE_SCRIPT': str(Path(__file__).parent / 'coding_fixtures/interactive_cli.py')})
    handoff = tmp_path / 'handoff.json'
    output = tmp_path / 'result.json'
    requirement = '完整需求：保留中文和🙂，不从终端摘要猜任务。'
    handoff.write_text(json.dumps({'taskId': 'task-fixture', 'attemptId': 'attempt-fixture', 'requirement': requirement,
                                   'resultFile': str(output)}, ensure_ascii=False), encoding='utf-8')
    try:
        client.start()
        workspace = client.call(['worktree', 'create', '--cwd', str(root), '--branch', 'codex/meido-agent',
                                 '--base', base, '--path', str(worktree), '--label', 'Fixture', '--no-focus'])
        pane = workspace['root_pane']['pane_id']
        agent = client.launch_agent('fixture-agent', pane, kind, arguments)
        assert agent['agent'] == kind
        native = agent['agent_session']['value']
        assert native
        client.prompt('fixture-agent', 'MEIDO_HANDOFF ' + str(handoff))
        deadline = time.monotonic() + 10
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert json.loads(output.read_text(encoding='utf-8'))['summary'] == requirement
        assert (worktree / 'fixture.txt').read_text(encoding='utf-8') == requirement
        assert client.observe_agent('fixture-agent')['agent_session']['value'] == native
    finally:
        client.close()
    assert client.cleanup_state == 'complete'
