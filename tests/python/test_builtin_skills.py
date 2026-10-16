import json
import os
import subprocess

import pytest

from backend.app.agent_runtime import CapabilityRegistry
from backend.app.agent_runtime.skills import SkillDescriptor, SkillRegistry


def write_skill(root, name, header="", body="技能正文"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nid: {name}\nversion: 1.0.0\ndescription: 测试技能\nactivation: explicit\n{header}---\n{body}",
        encoding="utf-8",
    )
    return directory


def test_missing_skill_dependencies_explain_unavailability_without_exposing_host_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MEIDO_SKILL_TEST_SECRET", "private-value-never-in-context")
    write_skill(tmp_path, "needs-cli", "requires.bins: [meido-missing-command]\nrequires.env: [MEIDO_SKILL_TEST_SECRET]\n")
    registry = SkillRegistry.discover(tmp_path)

    result = registry.resolve(prompt="", explicit_ids={"needs-cli"})

    assert not result.skills
    diagnostics = json.loads(json.dumps([dict(item) for item in result.diagnostics]))
    assert diagnostics == [{
        "skillId": "needs-cli", "status": "unavailable", "error": "skill dependencies are unavailable",
        "missingBins": ["meido-missing-command"], "missingEnv": ["MEIDO_SKILL_TEST_SECRET"],
    }]
    assert "meido-missing-command" in "\n".join(result.prompt_sections)
    assert "private-value-never-in-context" not in json.dumps(diagnostics) + "\n".join(result.prompt_sections)


def test_fixed_codex_profile_dependency_can_qualify_without_path_and_denial_is_specific(monkeypatch):
    from pathlib import Path
    registry = SkillRegistry.discover(Path(__file__).resolve().parents[2] / 'backend/app/builtin_skills', source='builtin')
    monkeypatch.setattr('shutil.which', lambda _: None)
    assert not registry.resolve(prompt='write code').skills
    denied = registry.resolve(prompt='$codex-delegate', dependency_check=lambda skill, name: '专用 profile 尚未核验')
    assert not denied.skills
    assert '专用 profile 尚未核验' in str(denied.diagnostics)
    selected = registry.resolve(prompt='$codex-delegate', dependency_check=lambda skill, name: None)
    assert selected.skills[0].skill_id == 'codex-delegate'
    assert selected.skills[0].tools == ('spawn', 'spawn_manage')


def test_skill_script_resource_is_verified_and_never_automatically_executed(tmp_path):
    directory = write_skill(tmp_path, "with-script")
    scripts = directory / "scripts"
    scripts.mkdir()
    script = scripts / "helper.py"
    script.write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    registry = SkillRegistry.discover(tmp_path)
    selected = registry.resolve(prompt="", explicit_ids={"with-script"})
    assert [item.path for item in selected.skills[0].supporting_files] == ["with-script/scripts/helper.py"]
    script.write_text("changed after discovery", encoding="utf-8")
    rejected = registry.resolve(prompt="", explicit_ids={"with-script"})
    assert not rejected.skills
    assert rejected.diagnostics[-1]["error"] == "skill supporting file changed after discovery"


def test_role_skill_overrides_builtin_without_a_duplicate_error(tmp_path):
    write_skill(tmp_path / "builtin", "same-name", body="builtin content")
    write_skill(tmp_path / "role", "same-name", body="role content")
    registry = SkillRegistry.discover_many(((tmp_path / "builtin", "builtin"), (tmp_path / "role", "role")))
    selected = registry.resolve(prompt="", explicit_ids={"same-name"})
    assert [(item.source, item.body) for item in selected.skills] == [("role", "role content")]
    assert not any("error" in item for item in selected.diagnostics)
    assert any(item.get("status") == "shadowed" and item["source"] == "builtin" for item in selected.diagnostics)


