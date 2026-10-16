"""Host evidence and exact-session continuation through real local Git/CLI seams."""
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from backend.app.coding_tasks.codex_exec import CodexExecutor
from backend.app.coding_tasks import evidence as host_evidence
from backend.app.coding_tasks.errors import CodingFailure
from backend.app.coding_tasks.executor import HerdrExecutor
from backend.app.coding_tasks.git_repository import git, git_environment
from backend.app.coding_tasks.owned_process import OwnedProcess
from test_codex_executor import fixture_process, setup_attempt

pytestmark = pytest.mark.usefixtures('local_platform_only')


class LocalHerdrBoundary:
    """Herdr protocol seam; the editing CLI and validators are owned real processes."""
    def __init__(self, executable, root, **kwargs):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        home = self.root / 'home'
        home.mkdir(exist_ok=True)
        self.env = {'CODEX_HOME': str(home)}
        self.arguments = kwargs['agent_arguments']
        self.worktree = None
        self.agent = {'agent': 'codex', 'terminal_id': 'terminal', 'workspace_id': 'workspace',
                      'pane_id': 'pane', 'agent_session': {'value': '9f878f99-cdcc-4c5a-bc6b-66892194f543'}}
        self.journal = kwargs['resource_journal']

    def install_integration(self, kind): pass
    def start(self): return {'generation': 'fixture', 'pid': 1}
    def call(self, argv):
        self.worktree = Path(argv[argv.index('--path') + 1])
        if argv[1] == 'create':
            git(Path(argv[argv.index('--cwd') + 1]), 'worktree', 'add', '-b',
                argv[argv.index('--branch') + 1], str(self.worktree), argv[argv.index('--base') + 1])
        return {'root_pane': {'pane_id': 'pane'}}
    def launch_agent(self, *args):
        if self.arguments[0] == 'resume':
            assert self.arguments[1] == self.agent['agent_session']['value']
        return self.agent
    def prompt(self, name, prompt):
        handoff = Path(prompt.split('MEIDO_HANDOFF ', 1)[1])
        script = ('import json,sys;from pathlib import Path;'
                  'h=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"));'
                  'Path("fixture.txt").write_text(h["requirement"],encoding="utf-8");'
                  'Path(h["resultFile"]).write_text(json.dumps(dict(task_id=h["taskId"],'
                  'run_id=h["attemptId"],status="completed",summary="local fixture",'
                  'validation=["real-check"],blockers=[])),encoding="utf-8")')
        process = OwnedProcess([sys.executable, '-I', '-c', script, str(handoff)], self.worktree,
                               git_environment(), journal=self.journal)
        assert process.communicate(timeout=10).returncode == 0
    def observe_agent(self, name): return self.agent
    def close(self): pass


def execute(tmp_path, kind, record, ledger):
    # Ordinary task path at a normal Windows AppData-like depth, never the probe override.
    root = tmp_path / 'normal-windows-appdata-depth' / 'coding'
    executor = (CodexExecutor(root, record, ledger, authorized=lambda: True, process_factory=fixture_process)
                if kind == 'codex_exec' else
                HerdrExecutor(root, record, ledger, authorized=lambda: True, client_factory=LocalHerdrBoundary))
    outcome = executor.run(SimpleNamespace(deadline=time.monotonic() + 100))
    worktree = root / 't' / hashlib.sha256(record['origin']['role_id'].encode()).hexdigest()[:16] / record['id'][7:] / 'w'
    return outcome, worktree


