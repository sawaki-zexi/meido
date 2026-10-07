from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from typing import cast

from ..models import Message, ModelConfiguration, Role
from ..model_adapter import ModelAdapter, ModelStreamError, ModelTextDelta, ModelToolCall
from .provider import AssistantDoneEvent, CancellationToken, ProviderErrorEvent, ProviderEvent, ProviderStartEvent, TextDeltaEvent, ToolCallEndEvent
from .types import AssistantMessage, ToolCall, UserMessage, ToolResultMessage


class MeidoProvider:
    """Adapt Meido's model boundary to the provider-neutral Runtime contract."""

    def __init__(
        self,
        adapter: ModelAdapter,
        role: Role,
        configuration: ModelConfiguration | None,
        memory_context: str = "",
    ) -> None:
        self.adapter = adapter
        self.role = role
        self.configuration = configuration
        self.memory_context = memory_context

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[object],
        tools: Sequence[dict[str, object]],
        signal: CancellationToken,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del model, session_id
        if tools:
            structured = getattr(self.adapter, "stream_structured_messages", None)
            if structured is None:
                async def unsupported() -> AsyncIterator[ProviderEvent]:
                    yield ProviderStartEvent()
                    yield ProviderErrorEvent("当前模型适配器尚未支持结构化工具调用")
                return unsupported()
            return self._stream_structured(system, messages, tools, signal, structured)
        return self._stream(messages, signal)

    def _stream_structured(self, system, messages, tools, signal, structured) -> AsyncIterator[ProviderEvent]:
        async def stream() -> AsyncIterator[ProviderEvent]:
            runtime_messages = self._structured_messages(system, messages)
            yield ProviderStartEvent()
            content = ""
            tool_calls: list[ToolCall] = []
            try:
                source = structured(runtime_messages, self.configuration, [dict(tool) for tool in tools])
                async for event in source:
                    signal.raise_if_cancelled()
                    if isinstance(event, ModelTextDelta):
                        content += event.text
                        yield TextDeltaEvent(event.text)
                    elif isinstance(event, ModelToolCall):
                        call = ToolCall(event.id, event.name, event.arguments)
                        tool_calls.append(call)
                        yield ToolCallEndEvent(call)
                    elif isinstance(event, ModelStreamError):
                        yield ProviderErrorEvent(event.error)
                        return
                yield AssistantDoneEvent(AssistantMessage(
                    content=content,
                    tool_calls=tuple(tool_calls),
                    stop_reason="tool_use" if tool_calls else "stop",
                ))
            except Exception as error:
                if isinstance(error, RuntimeError) and str(error) == "agent run cancelled":
                    raise
                yield ProviderErrorEvent(str(error))
        return stream()

    def _structured_messages(self, system: str, messages: Sequence[object]) -> list[dict[str, object]]:
        prompt = system or self._role_system_prompt()
        result: list[dict[str, object]] = [{"role": "system", "content": prompt}]
        for message in messages:
            if isinstance(message, UserMessage):
                result.append({"role": "user", "content": message.content})
            elif isinstance(message, AssistantMessage):
                item: dict[str, object] = {"role": "assistant", "content": message.content or None}
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in message.tool_calls
                    ]
                result.append(item)
            elif isinstance(message, ToolResultMessage):
                result.append({
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                })
        return result

    def _role_system_prompt(self) -> str:
        profile = self.role.profile
        return "\n\n".join(part for part in [
            f"你正在扮演 {self.role.name}。",
            f"简介：{self.role.description}" if self.role.description else "",
            f"角色设定：{profile.profile}",
            f"性格：{profile.personality}" if profile.personality else "",
            f"行为规则：{profile.behaviorRules}" if profile.behaviorRules else "",
            f"回复约束：{profile.responseConstraints}" if profile.responseConstraints else "",
            f"称呼用户：{profile.nickname}" if profile.nickname else "",
            self.memory_context,
        ] if part)

    async def _stream(self, messages: Sequence[object], signal: CancellationToken) -> AsyncIterator[ProviderEvent]:
        history: list[Message] = []
        for message in messages:
            if isinstance(message, UserMessage):
                history.append(_message("user", message.content))
            elif isinstance(message, AssistantMessage):
                history.append(_message("assistant", message.content))
            elif isinstance(message, ToolResultMessage):
                history.append(_message("tool", message.content))
        stream_reply = self.adapter.stream_reply
        try:
            accepts_memory = "memory_context" in inspect.signature(stream_reply).parameters
        except (TypeError, ValueError):
            accepts_memory = False
        yield ProviderStartEvent()
        source: object = (
            stream_reply(self.role, history, self.configuration, memory_context=self.memory_context)
            if accepts_memory
            else stream_reply(self.role, history, self.configuration)
        )
        content = ""
        async for delta in cast(AsyncIterator[str], source):
            signal.raise_if_cancelled()
            content += delta
            yield TextDeltaEvent(delta)
        yield AssistantDoneEvent(AssistantMessage(content=content))


def _message(role: str, content: str) -> Message:
    return Message(
        id="runtime",
        sessionKey="runtime",
        sequence=0,
        role=role,
        content=content,
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
