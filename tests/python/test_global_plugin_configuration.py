import os
import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from backend.app.session_manager import SessionManager
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime


@pytest.fixture
def client(tmp_path, monkeypatch):
    initialize_databases(tmp_path / 'data')
    roles = RoleStore(tmp_path / 'roles')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    tasks = TaskRuntime(tmp_path / 'tasks')
    monkeypatch.setattr(main, 'store', roles)
    monkeypatch.setattr(main, 'session_store', sessions)
    monkeypatch.setattr(main, 'session_manager', SessionManager(roles, sessions))
    monkeypatch.setattr(main, 'task_runtime', tasks)
    from backend.app.agent_runtime import RuntimeManager
    monkeypatch.setattr(main, 'runtime_manager', RuntimeManager(sessions))
    from backend.app.plugin_configuration import PluginConfigurationStore
    monkeypatch.setattr(main, 'plugin_configuration', PluginConfigurationStore(tmp_path / 'plugins.json'))
    try:
        yield TestClient(main.app)
    finally:
        tasks.close()


def test_browser_enable_is_global_and_does_not_rewrite_role_data(client):
    roles = [client.post('/api/roles', json={'name': name, 'profile': {'profile': 'role'}}).json()['role']
             for name in ['first', 'second']]
    before = [main.store.get(role['id']).model_dump() for role in roles]
    settings = client.get('/api/plugin-management/browser').json()
    assert settings['enabled'] is False
    enabled = client.put('/api/plugin-management/browser/enabled', json={
        'enabled': True, 'expectedRevision': settings['revision'],
    })
    assert enabled.status_code == 200, enabled.text
    for role in roles:
        catalog = client.get(f"/api/roles/{role['id']}/tools").json()['tools']
        assert next(item for item in catalog if item['name'] == 'agent_browser_open')['enabled']
    assert [main.store.get(role['id']).model_dump() for role in roles] == before
    disabled = client.put('/api/plugin-management/browser/enabled', json={
        'enabled': False, 'expectedRevision': enabled.json()['revision'],
    })
    assert disabled.status_code == 200, disabled.text
    for role in roles:
        catalog = client.get(f"/api/roles/{role['id']}/tools").json()['tools']
        assert not next(item for item in catalog if item['name'] == 'agent_browser_open')['enabled']


def test_memory_switch_patch_preserves_independently_paused_memory(client, monkeypatch, tmp_path):
    from backend.app.memory_config import MemorySettings, MemorySettingsStore
    settings = MemorySettingsStore(tmp_path / 'memory.json')
    settings.save(MemorySettings(enabled=False, embeddingEnabled=True))
    monkeypatch.setattr(main, 'memory_settings_store', settings)
    response = client.put('/api/memory/settings', json={'embeddingEnabled': False})
    assert response.status_code == 200
    assert response.json()['enabled'] is False
    assert response.json()['embeddingEnabled'] is False


def test_parameters_and_activation_are_independent_and_stale_edits_conflict(client):
    browser = client.get('/api/plugin-management/browser').json()
    configured = client.put('/api/plugin-management/browser/configuration', json={
        'values': {'headed': True, 'timeout_seconds': 17}, 'expectedRevision': browser['revision'],
    })
    assert configured.status_code == 200, configured.text
    assert configured.json()['enabled'] is False
    assert client.put('/api/plugin-management/browser/enabled', json={
        'enabled': True, 'expectedRevision': browser['revision'],
    }).status_code == 409
    started = client.put('/api/plugin-management/browser/enabled', json={
        'enabled': True, 'expectedRevision': configured.json()['revision'],
    }).json()
    assert started['values'] == {'headed': True, 'timeout_seconds': 17}
    assert client.put('/api/plugin-management/browser/configuration', json={
        'values': {'enabled': False}, 'expectedRevision': started['revision'],
    }).status_code == 422
    assert client.put('/api/plugin-management/browser/enabled', json={
        'enabled': 1, 'expectedRevision': started['revision'],
    }).status_code == 422
    from backend.app.plugin_configuration import PluginConfigurationStore
    restored = PluginConfigurationStore(main.plugin_configuration.path).get('browser')
    assert restored.enabled and restored.values == {'headed': True, 'timeout_seconds': 17}


