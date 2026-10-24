"""The compact owner settings preserve grants and control actual execution."""
import asyncio

import pytest

from backend.app import main
from backend.app.proactive_runtime import bindings
from backend.app.proactive_runtime.config import AutonomyConfig
from test_background_shell_api import api as shell_api
from test_spawn_api import api
from test_proactive_mcp import source
from test_proactive_snapshot import grant_for

pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.fixture
def settings_api(api, monkeypatch):
    client, roles, _, _ = api
    monkeypatch.setattr(main, '_proactive', None)
    response = client.post('/api/roles', json={'name': '主动推送设置', 'profile': {'profile': '测试角色'}})
    assert response.is_success, response.text
    role = response.json()['role']
    path = f"/api/roles/{role['id']}/autonomy"
    yield client, role['id'], path
    if main._proactive is not None:
        asyncio.run(main._proactive.close())


def model(client, name='角色模型'):
    response = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': name, 'apiKey': 'fixture-not-real'})
    assert response.is_success, response.text
    return response.json()['configuration']['id']


def update(value, **changes):
    return {**{key: item for key, item in value.items() if key != 'revision'},
            'expectedRevision': value['revision'], **changes}


def test_compact_defaults_and_save_preserve_existing_grants(settings_api):
    client, _, path = settings_api
    original = client.put(path, json={'expectedRevision': 0, 'config': {
        'driftToolGrants': ['read_file'], 'driftReadResourceIds': ['role-profile']}})
    assert original.status_code == 200, original.text
    response = client.get(path + '/settings')
    assert response.status_code == 200, response.text
    state = response.json()
    assert state['settings'] == {'revision': 1, 'enabled': False, 'profile': 'daily',
        'agentMaxSteps': 35, 'agentContentLimit': 5, 'agentWebFetchMaxChars': 8000,
        'driftMaxSteps': 20, 'driftMinIntervalHours': 3}
    saved = client.put(path + '/settings', json=update(state['settings'], profile='quiet', agentMaxSteps=12))
    assert saved.status_code == 200, saved.text
    config = client.get(path).json()['config']
    assert config['driftToolGrants'] == ['read_file']
    assert config['driftReadResourceIds'] == ['role-profile']
    assert config['profile'] == 'quiet' and config['agentMaxSteps'] == 12
    assert not config['activeDecisionEnabled'] and not config['driftEnabled']
    assert client.put(path + '/settings', json=update(state['settings'])).status_code == 409


def test_one_switch_uses_role_model_and_disables_both_modes(settings_api):
    client, role_id, path = settings_api
    state = client.get(path + '/settings').json()['settings']
    assert client.put(path + '/settings', json=update(state, enabled=True)).status_code == 422
    identifier = model(client)
    saved = client.put(path + '/settings', json=update(state, enabled=True))
    assert saved.status_code == 200, saved.text
    app = main._proactive_application()
    assert '尚无内容来源或空闲任务' in saved.json()['activityNote']
    config = AutonomyConfig.model_validate(app.query(role_id)['config'])
    assert config.activeDecisionEnabled and config.driftEnabled and config.inheritRoleModel
    with app._transaction() as db:
        generation = db.execute('SELECT generation FROM autonomy_roles WHERE role_id=?', (role_id,)).fetchone()[0]
    app.invalidate_model('unrelated-model')
    with app._transaction() as db:
        assert db.execute('SELECT generation FROM autonomy_roles WHERE role_id=?', (role_id,)).fetchone()[0] == generation
    resolved = app.effective_config(role_id, config)
    assert resolved.decisionModelConfigurationId == resolved.driftModelConfigurationId == identifier
    frozen = bindings.freeze(app, role_id, resolved)
    assert bindings.current(app, role_id, frozen)
    other = model(client, '新角色模型')
    assert client.put(f'/api/roles/{role_id}/model-configuration', json={'configurationId': other}).status_code == 200
    assert not bindings.current(app, role_id, frozen)
    assert app.effective_config(role_id, config).decisionModelConfigurationId == other
    disabled = client.put(path + '/settings', json=update(saved.json()['settings'], enabled=False))
    assert disabled.status_code == 200, disabled.text
    config = client.get(path).json()['config']
    assert not config['activeDecisionEnabled'] and not config['driftEnabled']


@pytest.mark.parametrize('field,value', [('agentMaxSteps', 0), ('agentContentLimit', 0),
    ('agentContentLimit', 6),
    ('agentWebFetchMaxChars', 0), ('driftMaxSteps', 21), ('driftMinIntervalHours', 2), ('profile', 'unknown')])
def test_compact_settings_reject_invalid_values_without_saving(settings_api, field, value):
    client, _, path = settings_api
    state = client.get(path + '/settings').json()['settings']
    response = client.put(path + '/settings', json=update(state, **{field: value}))
    assert response.status_code == 422, response.text
    assert client.get(path + '/settings').json()['settings'] == state


def test_disabling_preserves_expired_source_grants_and_reenabling_requires_valid_grants(settings_api, source, monkeypatch):
    from backend.app.mcp_runtime import McpHost
    client, role_id, path = settings_api
    _, store, data, *_ = source
    saved_source = store.save(data.model_copy(update={'roleIds': [role_id]}))
    grant = grant_for(source).model_copy(update={'configRevision': saved_source.revision, 'grantRevision': saved_source.grant_id})
    host = McpHost(store)
    monkeypatch.setattr(main, 'mcp_host', host)
    identifier = model(client)
    saved = client.put(path, json={'expectedRevision': 0, 'config': {'activeDecisionEnabled': True,
        'decisionModelConfigurationId': identifier, 'consentRevision': 1, 'sourceGrants': [grant.model_dump(mode='json')]}})
    assert saved.status_code == 200, saved.text
    store.save(data.model_copy(update={'roleIds': []}))
    state = client.get(path + '/settings').json()['settings']
    disabled = client.put(path + '/settings', json=update(state, enabled=False))
    assert disabled.status_code == 200, disabled.text
    assert client.get(path).json()['config']['sourceGrants'] == saved.json()['config']['sourceGrants']
    assert client.put(path + '/settings', json=update(disabled.json()['settings'], enabled=True)).status_code == 422


def test_quiet_strategy_survives_restart_without_early_decision(settings_api, monkeypatch):
    client, _, path = settings_api
    now = [1780000000.0]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model(client)
    state = client.get(path + '/settings').json()['settings']
    saved = client.put(path + '/settings', json=update(state, enabled=True, profile='quiet'))
    assert saved.status_code == 200, saved.text
    assert client.get(path).json()['nextDecisionAt'] == now[0] + 7200
    asyncio.run(main._proactive.close())
    monkeypatch.setattr(main, '_proactive', None)
    now[0] += 100
    assert client.get(path).json()['nextDecisionAt'] == now[0] + 7200
    now[0] += 1800
    assert asyncio.run(main._proactive.on_clock()) == 0
    assert client.get(path).json()['runs'] == []
