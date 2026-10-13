"""Shared helpers for provider-specific model protocol adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
import json
import re
import hashlib

from .model_types import ModelCost, ModelImageBlock, ModelTextBlock


OPENAI_COMPLETIONS_API = "openai-completions"
OPENAI_RESPONSES_API = "openai-responses"
ANTHROPIC_MESSAGES_API = "anthropic-messages"
GOOGLE_GENERATIVE_API = "google-generative"
MISTRAL_CONVERSATIONS_API = "mistral-conversations"
BEDROCK_CONVERSE_API = "bedrock-converse"
PI_MESSAGES_API = "pi-messages"
AZURE_OPENAI_RESPONSES_API = "azure-openai-responses"
OPENAI_CODEX_RESPONSES_API = "openai-codex-responses"
GOOGLE_VERTEX_API = "google-vertex"


def copy_json(value: object) -> object:
    """Copy read-only mappings into values accepted by JSON encoders."""
    if isinstance(value, Mapping):
        return {str(key): copy_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [copy_json(item) for item in value]
    return copy.deepcopy(value)


def model_cost(value: object) -> ModelCost:
    """Parse optional provider cost fields without making usage parsing strict."""
    if not isinstance(value, Mapping):
        return ModelCost()
    raw = value.get("cost") if isinstance(value.get("cost"), Mapping) else value

    def number(*keys: str) -> float:
        for key in keys:
            candidate = raw.get(key) if isinstance(raw, Mapping) else None
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate >= 0:
                return float(candidate)
        return 0.0

    return ModelCost(
        input=number("input", "input_cost", "prompt"),
        output=number("output", "output_cost", "completion"),
        cache_read=number("cacheRead", "cache_read", "cache_read_cost"),
        cache_write=number("cacheWrite", "cache_write", "cache_write_cost"),
        total=number("total", "total_cost"),
    )


def tool_name(tool: Mapping[str, object]) -> str:
    function = tool.get("function")
    candidate = function if isinstance(function, Mapping) else tool
    name = candidate.get("name") if isinstance(candidate, Mapping) else None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("工具缺少有效名称")
    return name.strip()


def tool_description(tool: Mapping[str, object]) -> str:
    function = tool.get("function")
    candidate = function if isinstance(function, Mapping) else tool
    value = candidate.get("description") if isinstance(candidate, Mapping) else None
    return value.strip() if isinstance(value, str) else ""


def tool_parameters(tool: Mapping[str, object]) -> dict[str, object]:
    function = tool.get("function")
    candidate = function if isinstance(function, Mapping) else tool
    parameters = candidate.get("parameters") if isinstance(candidate, Mapping) else None
    if parameters is None and isinstance(candidate, Mapping):
        parameters = candidate.get("input_schema", candidate.get("inputSchema"))
    if parameters is None:
        parameters = tool.get("input_schema", tool.get("inputSchema"))
    if parameters is None or parameters == {}:
        return {"type": "object", "properties": {}}
    if not isinstance(parameters, Mapping):
        raise ValueError(f"工具参数 schema 必须是对象: {tool_name(tool)}")
    result = copy_json(parameters)
    if not isinstance(result, dict):
        raise ValueError(f"工具参数 schema 必须是对象: {tool_name(tool)}")
    if result.get("type") not in (None, "object"):
        raise ValueError(f"工具参数 schema 顶层必须是 object: {tool_name(tool)}")
    result["type"] = "object"
    properties = result.get("properties")
    if properties is None:
        result["properties"] = {}
    elif not isinstance(properties, dict):
        raise ValueError(f"工具参数 schema.properties 必须是对象: {tool_name(tool)}")
    _validate_schema(result, tool_name(tool))
    return result


def _validate_schema(schema: object, name: str) -> None:
    """Check known schema nodes, leaving annotations and extensions untouched."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise ValueError(f"工具参数 schema 子节点必须是对象或布尔值: {name}")
    schema_type = schema.get("type")
    valid_types = {"array", "boolean", "integer", "null", "number", "object", "string"}
    if schema_type is not None:
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        if (
            not types
            or any(not isinstance(item, str) or item not in valid_types for item in types)
            or len(types) != len(set(types))
        ):
            raise ValueError(f"工具参数 schema.type 必须是有效类型: {name}")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ValueError(f"工具参数 schema.enum 必须是非空数组: {name}")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError(f"工具参数 schema.properties 必须是对象: {name}")
    _validate_schema_properties(properties, name)
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(item, str) for item in required)
        or len(required) != len(set(required))
        or any(item not in properties for item in required)
    ):
        raise ValueError(f"工具参数 schema.required 必须引用 properties: {name}")
    for keyword in ("additionalProperties", "additionalItems", "contains", "not", "if", "then", "else",
                    "propertyNames", "unevaluatedProperties", "unevaluatedItems"):
        if keyword in schema:
            _validate_schema(schema[keyword], name)
    if "items" in schema:
        items = schema["items"]
        for child in items if isinstance(items, list) else (items,):
            _validate_schema(child, name)
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if keyword not in schema:
            continue
        children = schema[keyword]
        if not isinstance(children, list):
            raise ValueError(f"工具参数 schema.{keyword} 必须是数组: {name}")
        for child in children:
            _validate_schema(child, name)
    for keyword in ("$defs", "definitions", "patternProperties", "dependentSchemas"):
        if keyword not in schema:
            continue
        children = schema[keyword]
        if not isinstance(children, Mapping):
            raise ValueError(f"工具参数 schema.{keyword} 必须是对象: {name}")
        for child in children.values():
            _validate_schema(child, name)


