"""Availability describes at least one current grant, never a task's authority."""
import hashlib
from pathlib import Path
import sys

import pytest

from backend.app.agent_runtime.skills import SkillRegistry
from backend.app.coding_tasks.config import CodingConfigStore, CodingRoleConfig
from backend.app.coding_tasks.git_repository import git

pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.fixture
def configured(tmp_path):
    config = CodingConfigStore(tmp_path / 'config.db', native_supported=lambda: True)
    try:
        grants = []
        for name in ('first', 'second'):
            root = tmp_path / name
            root.mkdir()
            git(root, 'init', '-b', 'main')
            git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '--allow-empty', '-m', 'base')
            grants.append(config.save_repository({'root': str(root), 'allowedRoleIds': ['role']}))
        auth = tmp_path / 'synthetic-auth'
        auth.mkdir()
        (auth / 'auth.json').write_text('{"fixture":"not-real-auth"}', encoding='utf-8')
        exe = Path(sys.executable).resolve(strict=True)
        profiles = []
        for executor in ('codex_exec', 'herdr'):
            profile = config.save_profile({'name': executor, 'executor': executor, 'kind': 'codex',
                'executable': str(exe), 'sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
                'version': '0.159.3', 'model': 'synthetic-only', 'authDirectory': str(auth),
                'allowedRoleIds': ['role']})
            # A private host seam, not native qualification or real account use.
            profiles.append(config.record_verification(profile['id'], revision=profile['revision'],
                native_session='synthetic', resumed_session='synthetic', manifest_hash='synthetic-proof'))
        config.set_enabled(True)
        role = CodingRoleConfig(enabled=True, repoGrantIds=[item['id'] for item in grants],
            profileIds=[item['id'] for item in profiles], defaultCodexProfileId=profiles[0]['id'],
            defaultHerdrProfileId=profiles[1]['id'])
        yield config, role, grants, profiles
    finally:
        config.close()


def revoke(config, grant):
    config.save_repository({key: ([] if key == 'allowedRoleIds' else grant[key])
        for key in ('id', 'root', 'allowedRoleIds')})


@pytest.mark.parametrize('first_state', ['revoked', 'missing', 'missing_path', 'replaced'])
def test_current_second_repository_keeps_explicit_skill_available(configured, first_state):
    config, role, grants, _ = configured
    rejected_id = grants[0]['id']
    if first_state == 'revoked':
        revoke(config, grants[0])
    elif first_state == 'missing':
        role.repoGrantIds[0] = rejected_id = 'old-missing-grant'
    else:
        root = Path(grants[0]['root'])
        root.rename(root.with_name('owned-first-original'))
        if first_state == 'replaced':
            root.mkdir()
            git(root, 'init', '-b', 'main')
    with pytest.raises((PermissionError, ValueError, OSError)):
        config.resolve('role', role, rejected_id, executor='codex_exec')
    assert config.resolve('role', role, grants[1]['id'], executor='codex_exec')[0]['id'] == grants[1]['id']
    assert config.availability('role', role, executor='codex_exec')['status'] == 'available'
    registry = SkillRegistry.discover(Path(__file__).resolve().parents[2] / 'backend/app/builtin_skills', source='builtin')
    def dependency_check(skill, name):
        status = config.availability('role', role, executor='codex_exec')
        return None if status['status'] == 'available' else status['reason']
    selection = registry.resolve(prompt='$codex-delegate', dependency_check=dependency_check)
    assert [item.skill_id for item in selection.skills] == ['codex-delegate'], selection.diagnostics


def test_no_current_grant_has_a_specific_bounded_unavailable_diagnostic(configured):
    config, role, grants, _ = configured
    for grant in grants:
        revoke(config, grant)
    role.repoGrantIds.append('old-missing-grant')
    status = config.availability('role', role, executor='codex_exec')
    assert status['status'] == 'unavailable'
    assert 'repository' in status['reason'] and len(status['reason']) <= 300


def test_all_current_repository_paths_invalid_remain_unavailable(configured):
    config, role, grants, _ = configured
    for grant in grants:
        root = Path(grant['root'])
        root.rename(root.with_name(root.name + '-owned-original'))
    status = config.availability('role', role, executor='codex_exec')
    assert status['status'] == 'unavailable'
    assert status['reason'] and len(status['reason']) <= 300


def test_candidate_scan_does_not_fall_back_to_another_executor_or_default(configured):
    config, role, grants, profiles = configured
    revoke(config, grants[0])
    role.defaultCodexProfileId = profiles[1]['id']  # A verified Herdr profile is not Codex exec authority.
    status = config.availability('role', role, executor='codex_exec')
    assert status['status'] == 'unavailable' and 'profile' in status['reason']
    with pytest.raises(PermissionError, match='profile'):
        config.resolve('role', role, grants[1]['id'], executor='codex_exec')
    role.defaultCodexProfileId = profiles[0]['id']
    assert config.availability('role', role, executor='codex_exec')['status'] == 'available'
    # Codex qualification also cannot replace a missing owned Herdr build.
    herdr = config.availability('role', role, executor='herdr')
    assert herdr['status'] == 'unavailable' and 'Herdr native build' in herdr['reason']


def test_candidate_scan_keeps_profile_qualification_and_early_gates(configured):
    config, role, grants, profiles = configured
    revoke(config, grants[0])
    profile = profiles[0]
    config.save_profile({key: value for key, value in profile.items()
                         if key not in {'verification', 'revision', 'authenticationRevision'}})
    status = config.availability('role', role, executor='codex_exec')
    assert status['status'] == 'unavailable' and 'verification' in status['reason']
    config.native_supported = lambda: False
    assert 'Windows' in config.availability('role', role, executor='codex_exec')['reason']
    config.set_enabled(False)
    assert config.availability('role', role, executor='codex_exec')['status'] == 'disabled'