def test_legacy_role_flags_cannot_bypass_global_off(client):
    role = client.post('/api/roles', json={'name': 'legacy', 'profile': {'profile': 'role'},
        'agentConfig': {'browserUse': {'enabled': True}, 'enabledPlugins': ['browser_use'],
                        'enabledTools': ['agent_browser_open'], 'grantedCapabilities': ['browser.session']},
    }).json()['role']
    catalog = client.get(f"/api/roles/{role['id']}/tools").json()['tools']
    assert not next(item for item in catalog if item['name'] == 'agent_browser_open')['modelVisible']


def test_unconfirmed_cleanup_blocks_reenable_and_can_be_retried(client, monkeypatch):
    async def incomplete(config):
        raise RuntimeError('original owner not yet reaped')
    original = main.browser_use_sessions.reconfigure
    monkeypatch.setattr(main.browser_use_sessions, 'reconfigure', incomplete)
    failed = client.put('/api/plugin-management/browser/enabled', json={'enabled': True, 'expectedRevision': 0})
    assert failed.status_code == 503
    state = client.get('/api/plugin-management/browser').json()
    assert not state['enabled'] and state['cleanupPending']
    assert client.put('/api/plugin-management/browser/enabled', json={
        'enabled': True, 'expectedRevision': state['revision'],
    }).status_code == 409
    monkeypatch.setattr(main.browser_use_sessions, 'reconfigure', original)
    retry = client.put('/api/plugin-management/browser/enabled', json={
        'enabled': False, 'expectedRevision': state['revision'],
    })
    assert retry.status_code == 200 and not retry.json()['cleanupPending']


def test_browser_failed_close_retries_original_owner_before_reenable(client, monkeypatch, tmp_path):
    import asyncio
    from backend.app.agent_runtime import CancellationToken
    from backend.app.browser_use import BrowserSessions, BrowserUseConfig
    class Owner:
        closes = 0
        alive = True
        async def call(self, *args, **kwargs):
            return {'text': 'local fixture'}
        async def close(self):
            self.closes += 1
            if self.closes == 1:
                raise RuntimeError('still alive')
            self.alive = False
    owner = Owner()
    browser = BrowserSessions(tmp_path / 'browser', BrowserUseConfig(), transport=owner,
        enabled=lambda: main.plugin_configuration.enabled('browser'))
    monkeypatch.setattr(main, 'browser_use_sessions', browser)
    on = client.put('/api/plugin-management/browser/enabled', json={'enabled': True, 'expectedRevision': 0}).json()
    asyncio.run(browser.call('role-a', 'agent_browser_open', {'url': 'http://127.0.0.1/'}, CancellationToken()))
    failed = client.put('/api/plugin-management/browser/enabled', json={'enabled': False, 'expectedRevision': on['revision']})
    assert failed.status_code == 503 and owner.alive
    state = client.get('/api/plugin-management/browser').json()
    retry = client.put('/api/plugin-management/browser/enabled', json={'enabled': False, 'expectedRevision': state['revision']})
    assert retry.status_code == 200
    assert not owner.alive and owner.closes == 2


