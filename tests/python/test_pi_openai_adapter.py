import asyncio
import json

import httpx

from backend.app.model_types import (
    ModelAssistantMessage,
    ModelResponseMetadata,
    ModelStreamDone,
    ModelStreamError,
    ModelTextDelta,
    ModelThinkingDelta,
    ModelToolCallBlock,
    ModelToolCallEnd,
    ModelToolResultMessage,
    ModelUserMessage,
    ModelTextBlock,
    ModelSystemMessage,
)
from backend.app.models import ModelConfiguration
from backend.app.pi_openai_adapter import OpenAICompletionsRichAdapter


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
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":7,"prompt_tokens_details":{"cached_tokens":3},"completion_tokens_details":{"reasoning_tokens":2}}}',
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


def test_rich_adapter_emits_deepseek_compat_request_and_replays_tool_history(monkeypatch):
    body = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    requests = []
    _patch_client(monkeypatch, body, requests)
    call = ModelToolCallBlock("bad|id", "todo.write", {"title": "x"})
    result = ModelToolResultMessage((ModelTextBlock("saved"),), "bad|id", "todo.write")

    async def collect():
        return [event async for event in OpenAICompletionsRichAdapter().stream_messages(
            (
                ModelUserMessage((ModelTextBlock("save"),)),
                ModelAssistantMessage((call,)),
                result,
            ),
            _configuration("deepseek", "https://api.deepseek.com"),
            tools=[{"type": "function", "function": {"name": "todo.write"}}],
        )]

    asyncio.run(collect())
    payload = json.loads(requests[0].content)
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["messages"][1]["tool_calls"][0]["function"]["name"] != "todo.write"
    assert payload["messages"][2]["tool_call_id"] == payload["messages"][1]["tool_calls"][0]["id"]
    assert payload["stream_options"] == {"include_usage": True}


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
        'data: {"delta":{"content":"hello"}}]}',
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
