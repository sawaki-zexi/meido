import pytest

from backend.app.models import RoleInput, RoleUpdateInput
from backend.app.role_store import RoleStore

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_saved_coding_grants_survive_restart_and_older_client_omission(tmp_path):
    root = tmp_path / 'roles'
    store = RoleStore(root)
    role = store.create(RoleInput.model_validate({'name': 'Coding role', 'profile': {'profile': 'Fixture'},
        'agentConfig': {'coding': {'enabled': True, 'repoGrantIds': ['repo-a'], 'profileIds': ['profile-a'],
                                   'defaultHerdrProfileId': 'profile-a', 'maxAttemptsPerDay': 3}}}))
    assert store.get(role.id).agentConfig.model_dump()['coding']['enabled'] is True
    store = RoleStore(root)
    store.update(role.id, RoleUpdateInput.model_validate({'name': 'Coding role', 'profile': {'profile': 'Fixture'},
                                                        'agentConfig': {'enabledTools': ['read_file']}}))
    preserved = RoleStore(root).get(role.id).agentConfig.model_dump()['coding']
    assert preserved == {'enabled': True, 'repoGrantIds': ['repo-a'], 'profileIds': ['profile-a'],
                         'defaultHerdrProfileId': 'profile-a', 'defaultCodexProfileId': None, 'maxAttemptsPerDay': 3}
    store = RoleStore(root)
    store.update(role.id, RoleUpdateInput.model_validate({'name': 'Coding role', 'profile': {'profile': 'Fixture'},
                                                        'agentConfig': {'coding': {'enabled': False}}}))
    disabled = RoleStore(root).get(role.id).agentConfig.model_dump()['coding']
    assert disabled['enabled'] is False
    assert disabled['repoGrantIds'] == ['repo-a']
    assert disabled['maxAttemptsPerDay'] == 3