def _validate_schema_properties(properties: Mapping[str, object], name: str) -> None:
    for property_name, schema in properties.items():
        if not isinstance(property_name, str) or not isinstance(schema, (Mapping, bool)):
            raise ValueError(f"工具参数 schema.properties 成员必须是对象或布尔值: {name}")
        if isinstance(schema, bool):
            continue
        _validate_schema(schema, name)


def normalize_tools(tools: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    """Return provider-neutral function definitions without mutating input."""
    normalized: list[dict[str, object]] = []
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise ValueError("工具定义必须是对象")
        if tool.get("type") not in (None, "function"):
            raise ValueError("工具定义的类型必须是 function")
        name = tool_name(tool)
        if name in names:
            raise ValueError(f"工具名称重复: {name}")
        names.add(name)
        normalized.append({
            "name": name,
            "description": tool_description(tool),
            "parameters": tool_parameters(tool),
        })
    return tuple(normalized)


def json_object(value: object, *, label: str = "模型工具参数") -> dict[str, object]:
    if isinstance(value, Mapping):
        value = copy_json(value)
    elif isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except json.JSONDecodeError as error:
            raise ValueError(f"{label}不是有效 JSON") from error
    else:
        raise ValueError(f"{label}不是 JSON 对象")
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是 JSON 对象")
    return value


def tool_result_text(blocks: Sequence[object]) -> str:
    """Render text-only tool result fields without silently dropping images."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, ModelTextBlock):
            parts.append(block.text)
        elif isinstance(block, ModelImageBlock):
            parts.append(f"[image omitted: {block.media_type or 'unknown'}]")
    return "".join(parts)


def safe_tool_id(value: str, *, max_length: int = 64) -> str:
    """Normalize opaque ids for protocols that only accept simple characters."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", value or "") and len(value) <= max_length:
        return value
    return f"call_{hashlib.sha256(value.encode('utf-8')).hexdigest()}"[:max_length]


def compatible_tool_names(
    tools: Sequence[Mapping[str, object]],
    messages: Sequence[object],
    *,
    pattern: str = r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z",
) -> tuple[dict[str, str], dict[str, str]]:
    """Create stable protocol-safe aliases for tool definitions and history."""
    names: list[str] = []
    for tool in tools:
        try:
            names.append(tool_name(tool))
        except ValueError:
            continue
    for message in messages:
        content = getattr(message, "content", ())
        for block in content if isinstance(content, (list, tuple)) else ():
            name = getattr(block, "name", None)
            if isinstance(name, str) and name:
                names.append(name)
        name = getattr(message, "tool_name", None)
        if isinstance(name, str) and name:
            names.append(name)

    unique_names = list(dict.fromkeys(names))
    valid = re.compile(pattern)
    reserved = {name for name in unique_names if valid.fullmatch(name)}
    used: set[str] = set()
    forward: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for index, name in enumerate(unique_names):
        if valid.fullmatch(name) and name not in used:
            alias = name
        else:
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
            for width in range(12, 53, 4):
                alias = f"meido_tool_{digest[:width]}"
                if alias not in reserved and alias not in used:
                    break
            else:
                alias = f"meido_tool_{digest[:45]}_{index}"
        forward[name] = alias
        reverse[alias] = name
        used.add(alias)
    return forward, reverse


def effective_tools(
    messages: Sequence[object],
    tools: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Apply transcript tool additions/removals over the call's tool snapshot."""
    ordered: dict[str, dict[str, object]] = {}
    for tool in normalize_tools(tools):
        ordered[tool_name(tool)] = tool
    for message in messages:
        if getattr(message, "role", None) != "system":
            continue
        for name in getattr(message, "tools_removed", ()):
            ordered.pop(name, None)
        for tool in normalize_tools(getattr(message, "tools_added", ())):
            ordered[tool_name(tool)] = tool
    return tuple(ordered.values())


def remap_message_tool_names(messages: Sequence[object], aliases: Mapping[str, str]) -> tuple[object, ...]:
    """Copy canonical messages while replacing tool names in calls/results."""
    from dataclasses import replace

    result: list[object] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, tuple):
            changed = tuple(
                replace(block, name=aliases.get(block.name, block.name))
                if hasattr(block, "name") and isinstance(getattr(block, "name", None), str)
                else block
                for block in content
            )
            message = replace(message, content=changed)
        if hasattr(message, "tool_name"):
            message = replace(message, tool_name=aliases.get(message.tool_name, message.tool_name))
        result.append(message)
    return tuple(result)