def test_linked_skill_root_cannot_import_another_roles_skill(tmp_path):
    write_skill(tmp_path / "other-role" / "skills", "private", body="another role's private instructions")
    root = tmp_path / "skills"
    target = tmp_path / "other-role" / "skills"
    directory_link(root, target)
    registry = SkillRegistry.discover(root, source="role")
    result = registry.resolve(prompt="", explicit_ids={"private"})
    assert not result.skills
    assert any("skill discovery root" in item.get("error", "") for item in result.diagnostics)


def directory_link(root, target):
    if os.name == "nt":
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path $env:MEIDO_TEST_SKILL_LINK -Target $env:MEIDO_TEST_SKILL_TARGET | Out-Null"],
            env=dict(os.environ, MEIDO_TEST_SKILL_LINK=str(root), MEIDO_TEST_SKILL_TARGET=str(target)),
            check=True, capture_output=True, timeout=10)
    else:
        os.symlink(target, root, target_is_directory=True)


@pytest.mark.parametrize("timing", ["before-discovery", "after-discovery"])
@pytest.mark.parametrize("ancestor", ["role", "workspace"])
def test_skill_ancestor_links_are_rejected_at_discovery_and_activation(tmp_path, timing, ancestor):
    source = tmp_path / "role-a" / "workspace" / "skills"
    destination = tmp_path / "role-b" / "workspace" / "skills"
    write_skill(destination, "private", body="same bytes must not authorize a different role path")
    link = tmp_path / "role-a" if ancestor == "role" else tmp_path / "role-a" / "workspace"
    target = tmp_path / "role-b" if ancestor == "role" else tmp_path / "role-b" / "workspace"
    link.parent.mkdir(parents=True, exist_ok=True)
    if timing == "after-discovery":
        write_skill(source, "private", body="same bytes must not authorize a different role path")
        registry = SkillRegistry.discover(source, source="role")
        retired = link.with_name(link.name + "-retired")
        assert link.is_relative_to(tmp_path) and retired.is_relative_to(tmp_path)
        link.rename(retired)
    directory_link(link, target)
    if timing == "before-discovery":
        registry = SkillRegistry.discover(source, source="role")
    result = registry.resolve(prompt="$private")
    assert not result.skills
    assert result.diagnostics


def descriptor(**overrides):
    values = dict(skill_id="injected", version="1.0.0", description="host contribution", tools=(),
        activation="explicit", source="project", path="injected/SKILL.md", content_hash="host-version-1", body="instructions")
    return SkillDescriptor(**(values | overrides))


def test_contributed_skill_dependencies_cannot_mutate_an_issued_snapshot():
    commands, environment = [], []
    registry = CapabilityRegistry(skills=SkillRegistry([descriptor(required_bins=commands, required_env=environment)]))
    snapshot = registry.resolve(role_id="role-a", session_key="role:role-a", run_id="run-a", explicit_skill_ids={"injected"}).snapshot
    commands.append("late-command")
    environment.append("LATE_ENV")
    assert snapshot.to_dict()["skills"][0]["requires"] == {"bins": [], "env": []}


@pytest.mark.parametrize("dependencies", [
    {"required_bins": ("../unapproved",)}, {"required_env": ("API_KEY=sentinel-value",)},
])
def test_contributed_skill_rejects_dependencies_containing_paths_or_values(dependencies):
    with pytest.raises(ValueError, match="plain command or environment names"):
        descriptor(**dependencies)


def test_dependency_diagnostics_are_immutable_and_export_fresh_arrays(tmp_path):
    write_skill(tmp_path, "missing", "requires.bins: [meido-missing-command]\n")
    snapshot = CapabilityRegistry(skills=SkillRegistry.discover(tmp_path)).resolve(
        role_id="role-a", session_key="role:role-a", run_id="run-a", explicit_skill_ids={"missing"}).snapshot
    exported = snapshot.to_dict()
    exported["skillDiagnostics"][0]["missingBins"].clear()
    assert snapshot.to_dict()["skillDiagnostics"][0]["missingBins"] == ["meido-missing-command"]
    with pytest.raises((TypeError, AttributeError)):
        snapshot.skill_diagnostics[0]["missingBins"].append("injected")
