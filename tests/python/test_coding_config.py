from pathlib import Path
import sys
import hashlib

import pytest

from backend.app.coding_tasks.config import CodingConfigStore, CodingRoleConfig
from backend.app.coding_tasks.git_repository import git

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_coding_is_disabled_and_has_no_implicit_repository_or_profile(tmp_path):
    config = CodingConfigStore(tmp_path / 'coding.db')
    try:
        assert config.public()['enabled'] is False
        role = CodingRoleConfig()
        assert role.enabled is False
        assert role.repoGrantIds == []
        assert role.profileIds == []
        assert role.defaultHerdrProfileId is None
        assert role.maxAttemptsPerDay == 5
        assert config.availability('role-a', role)['status'] == 'disabled'
    finally:
        config.close()


def test_owner_can_grant_only_a_real_git_root_and_permission_survives_restart(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base')
    child = root / 'child'
    child.mkdir()
    config = CodingConfigStore(tmp_path / 'coding.db')
    try:
        with pytest.raises(ValueError, match='Git'):
            config.save_repository({'root': str(child), 'allowedRoleIds': ['role-a']})
        grant = config.save_repository({'root': str(root), 'allowedRoleIds': ['role-a']})
        assert grant['root'] == str(root.resolve())
        assert grant['baseRef'] == 'main'
        assert grant['localCommit'] is False
        assert grant['fetch'] is False
        assert grant['push'] is False
        assert grant['draftPr'] is False
        assert config.public()['repositories'] == [grant]
        config.close()
        config = CodingConfigStore(tmp_path / 'coding.db')
        assert config.public()['repositories'][0]['revision'] == grant['revision']
    finally:
        config.close()


def test_profile_never_discovers_desktop_auth_and_executable_change_is_rejected(tmp_path):
    auth = tmp_path / 'explicit-auth'
    auth.mkdir()
    executable = Path(sys.executable).resolve(strict=True)
    config = CodingConfigStore(tmp_path / 'coding.db')
    try:
        with pytest.raises(ValueError, match='SHA'):
            config.save_profile({'name': 'Fixture', 'kind': 'codex', 'executable': str(executable),
                                 'sha256': '0' * 64, 'version': 'fixture-1', 'model': 'local-fixture',
                                 'authDirectory': str(auth)})
        profile = config.save_profile({'name': 'Fixture', 'kind': 'codex', 'executable': str(executable),
                                       'sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
                                       'version': 'fixture-1', 'model': 'local-fixture', 'authDirectory': str(auth)})
        assert profile['authDirectory'] == str(auth.resolve())
        assert profile['verification']['status'] == 'unverified'
        config.set_enabled(True)
        role = CodingRoleConfig(enabled=True, profileIds=[profile['id']], defaultHerdrProfileId=profile['id'])
        assert config.availability('role-a', role)['status'] == 'unavailable'
        assert 'repositories' in config.public()
    finally:
        config.close()
