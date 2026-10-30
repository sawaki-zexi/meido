import asyncio
import json

import httpx

from backend.app.model_types import (
    ModelAssistantMessage,
    ModelCapabilities,
    ModelResponseMetadata,
    ModelStreamDone,
    ModelStreamError,
    ModelTextDelta,
    ModelThinkingBlock,
    ModelThinkingDelta,
    ModelToolCallBlock,
    ModelToolCallEnd,
    ModelToolResultMessage,
    ModelUserMessage,
    ModelTextBlock,
    ModelSystemMessage,
)
from backend.app.models import ModelConfiguration
from backend.app.message_transform import transform_messages
from backend.app.pi_openai_adapter import OpenAICompletionsOptions, OpenAICompletionsRichAdapter


def _patch_client(monkeypatch, body: str, requests: list[httpx.Request]):
    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )


def _configuration(provider="openai", base_url="https://example.test", api_key="secret"):
    return ModelConfiguration(
        providerId=provider,
        provider=provider,
        baseUrl=base_url,
        model="test-model",
        apiKey=api_key,
    )


def test_rich_adapter_parses_reasoning_usage_and_finish_reason(monkeypatch):
    body = "\n".join([
        'data: {"id":"resp-1","model":"reasoner","choices":[{"delta":{"reasoning_content":"think"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":7,"prompt_tokens_details":{"cached_tokens":3},"completion_tokens_details":{"reasoning_tokens":2},"cost":{"input":0.01,"output":0.02,"total":0.03}}}',
        "data: [DONE]",
        "",
    ])
    requests = []
    _patch_client(monkeypatch, body, requests)

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(),
        )]

    events = asyncio.run(collect())
    assert ModelThinkingDelta("think") in events
    assert ModelTextDelta("answer") in events
    done = events[-1]
    assert isinstance(done, ModelStreamDone)
    assert done.message.metadata == ModelResponseMetadata(
        provider="openai",
        api="openai-completions",
        model="reasoner",
        response_id="resp-1",
        usage=done.message.metadata.usage,
        stop_reason="stop",
        timestamp=done.message.metadata.timestamp,
    )
    assert done.message.metadata.usage.input_tokens == 12
    assert done.message.metadata.usage.output_tokens == 7
    assert done.message.metadata.usage.cache_read_tokens == 3
    assert done.message.metadata.usage.reasoning_tokens == 2
    assert done.message.metadata.usage.cost.total == 0.03


def test_rich_adapter_rejects_stream_without_finish_reason(monkeypatch):
    body = 'data: {"choices":[{"delta":{"content":"answer"}}]}\n\ndata: [DONE]\n\n'
    _patch_client(monkeypatch, body, [])

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(),
        )]

    events = asyncio.run(collect())
    assert isinstance(events[-1], ModelStreamError)
    assert "finish_reason" in events[-1].error
    assert not any(isinstance(event, ModelStreamDone) for event in events)


def test_rich_adapter_can_allow_providers_without_finish_reason(monkeypatch):
    body = 'data: {"choices":[{"delta":{"content":"answer"}}]}\n\ndata: [DONE]\n\n'
    _patch_client(monkeypatch, body, [])
    adapter = OpenAICompletionsRichAdapter(options=OpenAICompletionsOptions(supports_finish_reason=False))

    async def collect():
        return [event async for event in adapter.stream_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(),
        )]

    events = asyncio.run(collect())
    assert isinstance(events[-1], ModelStreamDone)
    assert events[-1].message.metadata.stop_reason == "stop"


def test_rich_adapter_rejects_provider_error_finish_reason(monkeypatch):
    body = 'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}\n\ndata: [DONE]\n\n'
    _patch_client(monkeypatch, body, [])

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(),
        )]

    events = asyncio.run(collect())
    assert isinstance(events[-1], ModelStreamError)
    assert "content_filter" in events[-1].error


def test_rich_adapter_aggregates_multiple_tool_call_fragments(monkeypatch):
    body = "\n".join([
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"todo","arguments":"{\\"a\\":"}},{"index":1,"id":"call-2","function":{"name":"shell","arguments":"{\\"cmd\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}},{"index":1,"function":{"arguments":"\\"ls\\"}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
        "",
    ])
    requests = []
    _patch_client(monkeypatch, body, requests)

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("run"),)),),
            _configuration(),
            tools=[
                {"type": "function", "function": {"name": "todo"}},
                {"type": "function", "function": {"name": "shell"}},
            ],
        )]

    events = asyncio.run(collect())
    done = events[-1]
    assert isinstance(done, ModelStreamDone)
    calls = [block for block in done.message.content if isinstance(block, ModelToolCallBlock)]
    assert calls == [
        ModelToolCallBlock("call-1", "todo", {"a": 1}),
        ModelToolCallBlock("call-2", "shell", {"cmd": "ls"}),
    ]
    assert sum(isinstance(event, ModelToolCallEnd) for event in events) == 2


