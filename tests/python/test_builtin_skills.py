import json
import os
import subprocess

from backend.app.agent_runtime.skills import SkillRegistry


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
    diagnostics = [dict(item) for item in result.diagnostics]
    assert diagnostics == [{
        "skillId": "needs-cli", "status": "unavailable", "error": "skill dependencies are unavailable",
        "missingBins": ["meido-missing-command"], "missingEnv": ["MEIDO_SKILL_TEST_SECRET"],
    }]
    assert "meido-missing-command" in "\n".join(result.prompt_sections)
    assert "private-value-never-in-context" not in json.dumps(diagnostics) + "\n".join(result.prompt_sections)


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
    if os.name == "nt":
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path $env:MEIDO_TEST_SKILL_LINK -Target $env:MEIDO_TEST_SKILL_TARGET | Out-Null"],
            env=dict(os.environ, MEIDO_TEST_SKILL_LINK=str(root), MEIDO_TEST_SKILL_TARGET=str(target)),
            check=True, capture_output=True, timeout=10)
    else:
        os.symlink(target, root, target_is_directory=True)
    registry = SkillRegistry.discover(root, source="role")
    result = registry.resolve(prompt="", explicit_ids={"private"})
    assert not result.skills
    assert any("skill discovery root" in item.get("error", "") for item in result.diagnostics)
