import json

import pytest

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