def test_rich_adapter_merges_cumulative_tool_id_and_name_deltas(monkeypatch):
    body = "\n".join([
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-","function":{"name":"sh","arguments":"{\\"x\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"shell","arguments":"1}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
        "",
    ])
    _patch_client(monkeypatch, body, [])

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("run"),)),),
            _configuration(),
            tools=[{"type": "function", "function": {"name": "shell"}}],
        )]

    events = asyncio.run(collect())
    done = events[-1]
    assert isinstance(done, ModelStreamDone)
    calls = [block for block in done.message.content if isinstance(block, ModelToolCallBlock)]
    assert calls == [ModelToolCallBlock("call-1", "shell", {"x": 1})]


def test_rich_adapter_parses_partial_tool_arguments(monkeypatch):
    body = "\n".join([
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"lookup","arguments":"{\\"key\\":\\"partial"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
        "",
    ])
    _patch_client(monkeypatch, body, [])

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("lookup"),)),),
            _configuration(),
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )]

    events = asyncio.run(collect())
    done = events[-1]
    assert isinstance(done, ModelStreamDone)
    call = next(block for block in done.message.content if isinstance(block, ModelToolCallBlock))
    assert call.arguments == {"key": "partial"}


def test_rich_adapter_rejects_malformed_tool_arguments(monkeypatch):
    body = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"lookup","arguments":"not-json"}}]},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'
    _patch_client(monkeypatch, body, [])

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("lookup"),)),),
            _configuration(),
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )]

    events = asyncio.run(collect())
    assert isinstance(events[-1], ModelStreamError)
    assert "不是有效 JSON" in events[-1].error


def test_rich_adapter_rejects_non_json_tool_argument_fragments(monkeypatch):
    body = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"lookup","arguments":[]}}]},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'
    _patch_client(monkeypatch, body, [])

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("lookup"),)),),
            _configuration(),
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )]

    events = asyncio.run(collect())
    assert isinstance(events[-1], ModelStreamError)
    assert "参数格式无效" in events[-1].error


def test_rich_adapter_supports_legacy_function_call_and_full_message(monkeypatch):
    body = "\n".join([
        'data: {"choices":[{"delta":{"function_call":{"name":"lookup","arguments":"{\\"key\\":"}}}]}',
        'data: {"choices":[{"delta":{"function_call":{"arguments":"1}"}}}]}',
        'data: {"choices":[{"message":{"content":"done","tool_calls":[{"id":"call-full","type":"function","function":{"name":"lookup","arguments":"{\\"key\\":\\"name\\"}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
        "",
    ])
    requests = []
    _patch_client(monkeypatch, body, requests)

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("find"),)),),
            _configuration(),
        )]

    events = asyncio.run(collect())
    done = events[-1]
    assert isinstance(done, ModelStreamDone)
    calls = [block for block in done.message.content if isinstance(block, ModelToolCallBlock)]
    assert calls[0] == ModelToolCallBlock("call-legacy", "lookup", {"key": 1})
    assert calls[1] == ModelToolCallBlock("call-full", "lookup", {"key": "name"})
    assert ModelTextBlock("done") in done.message.content


def test_rich_adapter_merges_complete_tool_snapshot_after_delta(monkeypatch):
    body = "\n".join([
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-","function":{"name":"look","arguments":"{\\"key\\":"}}]}}]}',
        'data: {"choices":[{"message":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\\"key\\":1}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
        "",
    ])
    _patch_client(monkeypatch, body, [])

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("lookup"),)),),
            _configuration(),
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )]

    events = asyncio.run(collect())
    done = events[-1]
    assert isinstance(done, ModelStreamDone)
    calls = [block for block in done.message.content if isinstance(block, ModelToolCallBlock)]
    assert calls == [ModelToolCallBlock("call-1", "lookup", {"key": 1})]
    assert sum(isinstance(event, ModelToolCallEnd) for event in events) == 1


def test_rich_adapter_rejects_tool_call_without_function_name(monkeypatch):
    body = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'
    _patch_client(monkeypatch, body, [])

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("lookup"),)),),
            _configuration(),
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )]

    events = asyncio.run(collect())
    assert isinstance(events[-1], ModelStreamError)
    assert "缺少函数名" in events[-1].error