@pytest.mark.parametrize('reenable', [False, True])
def test_browser_call_uses_current_plugin_state_in_real_http_turn(client, monkeypatch, tmp_path, local_platform_only, reenable):
    import httpx
    from backend.app.browser_use import BrowserSessions, BrowserUseConfig
    from backend.app.memory_config import MemorySettingsStore
    from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
    # This test exercises browser revocation, not automatic model-backed memory.
    memory = MemorySettingsStore(tmp_path / 'memory-settings.json')
    memory.update(enabled=False)
    monkeypatch.setattr(main, 'memory_settings_store', memory)
    class Transport:
        calls = []
        async def call(self, name, arguments, **kwargs):
            self.calls.append(name)
            return {'text': 'current browser result'}
        async def close(self):
            pass
    transport = Transport()
    monkeypatch.setattr(main, 'browser_use_sessions', BrowserSessions(tmp_path / 'browser', BrowserUseConfig(),
        transport=transport, enabled=lambda: main.plugin_configuration.enabled('browser')))
    class Adapter:
        count = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            if self.count == 1:
                yield ModelToolCall('search', 'tool_search', {'query': 'select:agent_browser_open'})
                yield ModelResponseDone("tool_use")
            elif self.count == 2:
                assert 'agent_browser_open' in {item['function']['name'] for item in tools}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://local') as settings:
                    for enabled in ((False, True) if reenable else (False,)):
                        current = (await settings.get('/api/plugin-management/browser')).json()
                        changed = await settings.put('/api/plugin-management/browser/enabled', json={
                            'enabled': enabled, 'expectedRevision': current['revision']})
                        assert changed.status_code == 200
                yield ModelToolCall('current-open', 'agent_browser_open', {'url': 'http://127.0.0.1/'})
                yield ModelResponseDone("tool_use")
            else:
                results = [item for item in messages if item['role'] == 'tool']
                if reenable:
                    assert 'current browser result' in results[-1]['content']
                    yield ModelTextDelta('当前浏览器结果已返回')
                else:
                    assert 'revoked' in results[-1]['content'] or 'changed' in results[-1]['content']
                    yield ModelTextDelta('插件关闭，调用已撤销')
    adapter = Adapter()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    on = client.put('/api/plugin-management/browser/enabled', json={'enabled': True, 'expectedRevision': 0})
    assert on.status_code == 200
    role = client.post('/api/roles', json={'name': 'browser turn', 'profile': {'profile': 'role'}}).json()['role']
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '看网页'})
    assert response.status_code == 200
    assert ('当前浏览器结果已返回' if reenable else '插件关闭，调用已撤销') in response.text
    assert adapter.count == 3
    assert transport.calls == (['agent_browser_open'] if reenable else [])


def test_activation_failure_leaves_plugin_denied_until_explicit_cleanup(client, monkeypatch):
    pet = main._desktop_pet_application()
    calls = []
    async def activate():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError('new host still requires cleanup')
    monkeypatch.setattr(pet, 'plugin_changed', activate)
    failed = client.put('/api/plugin-management/pet/enabled', json={'enabled': True, 'expectedRevision': 0})
    assert failed.status_code == 503
    current = client.get('/api/plugin-management/pet').json()
    assert not current['enabled'] and current['cleanupPending']
    retry = client.put('/api/plugin-management/pet/enabled', json={'enabled': False, 'expectedRevision': current['revision']})
    assert retry.status_code == 200 and not retry.json()['enabled'] and not retry.json()['cleanupPending']


@pytest.mark.skipif(os.name != 'nt', reason='Coding Agent host is Windows-only by specification')
def test_coding_global_enable_ignores_legacy_switch_but_requires_trusted_repository(client):
    role = client.post('/api/roles', json={'name': 'coding policy', 'profile': {'profile': 'role'}}).json()['role']
    assert not role['agentConfig']['coding']['enabled']
    assert client.put('/api/coding/configuration', json={'enabled': True}).status_code == 200
    state = client.get(f"/api/coding/roles/{role['id']}/tasks")
    assert state.status_code == 200
    availability = state.json()['availability']
    assert availability['status'] == 'unavailable'
    assert '可信仓库' in availability['reason']
    assert client.get(f"/api/roles/{role['id']}").json()['role']['agentConfig']['coding']['enabled'] is False


def test_saving_one_role_purpose_keeps_unsubmitted_tool_policy(client):
    role = client.post('/api/roles', json={'name': 'purpose patch', 'profile': {'profile': 'role'},
        'agentConfig': {'shell': {'enabled': True, 'allowedCommands': ['python'], 'timeoutSeconds': 17},
            'spawn': {'timeoutSeconds': 43}, 'browserUse': {'enabled': True},
            'memoryRecall': {'enabled': True}, 'enabledTools': ['custom.query'],
            'enabledPlugins': ['custom'], 'grantedCapabilities': ['custom.read']},
    }).json()['role']
    for patch in ({'novelai': {'promptTagsEnabled': True}}, {}):
        updated = client.put(f"/api/roles/{role['id']}", json={'name': role['name'], 'profile': role['profile'], 'agentConfig': patch})
        assert updated.status_code == 200, updated.text
        config = updated.json()['role']['agentConfig']
        for key in ('shell', 'spawn', 'browserUse', 'memoryRecall', 'enabledTools', 'enabledPlugins', 'grantedCapabilities'):
            assert config[key] == role['agentConfig'][key], key
