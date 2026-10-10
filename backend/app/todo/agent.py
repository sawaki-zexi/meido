from __future__ import annotations

from collections.abc import Mapping
import json

from ..agent_runtime.plugins import PluginContext, PluginContribution, PluginManifest
from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition
from .application import TodoApplicationService


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

    def __init__(self, application: TodoApplicationService) -> None:
        self._application = application

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del context, on_update
        items = self._application.search(str(arguments["query"]), int(arguments.get("limit", 5)))
        safe_items = [
            {key: item[key] for key in ("id", "title", "description", "status", "dueDate", "dueAt", "reminderDate", "reminderAt")}
            for item in items
        ]
        return AgentToolResult("当前清单中没有相关待办。" if not safe_items else str(safe_items), details={"count": len(safe_items)})


class TodoWriteTool:
    definition = ToolDefinition(
        name="todo.write",
        description=(
            "Manage the owner's todo list only when the owner clearly instructs you to create, change, "
            "complete, cancel, or restore an item. Do not call this tool for ordinary sharing, ideas, "
            "hypothetical or conditional statements, quoted text, or requests for advice. For non-create "
            "operations, identify the item with todoId or its exact targetTitle. Ask for clarification "
            "when the title matches more than one item."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["create", "update", "complete", "cancel", "restore"]},
                "todoId": {"type": "string", "minLength": 1},
                "targetTitle": {"type": "string", "minLength": 1, "maxLength": 200},
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "description": {"type": "string", "maxLength": 2000},
                "dueDate": {"type": ["string", "null"], "format": "date"},
                "dueAt": {"type": ["string", "null"], "format": "date-time"},
                "reminderAt": {"type": ["string", "null"], "format": "date-time"},
                "reminderDate": {"type": ["string", "null"], "format": "date"},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
        risk="mutating",
        source="plugin:todo",
        exposure="model_only",
        output_limit=3000,
    )

    def __init__(self, application: TodoApplicationService) -> None:
        self._application = application

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        operation = arguments.get("operation")
        try:
            result = self._application.write(
                str(operation),
                arguments,
                role_id=context.role_id,
                session_key=context.session_key,
                run_id=context.run_id,
                tool_call_id=context.tool_call_id,
            )
        except (ValueError, KeyError) as error:
            return AgentToolResult(str(error), is_error=True)
        return AgentToolResult(json.dumps(result, ensure_ascii=False), details={"todoId": result["id"]})


class TodoReminderControlTool:
    definition = ToolDefinition(
        name="todo.reminder_control",
        description=(
            "Manage reminder participation for the current role only. Join or leave starting tomorrow "
            "only when the owner clearly asks you to take or stop this responsibility; do not act on "
            "ordinary sharing, hypotheticals, quoted text, or advice questions. Pause or resume all "
            "reminders for today only when the owner clearly asks for that change."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["join", "leave", "pause_today", "resume_today"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk="mutating",
        source="plugin:todo",
        exposure="model_only",
        output_limit=500,
    )

    def __init__(self, application: TodoApplicationService) -> None:
        self._application = application

    def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        try:
            result = self._application.control_reminders(
                str(arguments.get("action", "")),
                role_id=context.role_id,
                session_key=context.session_key,
                run_id=context.run_id,
            )
        except (ValueError, KeyError) as error:
            return AgentToolResult(str(error), is_error=True)
        return AgentToolResult(json.dumps(result, ensure_ascii=False), details=result)


TODO_PLUGIN_MANIFEST = PluginManifest(
    plugin_id="todo",
    version="1.0.0",
    requested_capabilities=("todo.write",),
    source="builtin",
    trust_level="builtin",
    declared_tools=("todo.search", "todo.write", "todo.reminder_control"),
)


def todo_plugin_factory(context: PluginContext) -> PluginContribution:
    application = context.require("todo.write")
    if not isinstance(application, TodoApplicationService):
        raise TypeError("todo.write host service has an invalid type")
    return PluginContribution(tools=(TodoSearchTool(application), TodoWriteTool(application), TodoReminderControlTool(application)))


def register_todo_agent_plugin(registry) -> None:
    registry.register(TODO_PLUGIN_MANIFEST, todo_plugin_factory)