def test_rich_adapter_emits_deepseek_compat_request_and_replays_tool_history(monkeypatch):
    body = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    requests = []
    _patch_client(monkeypatch, body, requests)
    call = ModelToolCallBlock("bad|id", "todo.write", {"title": "x"})
    result = ModelToolResultMessage((ModelTextBlock("saved"),), "bad|id", "todo.write")
    thinking = ModelAssistantMessage((ModelThinkingBlock("old reasoning", "opaque"),))

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (
                ModelUserMessage((ModelTextBlock("save"),)),
                thinking,
                ModelAssistantMessage((call,)),
                result,
            ),
            _configuration("deepseek", "https://api.deepseek.com"),
            tools=[{"type": "function", "function": {"name": "todo.write"}}],
        )]

    asyncio.run(collect())
    payload = json.loads(requests[0].content)
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_content" not in payload["messages"][1]
    assert payload["messages"][2]["tool_calls"][0]["function"]["name"] != "todo.write"
    assert payload["messages"][3]["tool_call_id"] == payload["messages"][2]["tool_calls"][0]["id"]
    assert payload["stream_options"] == {"include_usage": True}


def test_rich_adapter_keeps_empty_tools_when_replaying_tool_history():
    from backend.app.model_protocol import OPENAI_COMPLETIONS_API

    call = ModelToolCallBlock("call-1", "lookup", {"key": "name"})
    messages = (
        ModelUserMessage((ModelTextBlock("find"),)),
        ModelAssistantMessage((call,), ModelResponseMetadata(
            provider="openai", api=OPENAI_COMPLETIONS_API, model="test-model", stop_reason="tool_use",
        )),
        ModelToolResultMessage((ModelTextBlock("found"),), "call-1", "lookup"),
    )

    request, _, _ = OpenAICompletionsRichAdapter()._prepare_request(
        messages, _configuration(), (), max_tokens=None, stream=True,
    )

    assert request["tools"] == []
    assert "tool_choice" not in request


def test_rich_adapter_redacts_embedded_error_and_event_stream_result(monkeypatch):
    body = 'data: {"error":{"message":"bad secret"}}\n\ndata: [DONE]\n\n'
    requests = []
    _patch_client(monkeypatch, body, requests)

    async def run():
        adapter = OpenAICompletionsRichAdapter()
        event_stream = adapter.stream(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(api_key="secret"),
        )
        events = [event async for event in event_stream]
        return events, await event_stream.result()

    events, result = asyncio.run(run())
    assert isinstance(events[-1], ModelStreamError)
    assert "secret" not in events[-1].error
    assert result.metadata.stop_reason == "error"


def test_rich_adapter_normalizes_http_error_details(monkeypatch):
    def handler(request):
        return httpx.Response(
            400,
            json={"error": {"message": "invalid request secret"}},
            request=request,
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(api_key="secret"),
        )]

    events = asyncio.run(collect())
    assert isinstance(events[-1], ModelStreamError)
    assert "HTTP 400" in events[-1].error
    assert "secret" not in events[-1].error


def test_rich_adapter_complete_uses_same_full_message_parser(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp-complete",
                "model": "complete-model",
                "choices": [{
                    "message": {
                        "content": "完成",
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"key":"name"}'},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"input_tokens": 4, "output_tokens": 3},
            },
            request=request,
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )

    async def complete():
        return await OpenAICompletionsRichAdapter().complete_messages(
            (ModelUserMessage((ModelTextBlock("find"),)),),
            _configuration(),
        )

    message = asyncio.run(complete())
    assert message.metadata.response_id == "resp-complete"
    assert message.metadata.model == "complete-model"
    assert message.metadata.usage.input_tokens == 4
    assert message.metadata.stop_reason == "tool_use"
    assert requests[0].content and json.loads(requests[0].content)["stream"] is False


def test_rich_adapter_complete_rejects_missing_finish_reason(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "answer"}}]},
            request=request,
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )

    async def complete():
        return await OpenAICompletionsRichAdapter().complete_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(),
        )

    try:
        asyncio.run(complete())
    except RuntimeError as error:
        assert "finish_reason" in str(error)
    else:
        raise AssertionError("expected missing finish_reason to fail")


def test_rich_adapter_complete_redacts_embedded_error(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={"error": {"message": "provider rejected secret"}},
            request=request,
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )

    async def complete():
        return await OpenAICompletionsRichAdapter().complete_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(api_key="secret"),
        )

    try:
        asyncio.run(complete())
    except RuntimeError as error:
        assert "secret" not in str(error)
        assert "***" in str(error)
    else:
        raise AssertionError("expected provider error to fail")


