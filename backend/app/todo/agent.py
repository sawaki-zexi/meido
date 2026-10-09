from __future__ import annotations

from collections.abc import Mapping

from ..agent_runtime.plugins import PluginContext, PluginContribution, PluginManifest
from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition
from .store import TodoStore


class TodoReadPort:
    """Narrow capability adapter; agent plugins never receive the SQLite store."""

    def __init__(self, store: TodoStore) -> None:
        self._store = store

    def search(self, query: str, limit: int = 5) -> list[dict[str, object]]:
        return self._store.read_port(query, limit=limit)


class TodoSearchTool:
    definition = ToolDefinition(
        name="todo.search",
        description="Search the owner's active todo list. This tool is read-only.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk="read_only",
        source="plugin:todo",
        exposure="model_only",
        output_limit=4000,
    )

    def __init__(self, port: TodoReadPort) -> None:
        self._port = port

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del context, on_update
        items = self._port.search(str(arguments["query"]), int(arguments.get("limit", 5)))
        safe_items = [
            {key: item[key] for key in ("id", "title", "description", "status", "dueDate", "dueAt", "reminderDate", "reminderAt")}
            for item in items
        ]
        return AgentToolResult("当前清单中没有相关待办。" if not safe_items else str(safe_items), details={"count": len(safe_items)})


TODO_PLUGIN_MANIFEST = PluginManifest(
    plugin_id="todo",
    version="1.0.0",
    requested_capabilities=("todo.read",),
    source="builtin",
    trust_level="builtin",
    declared_tools=("todo.search",),
)


def todo_plugin_factory(context: PluginContext) -> PluginContribution:
    port = context.require("todo.read")
    if not isinstance(port, TodoReadPort):
        raise TypeError("todo.read host service has an invalid type")
    return PluginContribution(tools=(TodoSearchTool(port),))


def register_todo_agent_plugin(registry) -> None:
    registry.register(TODO_PLUGIN_MANIFEST, todo_plugin_factory)
