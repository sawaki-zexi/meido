import asyncio
from collections.abc import Mapping

import pytest

from backend.app.agent_runtime import (
    AgentToolResult,
    AssistantDoneEvent,
    AssistantMessage,
    CapabilityRegistry,
    CancellationToken,
    MeidoProvider,
    ToolCall,
    ToolCallEndEvent,
    ToolContext,
    ToolDefinition,
    UserMessage,
    run_agent_loop,
)
from backend.app.models import RoleInput, RoleProfile
from backend.app.agent_runtime.skills import SkillRegistry


class ReadTool:
    definition = ToolDefinition(
        name="memory.read",
        description="Read memory",
        input_schema={"type": "object"},
    )

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del arguments, context, on_update
        return AgentToolResult("ok")


def _write_skill(root, name: str, content: str):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    return directory


def test_skill_registry_discovers_and_filters_skill_tools(tmp_path):
    _write_skill(
        tmp_path,
        "study-plan",
        """---
id: study-plan
version: 1.2.0
description: 制定学习计划
tools:
  - memory.read
  - memory.write
activation: role_default
---
请先读取已有记忆，再给出分阶段学习计划。
""",
    )
    _write_skill(
        tmp_path,
        "broken",
        """---
id: broken
version: 1.0.0
activation: role_default
---
缺少 description。
""",
    )

    registry = SkillRegistry.discover(tmp_path, source="role")
    assert [skill.skill_id for skill in registry.skills] == ["study-plan"]
    assert registry.diagnostics[0]["skillId"] == "broken"
    assert registry.diagnostics[0]["source"] == "role"
    assert registry.diagnostics[0]["version"] == "1.0.0"

    active = registry.resolve(
        prompt="今天开始学习",
        enabled_tools={"memory.read"},
    )
    assert [skill.skill_id for skill in active.skills] == ["study-plan"]
    assert active.skills[0].tools == ("memory.read",)
    assert active.skills[0].content_hash
    assert active.skills[0].trust_level == "project"
    assert "学习计划" in active.prompt_sections[0]
    assert active.skills[0].path == "study-plan/SKILL.md"
    assert str(tmp_path) not in active.skills[0].path


def test_skill_discovery_rejects_unknown_sources(tmp_path):
    with pytest.raises(ValueError, match="unsupported skill source"):
        SkillRegistry.discover(tmp_path, source="untrusted")


def test_skill_activation_supports_explicit_and_keyword_modes(tmp_path):
    _write_skill(
        tmp_path,
        "explicit-only",
        """---
id: explicit-only
version: 1.0.0
description: 仅在明确调用时使用
activation: explicit
---
明确调用内容。
""",
    )
    _write_skill(
        tmp_path,
        "language",
        """---
id: language
version: 1.0.0
description: 英语学习和单词练习
activation: keyword
---
英语学习内容。
""",
    )

    registry = SkillRegistry.discover(tmp_path)
    assert registry.resolve(prompt="英语学习").skills[0].skill_id == "language"
    assert registry.resolve(prompt="普通聊天").skills == ()
    assert registry.resolve(prompt="普通聊天", explicit_ids={"explicit-only"}).skills[0].skill_id == "explicit-only"


def test_untrusted_skill_sources_require_explicit_activation(tmp_path):
    _write_skill(
        tmp_path,
        "external-help",
        """---
id: external-help
version: 1.0.0
description: 外部帮助
activation: keyword
---
外部规则。
""",
    )

    registry = SkillRegistry.discover(tmp_path, source="external")
    implicit = registry.resolve(prompt="外部帮助")

    assert implicit.skills == ()
    assert implicit.prompt_sections == ()
    assert implicit.diagnostics == ({
        "skillId": "external-help",
        "status": "denied",
        "error": "skill requires explicit activation for its trust level",
    },)

    explicit = registry.resolve(prompt="普通问题", explicit_ids={"external-help"})
    assert [skill.skill_id for skill in explicit.skills] == ["external-help"]
    assert explicit.skills[0].trust_level == "external"


def test_explicit_unknown_skill_is_reported_without_prompt_injection():
    resolution = SkillRegistry().resolve(
        prompt="普通问题",
        explicit_ids={"missing-skill"},
    )

    assert resolution.skills == ()
    assert resolution.prompt_sections == ()
    assert resolution.diagnostics == ({
        "skillId": "missing-skill",
        "status": "unknown",
        "error": "skill is not registered",
    },)


def test_skill_front_matter_accepts_inline_tool_list(tmp_path):
    _write_skill(
        tmp_path,
        "inline-tools",
        """---
id: inline-tools
version: 1.0.0
description: 内联工具列表
tools: [memory.read, memory.write]
activation: explicit
---
内联工具内容。
""",
    )

    skill = SkillRegistry.discover(tmp_path).resolve(
        prompt="",
        explicit_ids={"inline-tools"},
    ).skills[0]

    assert skill.tools == ("memory.read", "memory.write")

    _write_skill(
        tmp_path,
        "quoted-tools",
        """---
id: quoted-tools
version: 1.0.0
description: 带逗号工具名
tools: ["memory,read", memory.write]
activation: explicit
---
内容。
""",
    )
    quoted = SkillRegistry.discover(tmp_path).resolve(prompt="", explicit_ids={"quoted-tools"}).skills[0]
    assert quoted.tools == ("memory,read", "memory.write")


