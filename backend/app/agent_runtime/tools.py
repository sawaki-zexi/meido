from __future__ import annotations

from copy import deepcopy
import inspect
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .provider import CancellationToken

_RISKS = frozenset({"read_only", "mutating", "external"})
_EXPOSURES = frozenset({"direct", "model_only", "deferred", "hidden"})
_APPROVALS = frozenset({"auto", "prompt", "writes", "deny"})


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)
    risk: str = "read_only"
    source: str = "builtin"
    version: str = "1.0.0"
    exposure: str = "direct"
    approval: str = "auto"
    timeout_seconds: float | None = None
    output_limit: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name is required")
        if not self.description.strip():
            raise ValueError(f"tool description is required: {self.name}")
        if self.risk not in _RISKS:
            raise ValueError(f"unsupported tool risk: {self.risk}")
        if self.exposure not in _EXPOSURES:
            raise ValueError(f"unsupported tool exposure: {self.exposure}")
        if self.approval not in _APPROVALS:
            raise ValueError(f"unsupported tool approval: {self.approval}")
        if not self.version.strip():
            raise ValueError(f"tool version is required: {self.name}")
        if self.timeout_seconds is not None:
            if (
                isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0
            ):
                raise ValueError(f"tool timeout must be positive and finite: {self.name}")
        if self.output_limit is not None:
            if isinstance(self.output_limit, bool) or not isinstance(self.output_limit, int) or self.output_limit <= 0:
                raise ValueError(f"tool output limit must be positive integer: {self.name}")

    def as_provider_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": deepcopy(self.input_schema),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolContext:
    role_id: str
    session_key: str
    run_id: str
    signal: CancellationToken


@dataclass(frozen=True, slots=True)
class AgentToolResult:
    content: str
    details: object | None = None
    is_error: bool = False


class AgentTool(Protocol):
    definition: ToolDefinition

    def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
        on_update: Callable[[AgentToolResult], None] | None = None,
    ) -> AgentToolResult | Awaitable[AgentToolResult]: ...


class ToolRegistry:
    def __init__(self, tools: Sequence[AgentTool] = ()) -> None:
        self._tools: dict[str, AgentTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentTool) -> None:
        if tool.definition.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.definition.name}")
        self._tools[tool.definition.name] = tool

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def provider_schemas(self) -> list[dict[str, object]]:
        return [definition.as_provider_schema() for definition in self.definitions()]

    def validate_arguments(self, name: str, arguments: Mapping[str, object]) -> None:
        """Validate the bounded JSON-schema subset used by runtime tools."""

        tool = self.get(name)
        if tool is None:
            raise ValueError(f"unknown tool: {name}")
        schema = tool.definition.input_schema
        if schema.get("type") == "object":
            _validate_object_value("", arguments, schema)
        else:
            _validate_schema_value("arguments", arguments, schema)


def _validate_schema_value(path: str, value: object, schema: Mapping[str, object]) -> None:
    expected = schema.get("type")
    if expected == "string":
        if not isinstance(value, str):
            raise ValueError(f"argument {path} must be a string")
        _validate_bound(path, len(value), schema, "minLength", "maximum length")
        _validate_bound(path, len(value), schema, "maxLength", "maximum length", upper=True)
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"argument {path} must be an integer")
        _validate_number_bounds(path, value, schema)
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"argument {path} must be a number")
        _validate_number_bounds(path, value, schema)
    elif expected == "object":
        _validate_object_value(path, value, schema)
    elif expected == "array":
        if not isinstance(value, list):
            raise ValueError(f"argument {path} must be an array")
        _validate_bound(path, len(value), schema, "minItems", "minimum item count")
        _validate_bound(path, len(value), schema, "maxItems", "maximum item count", upper=True)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(f"{path}[{index}]", item, item_schema)
    elif expected == "boolean" and not isinstance(value, bool):
        raise ValueError(f"argument {path} must be a boolean")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ValueError(f"argument {path} has an unsupported value")


def _validate_object_value(path: str, value: object, schema: Mapping[str, object]) -> None:
    if not isinstance(value, dict):
        label = "arguments" if not path else f"argument {path}"
        raise ValueError(f"{label} must be an object")
    required = schema.get("required", [])
    if isinstance(required, list):
        missing = [key for key in required if isinstance(key, str) and key not in value]
        if missing:
            prefix = f" at {path}" if path else ""
            raise ValueError(f"missing required arguments{prefix}: {', '.join(missing)}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return
    if schema.get("additionalProperties") is False:
        unknown = [key for key in value if key not in properties]
        if unknown:
            prefix = f" at {path}" if path else ""
            raise ValueError(f"unknown arguments{prefix}: {', '.join(str(key) for key in unknown)}")
    for key, item in value.items():
        definition = properties.get(key)
        if not isinstance(definition, dict):
            continue
        item_path = f"{path}.{key}" if path else str(key)
        _validate_schema_value(item_path, item, definition)


def _validate_number_bounds(path: str, value: int | float, schema: Mapping[str, object]) -> None:
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
        raise ValueError(f"argument {path} is below the minimum")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
        raise ValueError(f"argument {path} exceeds the maximum")


def _validate_bound(
    path: str,
    value: int,
    schema: Mapping[str, object],
    key: str,
    description: str,
    *,
    upper: bool = False,
) -> None:
    bound = schema.get(key)
    if not isinstance(bound, int) or isinstance(bound, bool):
        return
    if (upper and value > bound) or (not upper and value < bound):
        raise ValueError(f"argument {path} exceeds the {description}" if upper else f"argument {path} is below the {description}")


async def resolve_tool_result(value: AgentToolResult | Awaitable[AgentToolResult]) -> AgentToolResult:
    if inspect.isawaitable(value):
        return await value
    return value