def test_rich_adapter_replays_system_tool_add_remove_state(monkeypatch):
    body = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    requests = []
    _patch_client(monkeypatch, body, requests)

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (
                ModelSystemMessage(
                    (ModelTextBlock("system"),),
                    tools_added=({"type": "function", "function": {"name": "added"}},),
                    tools_removed=("removed",),
                ),
            ),
            _configuration(),
            tools=[
                {"type": "function", "function": {"name": "removed"}},
                {"type": "function", "function": {"name": "kept"}},
            ],
        )]

    asyncio.run(collect())
    payload = json.loads(requests[0].content)
    names = [tool["function"]["name"] for tool in payload["tools"]]
    assert names == ["kept", "added"]


def test_rich_adapter_honors_cooperative_cancellation_at_sse_boundaries(monkeypatch):
    body = "\n".join([
        'data: {"choices":[{"delta":{"content":"first"}}]}',
        'data: {"choices":[{"delta":{"content":"second"}}]}',
        "data: [DONE]",
        "",
    ])
    requests = []
    _patch_client(monkeypatch, body, requests)
    checks = 0

    def cancel_check():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise RuntimeError("agent run cancelled")

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(),
            cancel_check=cancel_check,
        )]

    try:
        asyncio.run(collect())
    except RuntimeError as error:
        assert str(error) == "agent run cancelled"
    else:
        raise AssertionError("expected cooperative cancellation")


def test_rich_adapter_joins_multiline_sse_data_and_ignores_comments(monkeypatch):
    body = "\n".join([
        ": keep-alive",
        'data: {"choices":[',
        'data: {"delta":{"content":"hello"},"finish_reason":"stop"}]}',
        "",
        "data: [DONE]",
    ])
    requests = []
    _patch_client(monkeypatch, body, requests)

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),),
            _configuration(),
        )]

    events = asyncio.run(collect())
    assert ModelTextDelta("hello") in events
    assert isinstance(events[-1], ModelStreamDone)


def test_meido_provider_projects_rich_stream_into_runtime_events():
    from backend.app.agent_runtime import CancellationToken, MeidoProvider, TextDeltaEvent, UserMessage
    from backend.app.models import RoleInput, RoleProfile
    from backend.app.model_types import ModelStreamStart

    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools):
            assert messages[0].role == "system"
            assert tools
            yield ModelStreamStart()
            yield ModelTextDelta("完成")
            yield ModelStreamDone(
                ModelAssistantMessage(
                    (ModelTextBlock("完成"),),
                    ModelResponseMetadata(stop_reason="stop"),
                )
            )

    role = RoleInput(name="rich", profile=RoleProfile(profile="设定")).model_copy(update={"id": "role-rich"})
    provider = MeidoProvider(Adapter(), role, None)

    async def collect():
        return [event async for event in provider.stream_response(
            model="m",
            system="系统",
            messages=[UserMessage("hello")],
            tools=[{"type": "function", "function": {"name": "shell"}}],
            signal=CancellationToken(),
        )]

    # The runtime input type remains its own domain type; this test only checks
    # the rich adapter boundary through the provider projection.
    events = asyncio.run(collect())
    assert any(isinstance(event, TextDeltaEvent) and event.delta == "完成" for event in events)


def test_meido_provider_uses_rich_stream_for_plain_reasoning_turns():
    from backend.app.agent_runtime import CancellationToken, MeidoProvider, TextDeltaEvent, UserMessage
    from backend.app.models import RoleInput, RoleProfile

    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools):
            assert tools == []
            yield ModelTextDelta("普通回复")
            yield ModelStreamDone(ModelAssistantMessage(
                (ModelTextBlock("普通回复"),),
                ModelResponseMetadata(stop_reason="stop"),
            ))

    role = RoleInput(name="plain", profile=RoleProfile(profile="设定")).model_copy(update={"id": "role-plain"})
    provider = MeidoProvider(Adapter(), role, None)

    async def collect():
        return [event async for event in provider.stream_response(
            model="m",
            system="系统",
            messages=[UserMessage("hello")],
            tools=[],
            signal=CancellationToken(),
        )]

    events = asyncio.run(collect())
    assert any(isinstance(event, TextDeltaEvent) and event.delta == "普通回复" for event in events)


