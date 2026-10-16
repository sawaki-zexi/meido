import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from backend.app.coding_tasks.codex_exec import CodexExecutor
from backend.app.coding_tasks.git_repository import git, repository_identity
from backend.app.coding_tasks.owned_process import OwnedProcess

pytestmark = pytest.mark.usefixtures('local_platform_only')
LAUNCHER = Path(sys.executable).resolve()


def fixture_process(argv, cwd, env, **kwargs):
    return OwnedProcess([sys.executable, '-X', 'utf8', str(Path(__file__).parent / 'coding_fixtures/exec_cli.py'), *argv[1:]],
                        cwd, env, **kwargs)


def setup_attempt(tmp_path):
    repository = tmp_path / 'repo'
    repository.mkdir()
    git(repository, 'init', '-b', 'main')
    git(repository, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--allow-empty', '-m', 'base')
    auth = tmp_path / 'auth'
    auth.mkdir()
    (auth / 'auth.json').write_text('{"fixture":"not-a-real-token"}')
    record = {'id': 'coding_' + uuid.uuid4().hex, 'attemptId': 'attempt_' + uuid.uuid4().hex,
        'origin': {'role_id': 'fixture'}, 'branch': 'codex/exec-' + uuid.uuid4().hex,
        'requirement': '完整中文🙂\n保留换行和引号"', 'executor': 'codex_exec',
        'profile': {'kind': 'codex', 'executor': 'codex_exec', 'version': '0.159.3', 'model': 'fixture-only',
            'executable': str(LAUNCHER), 'authDirectory': str(auth),
            'authenticationRevision': hashlib.sha256((auth / 'auth.json').read_bytes()).hexdigest()},
        'repository': repository_identity(str(repository)) | {'baseRef': 'main', 'validation': [
            {'id': 'real-check', 'argv': [sys.executable, '-I', '-c',
                'from pathlib import Path;assert Path("fixture.txt").read_text(encoding="utf-8")'], 'cwd': '.'}]}}
    class Ledger:
        def __init__(self): self.value, self.task, self.jobs = {}, {}, set()
        def update_attempt(self, identifier, **changes): self.value.update(changes)
        def update_task(self, identifier, **changes): self.task.update(changes)
        def resource_job(self, attempt, name, active): self.jobs.add(name) if active else self.jobs.discard(name)
    ledger = Ledger()
    return record, ledger


def test_real_git_owned_jsonl_utf8_exec_and_exact_resume(tmp_path):
    record, ledger = setup_attempt(tmp_path)
    executor = CodexExecutor(tmp_path / 'coding', record, ledger, authorized=lambda: True, process_factory=fixture_process)
    result = executor.run(SimpleNamespace(deadline=time.monotonic() + 100))
    assert result.status == 'succeeded', ledger.value
    assert ledger.value['status'] == 'validated_diff'
    assert ledger.value['cleanupState'] == 'complete'
    assert not ledger.jobs
    session = ledger.value['nativeSession']
    worktree = next((tmp_path / 'coding').glob('t/*/*/w'))
    observed = json.loads((worktree / 'observed.json').read_text(encoding='utf-8'))
    assert observed['requirement'] == record['requirement']
    assert observed['args'][:1] == ['exec']
    assert git(Path(record['repository']['root']), 'status', '--porcelain') == ''
    continued = record | ledger.task | {'nativeSession': session, 'attemptId': 'attempt_' + uuid.uuid4().hex,
                                      'requirement': '继续同一个会话🙂'}
    second = CodexExecutor(tmp_path / 'coding', continued, ledger, authorized=lambda: True, process_factory=fixture_process)
    assert second.run(SimpleNamespace(deadline=time.monotonic() + 100)).status == 'succeeded', ledger.value
    observed = json.loads((worktree / 'observed.json').read_text(encoding='utf-8'))
    assert observed['args'][:3] == ['exec', 'resume', session]
    assert observed['requirement'] == '继续同一个会话🙂'
    assert not list((tmp_path / 'coding').glob('t/*/*/native/home/codex/auth.json'))
    assert not ledger.jobs


def test_completed_native_turn_needing_owner_input_preserves_distinct_business_state(tmp_path):
    record, ledger = setup_attempt(tmp_path)
    record['requirement'] = 'needs-owner-input'
    executor = CodexExecutor(tmp_path / 'coding', record, ledger, authorized=lambda: True, process_factory=fixture_process)
    result = executor.run(SimpleNamespace(deadline=time.monotonic() + 100))
    assert result.status == 'failed'
    assert ledger.value['status'] == 'needs_input'
    assert ledger.value['nativeTurnStatus'] == 'completed'
    assert ledger.value['cleanupState'] == 'complete' and not ledger.jobs


@pytest.mark.parametrize('mode', ['bad-utf8', 'bad-json', 'frame-budget', 'log-budget', 'missing-terminal',
    'wrong-session', 'turn-failed', 'nonzero-exit', 'stale-result', 'missing-result', 'result-budget'])
def test_native_protocol_or_fresh_result_failure_never_becomes_validated_diff(tmp_path, mode):
    record, ledger = setup_attempt(tmp_path)
    record['requirement'] = 'fault:' + mode
    executor = CodexExecutor(tmp_path / 'coding', record, ledger, authorized=lambda: True, process_factory=fixture_process)
    result = executor.run(SimpleNamespace(deadline=time.monotonic() + 100))
    assert result.status == 'failed'
    assert ledger.value['status'] == 'unknown' and ledger.value['dispatch']
    assert ledger.value['cleanupState'] == 'complete' and not ledger.jobs
    assert ledger.value['validation'] == []
    if mode == 'frame-budget':
        assert ledger.value['blockers'] == ['codex_jsonl_frame_over_budget']
    if mode == 'log-budget':
        assert ledger.value['blockers'] == ['OutputBudgetExceeded']
    assert not list((tmp_path / 'coding').glob('t/*/*/native/home/codex/auth.json'))
