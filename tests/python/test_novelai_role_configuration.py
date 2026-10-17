"""New role policy persists and survives clients omitting its configuration."""
from backend.app.models import AgentToolConfig, RoleInput, RoleProfile, RoleUpdateInput
from backend.app.role_store import RoleStore


def test_novelai_defaults_off_and_role_store_preserves_omitted_policy(tmp_path):
    assert AgentToolConfig().novelai.manualEnabled is False
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="Fixture", profile=RoleProfile(profile="Local test role"),
        agentConfig={"novelai": {"manualEnabled": True, "consentRevision": 1}}))
    reloaded = RoleStore(roles.root).get(role.id)
    assert reloaded.agentConfig.novelai.manualEnabled is True
    updated = roles.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
        agentConfig={"enabledTools": ["search_messages"]}))
    assert updated.agentConfig.novelai.manualEnabled is True
    assert updated.agentConfig.novelai.consentRevision == 1
    assert updated.agentConfig.enabledTools == ["search_messages"]
