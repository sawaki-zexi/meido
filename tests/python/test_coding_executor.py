import json
import hashlib
from pathlib import Path
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from backend.app.coding_tasks.executor import HerdrExecutor
from backend.app.coding_tasks.git_repository import git, repository_identity

pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.mark.parametrize('kind', ['codex', 'claude'])
@pytest.mark.parametrize('changed_state', ['none', 'head', 'authentication'])
def test_host_checks_original_base_and_final_diff_before_accepting_native_completion(tmp_path, kind, changed_state):
    repository = tmp_path / 'repo'
    repository.mkdir()
    git(repository, 'init', '-b', 'main')
    git(repository, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--allow-empty', '-m', 'base')
    auth = tmp_path / 'auth'
    auth.mkdir()
    (auth / ('auth.json' if kind == 'codex' else '.credentials.json')).write_text('{"fixture":"done"}')
    task_token, attempt_token = uuid.uuid4().hex, uuid.uuid4().hex
    record = {'id': 'coding_' + task_token, 'attemptId': 'attempt_' + attempt_token,
              'origin': {'role_id': 'role-fixture'}, 'branch': 'codex/shutdown-fixture',
              'requirement': 'validated content', 'nativeBuild': {'executable': sys.executable},
              'profile': {'kind': kind, 'model': 'fixture', 'executable': sys.executable,
                          'authDirectory': str(auth), 'authenticationRevision': hashlib.sha256(b'{"fixture":"done"}').hexdigest()},
              'repository': repository_identity(str(repository)) | {'baseRef': 'main',
                  'validation': [{'id': 'real-host-check', 'argv': [sys.executable, '-c',
                      'from pathlib import Path; assert Path("fixture.txt").read_text() == "validated content"']}]}}
    if changed_state == 'authentication':
        record['profile']['authenticationRevision'] = '0' * 64
    if changed_state == 'head':
        record['nativeSession'] = '9f878f99-cdcc-4c5a-bc6b-66892194f543'
        record['baseHead'] = git(repository, 'rev-parse', 'HEAD')
        worktree = tmp_path / 'coding/t' / hashlib.sha256(b'role-fixture').hexdigest()[:16] / task_token / 'w'
        git(repository, 'worktree', 'add', '-b', record['branch'], str(worktree), 'main')
        (worktree / 'manual.txt').write_text('unapproved commit')
        git(worktree, 'add', 'manual.txt')
        git(worktree, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'unapproved head')
    class Ledger:
        value = {}
        def update_attempt(self, identifier, **changes): self.value.update(changes)
        def update_task(self, identifier, **changes): pass
        def resource_job(self, attempt, name, active): pass
    class NativeBoundary:
        def __init__(self, executable, root, **kwargs):
            self.root = root
            self.root.mkdir(parents=True)
            home = root / 'home'
            home.mkdir()
            self.env = {'CODEX_HOME': str(home), 'CLAUDE_CONFIG_DIR': str(home)}
            arguments = kwargs['agent_arguments']
            self.allowed = [Path(arguments[index + 1]).resolve() for index, value in enumerate(arguments) if value == '--add-dir']
            if kind == 'claude':
                if '--settings' not in arguments:
                    raise PermissionError('dontAsk requires explicit scoped edit allow rules')
                policy = json.loads(arguments[arguments.index('--settings') + 1])['permissions']
                assert len(policy['allow']) == 2 and all(item.startswith('Edit(//') for item in policy['allow'])
                assert 'Bash' in policy['deny'] and 'PowerShell' in policy['deny']
            self.worktree = None
            self.agent = {'agent': kind, 'terminal_id': 'terminal', 'workspace_id': 'workspace',
                          'pane_id': 'pane', 'agent_session': {'value': '9f878f99-cdcc-4c5a-bc6b-66892194f543'}}
        def install_integration(self, kind): pass
        def start(self): return {'generation': record['attemptId'], 'pid': 1}
        def call(self, argv):
            self.worktree = Path(argv[argv.index('--path') + 1])
            if not self.worktree.exists():
                git(repository, 'worktree', 'add', '-b', record['branch'], str(self.worktree), 'main')
            return {'root_pane': {'pane_id': 'pane'}}
        def launch_agent(self, *args): return self.agent
        def prompt(self, name, prompt):
            path = Path(prompt.split('MEIDO_HANDOFF ', 1)[1])
            if self.allowed != [path.resolve().parent]:
                raise PermissionError('restricted CLI cannot write this attempt result directory')
            handoff = json.loads(path.read_text(encoding='utf-8'))
            (self.worktree / 'fixture.txt').write_text('validated content')
            Path(handoff['resultFile']).write_text(json.dumps({'task_id': handoff['taskId'],
                'run_id': handoff['attemptId'], 'status': 'completed', 'summary': 'done',
                'validation': ['real-host-check'], 'blockers': []}))
        def observe_agent(self, name): return self.agent
        def close(self):
            if self.worktree is not None:
                (self.worktree / 'fixture.txt').write_text('late unvalidated edit')
    ledger = Ledger()
    executor = HerdrExecutor(tmp_path / 'coding', record, ledger, authorized=lambda: True,
                             client_factory=NativeBoundary)
    outcome = executor.run(SimpleNamespace(deadline=time.monotonic() + 100))
    assert outcome.status == 'failed'
    if changed_state in {'head', 'authentication'}:
        assert ledger.value['status'] == 'failed'
        assert ledger.value['blockers'] == ['worktree_base_changed' if changed_state == 'head' else 'dedicated_cli_authentication_changed']
        assert not ledger.value.get('dispatch')
        return
    assert ledger.value['status'] == 'unknown'
    assert ledger.value['cleanupState'] == 'complete'
    assert ledger.value['blockers'] == ['files_changed_after_validation']
    assert ledger.value['validation'][0]['status'] == 'passed'
    assert ledger.value['summary'] == '[REDACTED]'
    assert 'done' not in outcome.output_tail
