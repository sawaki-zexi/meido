from dataclasses import asdict
from pathlib import Path

import pytest

from backend.app.coding_tasks.ledger import CodingLedger
from backend.app.task_runtime import TaskOrigin

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_same_call_recovers_original_attempt_and_conflicting_prompt_never_creates_a_second(tmp_path):
    ledger = CodingLedger(tmp_path / 'coding.db')
    origin = TaskOrigin('role-a', 'role:role-a', 'run-a', 'call-a')
    grant = {'id': 'repo-a', 'revision': 'repo-revision'}
    profile = {'id': 'profile-a', 'revision': 'profile-revision', 'executor': 'herdr', 'kind': 'codex'}
    try:
        first, created = ledger.admit(origin, requirement='完整需求', repository=grant, profile=profile, max_daily=5)
        repeated, repeated_created = ledger.admit(origin, requirement='完整需求', repository=grant, profile=profile, max_daily=5)
        assert created is True
        assert repeated_created is False
        assert repeated['id'] == first['id']
        assert repeated['attemptId'] == first['attemptId']
        with pytest.raises(ValueError, match='conflict'):
            ledger.admit(origin, requirement='changed requirement', repository=grant, profile=profile, max_daily=5)
        assert len(ledger.list(origin)) == 1
        assert ledger.list(TaskOrigin('role-a', 'thread:other', source_scope_id='visitor')) == []
        with pytest.raises(KeyError):
            ledger.inspect(first['id'], TaskOrigin('role-b', 'role:role-b'))
    finally:
        ledger.close()


def test_persistent_attempt_limits_and_original_call_survive_continuation(tmp_path):
    path = tmp_path / 'coding.db'
    ledger = CodingLedger(path)
    origin = TaskOrigin('role-a', 'role:role-a', 'run-a', 'call-a')
    grant = {'id': 'repo-a', 'revision': 'repo-revision'}
    profile = {'id': 'profile-a', 'revision': 'profile-revision', 'executor': 'herdr', 'kind': 'codex'}
    try:
        first, _ = ledger.admit(origin, requirement='first', repository=grant, profile=profile, max_daily=2)
        with pytest.raises(ValueError, match='concurrency'):
            ledger.admit(TaskOrigin('role-a', 'role:role-a', 'run-b', 'call-b'), requirement='parallel',
                repository=grant, profile=profile, max_daily=2)
        ledger.update_attempt(first['attemptId'], status='validated_diff', cleanupState='complete', nativeSession='exact-native-id')
        second, _ = ledger.admit(TaskOrigin('role-a', 'role:role-a', 'run-b', 'call-b'), task_id=first['id'],
            requirement='second', repository=grant, profile=profile, max_daily=2)
        original, created = ledger.admit(origin, requirement='first', repository=grant, profile=profile, max_daily=2)
        assert not created and original['attemptId'] == first['attemptId']
        assert ledger.inspect(first['id'], origin)['attemptId'] == second['attemptId']
        ledger.update_attempt(second['attemptId'], status='validated_diff', cleanupState='complete', nativeSession='exact-native-id')
        ledger.close()
        ledger = CodingLedger(path)
        with pytest.raises(ValueError, match='daily attempt budget'):
            ledger.admit(TaskOrigin('role-a', 'role:role-a', 'run-c', 'call-c'), task_id=first['id'],
                requirement='third', repository=grant, profile=profile, max_daily=2)
        assert ledger.inspect(first['id'], origin)['nativeSession'] == 'exact-native-id'
    finally:
        ledger.close()


def test_atomic_account_role_and_repository_slots_reject_parallel_admission(tmp_path):
    ledger = CodingLedger(tmp_path / 'coding.db')
    profile = {'id': 'profile', 'revision': 'revision', 'executor': 'herdr', 'kind': 'codex'}
    def admit(role, repo, call):
        return ledger.admit(TaskOrigin(role, 'role:' + role, 'run', call), requirement='bounded work',
            repository={'id': repo, 'revision': 'revision'}, profile=profile, max_daily=5)
    try:
        admit('role-a', 'repo-a', 'call-a')
        with pytest.raises(ValueError, match='concurrency'): admit('role-a', 'repo-b', 'call-b')
        with pytest.raises(ValueError, match='concurrency'): admit('role-b', 'repo-a', 'call-b')
        admit('role-b', 'repo-b', 'call-b')
        with pytest.raises(ValueError, match='concurrency'): admit('role-c', 'repo-c', 'call-c')
    finally:
        ledger.close()
