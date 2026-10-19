import pytest

from backend.app.advanced_settings import AdvancedSettingsStore, SettingsConflict


def test_partial_updates_restart_and_idempotent_replay(tmp_path):
    path = tmp_path / 'advanced.db'
    store = AdvancedSettingsStore(path)
    first = store.get()
    assert first['values']['maxIterations'] == 40
    saved = store.update(expected_revision=0, operation_id='first', values={'maxTokens': 4096})
    assert saved['revision'] == 1
    assert saved['values']['maxIterations'] == 40
    restarted = AdvancedSettingsStore(path)
    assert restarted.get() == saved
    assert restarted.update(expected_revision=0, operation_id='first', values={'maxTokens': 4096}) == saved
    with pytest.raises(SettingsConflict):
        restarted.update(expected_revision=1, operation_id='first', values={'maxTokens': 8000})
    with pytest.raises(SettingsConflict):
        restarted.update(expected_revision=0, operation_id='other', values={'maxTokens': 8000})


def test_invalid_values_do_not_commit_and_spawn_epoch_never_resurrects(tmp_path):
    store = AdvancedSettingsStore(tmp_path / 'advanced.db')
    for values in ({'maxTokens': True}, {'maxIterations': 0}, {'contextTargetRatio': .9}, {'unknown': 1}):
        with pytest.raises(ValueError):
            store.update(expected_revision=0, operation_id='invalid', values=values)
        assert store.get()['revision'] == 0
    off = store.update(expected_revision=0, operation_id='off', values={'spawnEnabled': False})
    on = store.update(expected_revision=1, operation_id='on', values={'spawnEnabled': True})
    assert off['spawnGeneration'] == on['spawnGeneration'] == 1


@pytest.mark.parametrize('external', [False, True])
def test_direct_exposure_activates_deferred_tools_without_exposing_hidden_or_external_denied_tools(external):
    from backend.app.agent_runtime.capabilities import CapabilityRegistry
    from backend.app.agent_runtime.tools import ToolDefinition
    class Tool:
        def __init__(self, name, exposure):
            self.definition = ToolDefinition(name, name, {'type': 'object'}, exposure=exposure)
    resolution = CapabilityRegistry([Tool('delayed', 'deferred'), Tool('hidden', 'hidden')]).resolve(
        role_id='r', session_key='role:r', run_id='run', discovery_enabled=True, direct_exposure=True,
        enabled_tools={'delayed', 'hidden'}, external_only=external)
    assert {tool.name for tool in resolution.snapshot.tools} == (set() if external else {'delayed'})
    assert not any(tool.name == 'tool_search' for tool in resolution.snapshot.candidates)