def test_skill_front_matter_rejects_duplicate_fields(tmp_path):
    _write_skill(
        tmp_path,
        "duplicate-fields",
        """---
id: duplicate-fields
id: overwritten
version: 1.0.0
description: 重复字段
---
内容。
""",
    )

    registry = SkillRegistry.discover(tmp_path)

    assert registry.skills == ()
    assert "duplicate front matter field: id" in registry.diagnostics[0]["error"]


def test_skill_discovery_rejects_oversized_files_before_loading_body(tmp_path):
    _write_skill(
        tmp_path,
        "oversized",
        """---
id: oversized
version: 1.0.0
description: 过大的技能
activation: explicit
---
""" + "x" * (256 * 1024),
    )

    registry = SkillRegistry.discover(tmp_path)

    assert registry.skills == ()
    assert registry.diagnostics == ({
        "path": str((tmp_path / "oversized" / "SKILL.md").resolve()),
        "skillId": "oversized",
        "source": "project",
        "version": "1.0.0",
        "error": "skill exceeds 256 KiB",
    },)


def test_skill_snapshot_and_agent_loop_use_filtered_prompt(tmp_path):
    _write_skill(
        tmp_path,
        "read-memory",
        """---
id: read-memory
version: 1.0.0
description: 使用记忆回答
tools:
  - memory.read
  - memory.write
activation: role_default
---
回答前先参考记忆。
""",
    )
    skills = SkillRegistry.discover(tmp_path)
    capabilities = CapabilityRegistry([ReadTool()], skills=skills).resolve(
        role_id="role-1",
        session_key="role:role-1",
        run_id="run-1",
        enabled_tools={"memory.read"},
        prompt_text="问题",
    )
    snapshot = capabilities.snapshot.to_dict()
    assert snapshot["skills"][0]["tools"] == ["memory.read"]
    assert snapshot["skills"][0]["trustLevel"] == "project"
    assert snapshot["promptSections"] == ["回答前先参考记忆。"]
    assert {
        item["tool"]
        for item in snapshot["skillDiagnostics"]
        if item.get("skillId") == "read-memory"
    } == {"memory.write"}

    class Provider:
        def __init__(self):
            self.systems = []

        def stream_response(self, *, model, system, messages, tools, signal, session_id=None):
            del model, messages, tools, signal, session_id
            self.systems.append(system)
            call = ToolCall("call-1", "memory.read", {})

            async def stream():
                yield ToolCallEndEvent(call)
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason="tool_use"))

            return stream()

    provider = Provider()

    async def collect():
        return [
            event
            async for event in run_agent_loop(
                provider=provider,
                model="fake",
                system=capabilities.prompt_context(),
                messages=[UserMessage("问题")],
                capabilities=capabilities,
                max_turns=1,
            )
        ]

    asyncio.run(collect())
    assert "回答前先参考记忆。" in provider.systems[0]


def test_meido_provider_appends_skill_prompt_without_dropping_role_prompt():
    class Adapter:
        def __init__(self):
            self.memory_contexts = []

        async def stream_reply(self, role, history, configuration=None, *, memory_context=""):
            del role, history, configuration
            self.memory_contexts.append(memory_context)
            yield "完成"

    adapter = Adapter()
    role = RoleInput(name="技能角色", profile=RoleProfile(profile="核心设定"))
    provider = MeidoProvider(adapter, role, None, capability_prompt="Skill 规则")

    async def collect():
        return [
            event
            async for event in provider.stream_response(
                model="fake",
                system="",
                messages=[UserMessage("问题")],
                tools=[],
                signal=CancellationToken(),
            )
        ]

    asyncio.run(collect())
    assert adapter.memory_contexts == ["Skill 规则"]


def test_meido_provider_appends_skill_prompt_to_explicit_structured_system():
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            del configuration, tools
            assert messages[0]["content"] == "调用方系统\n\nSkill 规则"
            yield ModelTextDelta("完成")

    from backend.app.model_adapter import ModelTextDelta

    provider = MeidoProvider(
        Adapter(),
        RoleInput(name="结构化角色", profile=RoleProfile(profile="核心设定")),
        None,
        capability_prompt="Skill 规则",
    )

    async def collect():
        return [
            event
            async for event in provider.stream_response(
                model="fake",
                system="调用方系统",
                messages=[UserMessage("问题")],
                tools=[{"type": "function", "function": {"name": "tool"}}],
                signal=CancellationToken(),
            )
        ]

    asyncio.run(collect())


def test_meido_provider_injects_skill_prompt_for_legacy_adapter():
    class LegacyAdapter:
        def __init__(self):
            self.profiles = []

        async def stream_reply(self, role, history, configuration=None):
            del history, configuration
            self.profiles.append(role.profile.profile)
            yield "完成"

    adapter = LegacyAdapter()
    role = RoleInput(name="旧适配器角色", profile=RoleProfile(profile="核心设定"))
    provider = MeidoProvider(adapter, role, None, capability_prompt="Skill 规则")

    async def collect():
        return [
            event
            async for event in provider.stream_response(
                model="fake",
                system="",
                messages=[UserMessage("问题")],
                tools=[],
                signal=CancellationToken(),
            )
        ]

    asyncio.run(collect())
    assert adapter.profiles == ["核心设定\n\nSkill 规则"]
    assert role.profile.profile == "核心设定"