def test_meido_provider_persists_thinking_for_next_provider_turn_without_public_leak():
    from datetime import datetime, timezone

    from backend.app.agent_runtime import AssistantDoneEvent, CancellationToken, MeidoProvider, UserMessage
    from backend.app.models import Message, RoleInput, RoleProfile
    from backend.app.session_manager import public_message

    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools):
            assistant_messages = [message for message in messages if message.role == "assistant"]
            if assistant_messages:
                assert assistant_messages[0].content[0].type == "thinking"
                assert assistant_messages[0].content[0].signature == "opaque"
            yield ModelTextDelta("回复")
            yield ModelStreamDone(ModelAssistantMessage(
                (ModelThinkingBlock("内部思考", "opaque"), ModelTextBlock("回复")),
                ModelResponseMetadata(stop_reason="stop", model="deepseek-chat"),
            ))

    role = RoleInput(name="thinking", profile=RoleProfile(profile="设定")).model_copy(update={"id": "role-thinking"})
    provider = MeidoProvider(Adapter(), role, None)

    async def first():
        return [event async for event in provider.stream_response(
            model="m", system="系统", messages=[UserMessage("hello")], tools=[], signal=CancellationToken()
        )]

    events = asyncio.run(first())
    done = next(event for event in events if isinstance(event, AssistantDoneEvent))
    assistant = done.message
    assert assistant.metadata["thinking"][0]["signature"] == "opaque"
    assert assistant.metadata["content_blocks"] == [
        {"text": "内部思考", "signature": "opaque", "type": "thinking", "redacted": False},
        {"text": "回复", "type": "text", "text_signature": None},
    ]
    public = public_message(Message(
        id="m",
        sessionKey="s",
        sequence=1,
        role="assistant",
        content="回复",
        status="completed",
        createdAt=datetime.now(timezone.utc),
        metadata=assistant.metadata,
    ))
    assert "thinking" not in public.metadata
    assert "content_blocks" not in public.metadata

    async def second_collect():
        return [event async for event in provider.stream_response(
            model="m",
            system="系统",
            messages=[UserMessage("hello"), assistant],
            tools=[],
            signal=CancellationToken(),
        )]

    second = asyncio.run(second_collect())
    assert second[0].type == "provider_start"


def test_meido_provider_restores_model_metadata_for_transcript_replay():
    from backend.app.agent_runtime import MeidoProvider
    from backend.app.agent_runtime.types import AssistantMessage
    from backend.app.message_transform import transform_messages
    from backend.app.model_types import ModelCapabilities
    from backend.app.models import RoleInput, RoleProfile

    role = RoleInput(name="metadata", profile=RoleProfile(profile="设定")).model_copy(update={"id": "role-metadata"})
    provider = MeidoProvider(object(), role, None)
    runtime_message = AssistantMessage(
        content="answer",
        metadata={
            "provider": "deepseek",
            "api": "openai-completions",
            "model": "deepseek-reasoner",
            "response_id": "response-1",
            "stop_reason": "stop",
            "usage": {"input_tokens": 12, "output_tokens": 4},
            "thinking": [{"text": "private thought", "signature": "opaque"}],
        },
    )

    restored = next(message for message in provider._rich_messages("system", [runtime_message]) if isinstance(message, ModelAssistantMessage))
    assert restored.metadata.model == "deepseek-reasoner"
    assert restored.metadata.response_id == "response-1"
    assert restored.metadata.usage.input_tokens == 12

    replayed = transform_messages(
        (restored,),
        capabilities=ModelCapabilities(preserve_thinking_signatures=True),
        target_model="deepseek-chat",
    )
    assert replayed[0].content == (ModelTextBlock("private thought"), ModelTextBlock("answer"))

    failed = next(message for message in provider._rich_messages("system", [AssistantMessage(
        content="partial",
        stop_reason="error",
        metadata={"model": "deepseek-reasoner", "stop_reason": "error"},
    )]) if isinstance(message, ModelAssistantMessage))
    assert transform_messages((failed,), target_model="deepseek-reasoner") == ()


def test_openai_completion_metadata_uses_wire_provider_for_replay(monkeypatch):
    def handler(request):
        del request
        return httpx.Response(200, json={
            "id": "response-1",
            "model": "custom-model",
            "choices": [{
                "message": {"content": "ok", "reasoning_content": "private"},
                "finish_reason": "stop",
            }],
        })

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(
        providerId="custom",
        provider="deepseek-compatible",
        api="openai-completions",
        baseUrl="https://provider.test/v1",
        model="custom-model",
        apiKey="secret",
    )

    message = asyncio.run(OpenAICompletionsRichAdapter().complete_messages(
        [ModelUserMessage((ModelTextBlock("hello"),))], configuration,
    ))

    assert message.metadata.provider == "deepseek-compatible"
    replayed = transform_messages(
        (message,),
        capabilities=ModelCapabilities(preserve_thinking_signatures=True),
        target_provider="deepseek-compatible",
        target_api="openai-completions",
        target_model="custom-model",
    )
    assert any(isinstance(block, ModelThinkingBlock) and block.text == "private" for block in replayed[0].content)