@pytest.mark.parametrize('kind', ['herdr', 'codex_exec'])
def test_validated_diff_has_complete_host_evidence_after_all_owned_processes_exit(tmp_path, kind):
    record, ledger = setup_attempt(tmp_path)
    record['executor'] = kind
    record['requirement'] = 'host-attested fixture'
    record['nativeBuild'] = {'executable': sys.executable}
    outcome, worktree = execute(tmp_path, kind, record, ledger)
    assert outcome.status == 'succeeded', ledger.value
    evidence = ledger.value['validationEvidence']
    assert evidence['taskId'] == record['id'] and evidence['attemptId'] == record['attemptId']
    assert evidence['cleanupState'] == 'complete' and not ledger.jobs
    assert evidence['snapshot']['head'] == git(worktree, 'rev-parse', 'HEAD')
    assert evidence['snapshot']['files']['fixture.txt']['sha256'] == hashlib.sha256(record['requirement'].encode()).hexdigest()
    assert evidence['validation'][0]['argv'] == record['repository']['validation'][0]['argv']
    assert evidence['validation'][0]['exitCode'] == 0
    assert evidence['baseHead'] == ledger.task['baseHead'] == evidence['startHead']
    assert evidence['snapshot']['tree'] and evidence['snapshot']['index']['sha256']
    assert evidence['originalDeadlineAt'] > evidence['businessDeadlineAt'] > time.time()
    assert json.loads(json.dumps(evidence)) == evidence
    stored = record | ledger.task | ledger.value
    assert host_evidence.verify_attestation(worktree, stored, time.monotonic() + 30) == evidence
    original = (worktree / 'fixture.txt').read_bytes()
    (worktree / 'fixture.txt').write_bytes(b'not host-validated')
    with pytest.raises(CodingFailure, match='host_attestation_snapshot_changed'):
        host_evidence.verify_attestation(worktree, stored, time.monotonic() + 30)
    (worktree / 'fixture.txt').write_bytes(original)
    git(worktree, 'add', 'fixture.txt')
    with pytest.raises(CodingFailure, match='host_attestation_snapshot_changed'):
        host_evidence.verify_attestation(worktree, stored, time.monotonic() + 30)
    git(worktree, 'add', '--all')
    assert git(worktree, 'write-tree') == evidence['snapshot']['tree']


@pytest.mark.parametrize('kind', ['herdr', 'codex_exec'])
def test_host_committed_head_can_continue_exact_session_without_rewriting_initial_base(tmp_path, kind):
    record, ledger = setup_attempt(tmp_path)
    record.update(executor=kind, nativeBuild={'executable': sys.executable})
    result, worktree = execute(tmp_path, kind, record, ledger)
    assert result.status == 'succeeded', ledger.value
    base, session = ledger.task['baseHead'], ledger.value['nativeSession']
    git(worktree, 'add', '--all')
    git(worktree, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'explicit fixture host commit')
    committed = git(worktree, 'rev-parse', 'HEAD')
    continued = record | ledger.task | {'attemptId': 'attempt_' + uuid.uuid4().hex, 'nativeSession': session,
                                       'expectedHead': committed, 'requirement': 'explicit owner continuation'}
    ledger.value = {}
    result, _ = execute(tmp_path, kind, continued, ledger)
    assert result.status == 'succeeded', ledger.value
    assert ledger.value['nativeSession'] == session
    assert ledger.value['validationEvidence']['baseHead'] == base
    assert ledger.value['validationEvidence']['startHead'] == committed
    assert ledger.task['baseHead'] == base
    assert not ledger.jobs
    # A later unauthorised commit cannot be adopted just because the same session exists.
    git(worktree, 'add', '--all')
    git(worktree, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'unapproved fixture commit')
    rejected = continued | {'attemptId': 'attempt_' + uuid.uuid4().hex}
    ledger.value = {}
    result, _ = execute(tmp_path, kind, rejected, ledger)
    assert result.status == 'failed'
    assert ledger.value['blockers'] == ['worktree_base_changed']
    assert not ledger.value.get('dispatch') and not ledger.value.get('validationEvidence')
    assert not ledger.jobs


def test_original_attempt_budget_cannot_be_renewed_by_rerun_wall_clock_or_unproven_record(monkeypatch):
    clock = SimpleNamespace(monotonic=lambda: 100.0, time=lambda: 1000.0)
    monkeypatch.setattr(host_evidence, 'time', clock)
    record = {'attemptId': 'attempt_' + uuid.uuid4().hex}
    original = host_evidence.begin_attempt_budget(record, 10000)
    record.update(original)
    assert original['originalDeadlineAt'] == 1600
    assert original['businessDeadlineAt'] == 1540
    assert host_evidence.begin_attempt_budget(record, 20000) == original
    clock.monotonic = lambda: 641.0
    clock.time = lambda: 0.0
    with pytest.raises(TimeoutError):
        host_evidence.remaining_attempt_budget(record)
    assert host_evidence.remaining_attempt_budget(record, business=False) == 59
    unproven = record | {'attemptId': 'attempt_' + uuid.uuid4().hex}
    with pytest.raises(CodingFailure, match='original_attempt_budget_unprovable'):
        host_evidence.remaining_attempt_budget(unproven)
    with pytest.raises(CodingFailure, match='original_attempt_budget_unprovable'):
        host_evidence.begin_attempt_budget(unproven, 20000)
