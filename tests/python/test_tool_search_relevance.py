"""Tool discovery behavior through the public execution and snapshot seams."""
import asyncio
import json
import pytest
from dataclasses import replace

from backend.app.agent_runtime import (
    AgentToolResult, CancellationToken, CapabilityRegistry, ToolContext, ToolDefinition,
)
from backend.app.channels.group_tools import LookupGroupContextTool, LookupGroupMemberTool
from backend.app.browser_use.tool import BrowserTool, load_tool_specs
from backend.app.coding_tasks.tools import CodingTool
from backend.app.schedules.tools import ScheduleTool
from backend.app.desktop_push import MessagePushTool
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


class CatalogTool:
    def __init__(self, definition):
        self.definition = definition

    async def execute(self, arguments, context, on_update=None):
        return AgentToolResult("catalog fixture")


def search(tools, query, **arguments):
    capabilities = CapabilityRegistry(tools).resolve(
        role_id="r", session_key="role:r", run_id="search",
        enabled_tools={"tool_search", *(tool.definition.name for tool in tools)},
        discovery_enabled=True,
        deferred_tool_names={tool.definition.name for tool in tools},
    )
    context = ToolContext("r", "role:r", "search", CancellationToken(), "find")
    return json.loads(asyncio.run(capabilities.get("tool_search").execute(
        {"query": query, **arguments}, context,
    )).content)


def test_natural_chinese_query_finds_actual_group_notes_definition():
    result = search([CatalogTool(LookupGroupContextTool.definition)], "查群笔记")
    assert result["unlocked"] == ["lookup_group_context"]


def test_group_member_is_discoverable_by_registered_hint_absent_from_description():
    assert "是谁" not in LookupGroupMemberTool.definition.description
    result = search([CatalogTool(LookupGroupMemberTool.definition)], "是谁")
    assert result["unlocked"] == ["lookup_group_member"]


@pytest.mark.parametrize(("query", "definitions", "expected"), [
    ("read", [
        ToolDefinition("a_bread", "opaque"),
        ToolDefinition("z_read", "opaque"),
        ToolDefinition("m_read", "opaque", source="mcp:catalog"),
    ], ["m_read", "z_read", "a_bread"]),
    ("meteor", [
        ToolDefinition("a_catalog", "meteor"),
        ToolDefinition("z_catalog", "opaque", search_hint="meteor"),
    ], ["z_catalog", "a_catalog"]),
    ("reader", [ToolDefinition("lookup_read", "opaque")], ["lookup_read"]),
    ("RSS订阅管理", [ToolDefinition("rss_feed", "管理订阅")], ["rss_feed"]),
])
def test_keywords_rank_name_parts_mcp_hints_and_mixed_text(query, definitions, expected):
    result = search([CatalogTool(item) for item in definitions], query)
    assert result["unlocked"] == expected
    assert all(item["why_matched"] for item in result["matched"])
    assert all(item["summary"] == definition.description[:120]
               for item, definition in zip(result["matched"],
                   [next(value for value in definitions if value.name == name) for name in expected]))
    assert all(item["always_on"] is False for item in result["matched"])


def test_plain_exact_name_wins_over_keyword_scores_and_ties_use_stable_top_k():
    definitions = [
        ToolDefinition("read_log", "opaque"),
        ToolDefinition("mcp_read_log_more", "read_log", source="mcp:catalog", search_hint="read_log"),
    ]
    exact = search([CatalogTool(item) for item in definitions], "read_log", top_k=10)
    assert exact["unlocked"] == ["read_log"]
    assert exact["matched"][0]["why_matched"] == ["名称:精确匹配"]
    ties = [CatalogTool(ToolDefinition(name, "opaque")) for name in ("z_read", "b_read", "a_read")]
    assert search(ties, "read", top_k=2)["unlocked"] == ["a_read", "b_read"]


def test_search_hint_uses_current_metadata_and_history_restores_names():
    from backend.app.agent_runtime.types import ToolResultMessage
    definition = ToolDefinition("archive_lookup", "opaque", search_hint="meteor")
    tool = CatalogTool(definition)
    registry = CapabilityRegistry([tool])
    options = dict(role_id="r", session_key="role:r", enabled_tools={"tool_search", definition.name},
                   discovery_enabled=True, deferred_tool_names=frozenset({definition.name}))
    original = registry.resolve(run_id="old", activated_tools={definition.name}, **options)
    saved = original.call_record(definition.name)
    assert saved['tools'][0]['searchHint'] == 'meteor'
    assert 'searchHint' not in json.dumps(original.provider_schemas())
    current = registry.resolve(run_id='current', **options)
    tool.definition = replace(definition, search_hint='comet')
    context = ToolContext('r', 'role:r', 'current', CancellationToken(), 'find')
    result = json.loads(asyncio.run(current.get('tool_search').execute({'query': 'comet'}, context)).content)
    assert result['unlocked'] == [definition.name]
    assert saved['tools'][0]['searchHint'] == 'meteor'
    restored = registry.resolve(run_id='restored', **options).select_window([
        ToolResultMessage('find', 'tool_search', '{}', unlocked_tools=(definition.name,))])
    assert restored.definition(definition.name).search_hint == 'comet'


def test_long_query_keeps_useful_top_k_with_bounded_valid_json_explanations():
    tools = [CatalogTool(ToolDefinition(f"tool_read_{index}", "read", search_hint="read")) for index in range(10)]
    result = search(tools, "x" * 1800 + " read", top_k=10)
    assert len(result["unlocked"]) == 10
    assert len(json.dumps(result, ensure_ascii=False)) <= 16000
    assert all(item.get("explanation_truncated") is True for item in result["matched"])


def test_production_browser_coding_and_schedule_hints_are_searchable():
    class BrowserOwner:
        timeout_seconds = 30

    browser = BrowserTool(BrowserOwner(), load_tool_specs()[0])
    coding = CodingTool("herdr_start_task", lambda: None, role_id="r", origin_resolver=lambda context: None,
                        authorized=lambda context: True, access={})
    for tool, query in ((browser, "填表"), (coding, "修bug"), (CatalogTool(ScheduleTool.definition), "延时执行")):
        result = search([tool], query)
        assert result["unlocked"] == [tool.definition.name]
        assert any(reason.startswith("提示:") for reason in result["matched"][0]["why_matched"])


def test_guest_never_sees_discovers_or_executes_desktop_push(tmp_path):
    from backend.app.agent_runtime import AssistantDoneEvent, AssistantMessage, ToolCall, run_agent_loop

    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    tool = MessagePushTool(sessions, lambda: tmp_path, role_id="r", authorized=lambda context: False)
    guest = CapabilityRegistry([tool]).resolve(
        role_id="r", session_key="thread:guest", run_id="guest",
        enabled_tools={"message_push", "tool_search"}, external_only=True, discovery_enabled=True,
    )
    assert {item["function"]["name"] for item in guest.provider_schemas()} == {"tool_search"}
    context = ToolContext("r", "thread:guest", "guest", CancellationToken(), "find")
    for query in ("桌面", "select:message_push", "message_push"):
        result = json.loads(asyncio.run(guest.get("tool_search").execute({"query": query}, context)).content)
        assert result["matched"] == result["unlocked"] == result["already_loaded"] == []

    class Provider:
        async def stream_response(self, messages, **kwargs):
            if messages:
                assert messages[-1].is_error
                yield AssistantDoneEvent(AssistantMessage(content="denied"))
            else:
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall("forged", "message_push", {
                    "channel": "desktop", "chat_id": "owner", "message": "guest must not write",
                }),)))

    async def execute():
        return [event async for event in run_agent_loop(provider=Provider(), model="fixture", system="",
            messages=[], capabilities=guest, max_turns=2, role_id="r", session_key="thread:guest", run_id="guest")]

    assert asyncio.run(execute())[-1].reason == "completed"
    assert sessions.list_messages("role:r") == []
