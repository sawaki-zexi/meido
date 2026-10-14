import asyncio
import json
from types import MappingProxyType

import httpx
import pytest

from backend.app.agent_runtime import AssistantDoneEvent, CancellationToken, MeidoProvider, ToolCallEndEvent
from backend.app.agent_runtime.types import AssistantMessage, AttachmentContent, ImageContent, ToolResultMessage, UserMessage
from backend.app.model_adapter import ModelStreamError, ModelToolCall, ModelTextDelta, OpenAICompatibleAdapter
from backend.app.models import ModelConfiguration, RoleInput, RoleProfile


def test_openai_compatible_adapter_aggregates_streamed_tool_call(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        body = "\n".join([
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"shell","arguments":"{\\"command\\":\\"echo"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":" hi\\\"}"}}]}}]}',
            "data: [DONE]",
            "",
        ])
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(
        providerId="openai",
        provider="openai",
        baseUrl="https://api.openai.com/v1",
        model="test-model",
        apiKey="secret",
    )

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}],
            configuration,
            [{"type": "function", "function": {"name": "shell"}}],
        )]

    events = asyncio.run(collect())
    assert events[0] == ModelToolCall("call-1", "shell", {"command": "echo hi"})
    payload = json.loads(requests[0].content)
    assert payload["tools"][0]["function"]["name"] == "shell"
    assert payload["tool_choice"] == "auto"


def test_openai_compatible_adapter_normalizes_legacy_function_call_stream(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        alias = requests[0]["tools"][0]["function"]["name"]
        assert requests[0]["messages"][0]["function_call"]["name"] == alias
        assert requests[0]["messages"][1]["name"] == alias
        body = "\n".join([
            "data: " + json.dumps({"choices": [{"delta": {"function_call": {
                "name": alias,
                "arguments": '{"message":',
            }}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"function_call": {
                "arguments": '"hello"}',
            }}}]}),
            "data: [DONE]",
            "",
        ])
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(
        providerId="custom",
        provider="custom",
        baseUrl="https://compatible.example/v1",
        model="test-model",
    )

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [
                {"role": "assistant", "content": None, "function_call": {"name": "plugin.echo", "arguments": "{}"}},
                {"role": "function", "name": "plugin.echo", "content": "{}"},
                {"role": "user", "content": "run"},
            ],
            configuration,
            [{"type": "function", "function": {"name": "plugin.echo"}}],
        )]

    events = asyncio.run(collect())

    assert events == [ModelToolCall("call-0", "plugin.echo", {"message": "hello"})]
    assert requests[0]["tools"][0]["function"]["name"] != "plugin.echo"


def test_openai_compatible_adapter_accepts_multiple_complete_message_tool_calls(monkeypatch):
    def handler(request):
        del request
        chunk = {
            "choices": [{"message": {"tool_calls": [
                {
                    "id": "call-complete-1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": '{"command":"echo hi"}'},
                },
                {
                    "id": "call-complete-2",
                    "type": "function",
                    "function": {"name": "shell", "arguments": '{"command":"echo there"}'},
                },
            ]}}],
        }
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}],
            configuration,
            [{"type": "function", "function": {"name": "shell"}}],
        )]

    assert asyncio.run(collect()) == [
        ModelToolCall("call-complete-1", "shell", {"command": "echo hi"}),
        ModelToolCall("call-complete-2", "shell", {"command": "echo there"}),
    ]


def test_openai_compatible_adapter_normalizes_legacy_tool_shape_and_history(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"完成"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")
    tool = MappingProxyType({
        "name": "plugin.echo",
        "description": "echo",
        "input_schema": MappingProxyType({
            "properties": MappingProxyType({"message": MappingProxyType({"type": "string"})}),
            "required": ["message"],
        }),
    })
    messages = [
        {"role": "assistant", "content": None, "function_call": {"name": "plugin.echo", "arguments": "{}"}},
        {"role": "function", "name": "plugin.echo", "content": "{}"},
        {"role": "user", "content": "run"},
    ]

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            messages, configuration, [tool]
        )]

    assert asyncio.run(collect()) == [ModelTextDelta("完成")]
    payload = requests[0]
    wire_name = payload["tools"][0]["function"]["name"]
    assert wire_name != "plugin.echo"
    assert payload["tools"][0]["function"]["parameters"] == {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
    assert payload["messages"][0]["function_call"]["name"] == wire_name
    assert payload["messages"][1]["name"] == wire_name
    assert messages[0]["function_call"]["name"] == "plugin.echo"


def test_openai_compatible_adapter_rejects_invalid_nested_tool_schema_before_network(monkeypatch):
    called = False

    def handler(request):
        nonlocal called
        called = True
        del request
        return httpx.Response(500)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}],
            configuration,
            [{"name": "ping", "parameters": {"properties": []}}],
        )]

    with pytest.raises(ValueError, match="schema.properties 必须是对象"):
        asyncio.run(collect())
    assert called is False


def test_openai_compatible_adapter_rejects_invalid_property_schema_and_required_names(monkeypatch):
    called = False

    def handler(request):
        nonlocal called
        called = True
        del request
        return httpx.Response(500)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")

    async def collect(tool):
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}], configuration, [tool]
        )]

    with pytest.raises(ValueError, match="properties 成员必须是对象"):
        asyncio.run(collect({"name": "ping", "parameters": {"properties": {"value": "string"}}}))
    with pytest.raises(ValueError, match="required 必须引用 properties"):
        asyncio.run(collect({"name": "ping", "parameters": {"properties": {}, "required": ["value"]}}))
    assert called is False


def test_openai_compatible_adapter_keeps_tool_calls_with_string_indexes_and_duplicate_ids(monkeypatch):
    def handler(request):
        del request
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": "0", "id": "call-1", "function": {"name": "shell", "arguments": '{"command":"one'}},
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": "1", "id": "call-2", "function": {"name": "shell", "arguments": '{"command":"two"}'}},
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": "0", "id": "call-1", "function": {"name": "shell", "arguments": '"}'}},
            ]}}]},
        ]
        body = "\n".join([
            *(f"data: {json.dumps(chunk)}" for chunk in chunks),
            "data: [DONE]",
            "",
        ])
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}],
            configuration,
            [{"name": "shell"}],
        )]

    assert asyncio.run(collect()) == [
        ModelToolCall("call-1", "shell", {"command": "one"}),
        ModelToolCall("call-2", "shell", {"command": "two"}),
    ]


def test_openai_compatible_adapter_preserves_repeated_incremental_argument_fragments(monkeypatch):
    def handler(request):
        del request
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call-1", "function": {"name": "shell", "arguments": '{"value":"x'}},
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"value":"x'}},
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"}'}}]}}]},
        ]
        body = "\n".join([*(f"data: {json.dumps(chunk)}" for chunk in chunks), "data: [DONE]", ""])
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}], configuration, [{"name": "shell"}]
        )]

    assert asyncio.run(collect()) == [ModelStreamError("模型工具参数不是有效 JSON: shell")]


def test_openai_compatible_adapter_accepts_complete_message_object_arguments(monkeypatch):
    def handler(request):
        del request
        chunk = {"choices": [{"message": {"tool_calls": [
            {"id": "call-complete", "type": "function", "function": {
                "name": "shell", "arguments": {"command": "echo hi"},
            }},
        ]}}]}
        return httpx.Response(
            200,
            text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}], configuration, [{"name": "shell"}]
        )]

    assert asyncio.run(collect()) == [
        ModelToolCall("call-complete", "shell", {"command": "echo hi"}),
    ]


def test_openai_compatible_adapter_normalizes_empty_tool_parameters(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"完成"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}],
            configuration,
            [{"function": {"name": "ping", "parameters": {}}}],
        )]

    assert asyncio.run(collect()) == [ModelTextDelta("完成")]
    assert requests[0]["tools"][0]["type"] == "function"
    assert requests[0]["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_openai_compatible_adapter_copies_read_only_tool_mappings(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"完成"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")
    tool = MappingProxyType({
        "type": "function",
        "function": MappingProxyType({
            "name": "readonly.tool",
            "parameters": MappingProxyType({
                "type": "object",
                "properties": MappingProxyType({"value": MappingProxyType({"type": "string"})}),
            }),
        }),
    })

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}], configuration, [tool]
        )]

    assert asyncio.run(collect()) == [ModelTextDelta("完成")]
    assert requests[0]["tools"][0]["function"]["parameters"]["properties"] == {
        "value": {"type": "string"},
    }


def test_openai_compatible_adapter_rejects_malformed_tool_before_network(monkeypatch):
    called = False

    def handler(request):
        nonlocal called
        called = True
        del request
        return httpx.Response(500)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="custom", provider="custom", baseUrl="https://example.test", model="m")

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "run"}],
            configuration,
            [{"type": "function", "function": {"name": "ping", "parameters": []}}],
        )]

    with pytest.raises(ValueError, match="参数 schema 必须是对象"):
        asyncio.run(collect())
    assert called is False


def test_deepseek_structured_requests_disable_thinking_without_reasoning_replay(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"完成"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(
        providerId="deepseek",
        provider="deepseek",
        baseUrl="https://api.deepseek.com",
        model="deepseek-flash",
    )

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "hello"}],
            configuration,
            [{"type": "function", "function": {"name": "todo.write"}}],
        )]

    assert asyncio.run(collect()) == [ModelTextDelta("完成")]
    assert requests[0]["thinking"] == {"type": "disabled"}


def test_openai_compatible_adapter_keeps_text_deltas_with_tools(monkeypatch):
    def handler(request):
        del request
        body = 'data: {"choices":[{"delta":{"content":"完成"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(providerId="x", provider="x", baseUrl="https://example.test", model="m")

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages([], configuration, [])]

    assert asyncio.run(collect()) == [ModelTextDelta("完成")]


def test_openai_compatible_adapter_surfaces_redacted_http_error_detail(monkeypatch):
    request_payloads = []

    def handler(request):
        request_payloads.append(json.loads(request.content))
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid function name; secret=test-secret"}},
            request=request,
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(
        providerId="deepseek",
        provider="deepseek",
        baseUrl="https://api.deepseek.com",
        model="deepseek-flash",
        apiKey="test-secret",
    )

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "hello"}],
            configuration,
            [{"type": "function", "function": {"name": "todo.write"}}],
        )]

    with pytest.raises(httpx.HTTPStatusError) as raised:
        asyncio.run(collect())

    assert raised.value.response.status_code == 400
    assert "Invalid function name" in str(raised.value)
    assert "test-secret" not in str(raised.value)
    assert request_payloads[0]["tools"][0]["function"]["name"] != "todo.write"


def test_openai_legacy_structured_rejects_invalid_tool_schema_before_http(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="data: [DONE]\n\n")

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    configuration = ModelConfiguration(
        providerId="openai",
        provider="openai",
        baseUrl="https://api.openai.com/v1",
        model="test-model",
    )

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages(
            [{"role": "user", "content": "hello"}],
            configuration,
            [{"type": "function", "function": {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {"count": "integer"}},
            }}],
        )]

    with pytest.raises(ValueError, match="schema.properties 成员必须是对象"):
        asyncio.run(collect())

    assert requests == []


def test_structured_adapter_uses_environment_configuration_when_unbound(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"完成"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv("MEIDO_MODEL_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("MEIDO_MODEL", "env-model")
    monkeypatch.setenv("MEIDO_API_KEY", "env-key")

    async def collect():
        return [event async for event in OpenAICompatibleAdapter().stream_structured_messages([], None, [])]

    assert asyncio.run(collect()) == [ModelTextDelta("完成")]
    assert requests[0].url == "https://env.example/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer env-key"


def test_meido_provider_sends_user_image_as_multimodal_content():
    from backend.app.agent_runtime import MeidoProvider

    class RecordingAdapter:
        def __init__(self):
            self.messages = None

        async def stream_structured_messages(self, messages, configuration, tools):
            self.messages = messages
            yield ModelTextDelta("收到")

    role = RoleInput(name="视觉角色", profile=RoleProfile(profile="设定")).model_copy(update={"id": "role-vision"})
    adapter = RecordingAdapter()
    provider = MeidoProvider(adapter, role, None)
    user = UserMessage(
        "看看这张图",
        attachments=(AttachmentContent("photo.png", "image/png", image=ImageContent("image/png", "aW1hZ2U=")),),
    )

    async def collect():
        return [event async for event in provider.stream_response(
            model="m", system="", messages=[user], tools=[{"type": "function"}], signal=__import__("backend.app.agent_runtime", fromlist=["CancellationToken"]).CancellationToken(),
        )]

    asyncio.run(collect())
    assert adapter.messages[1]["content"] == [
        {"type": "text", "text": "看看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U=", "detail": "low"}},
    ]
def test_meido_provider_maps_structured_transcript_and_tool_call():
    role = RoleInput(name="工具角色", profile=RoleProfile(profile="设定"), agentConfig={})
    seen = []

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            del configuration, tools
            seen.append(messages)
            if len(seen) == 1:
                yield ModelToolCall("call-1", "shell", {"command": "echo hi"})
            else:
                yield ModelTextDelta("完成")

    provider = MeidoProvider(Adapter(), role, None)

    async def collect(messages):
        return [event async for event in provider.stream_response(
            model="test",
            system="系统提示",
            messages=messages,
            tools=[{"type": "function", "function": {"name": "shell"}}],
            signal=CancellationToken(),
        )]

    first = asyncio.run(collect([UserMessage("运行命令")]))
    assert isinstance(first[1], ToolCallEndEvent)
    assert isinstance(first[-1], AssistantDoneEvent)
    call = first[1].tool_call
    second = asyncio.run(collect([
        UserMessage("运行命令"),
        AssistantMessage(tool_calls=(call,), stop_reason="tool_use"),
        ToolResultMessage(call.id, call.name, "hi"),
    ]))
    assert second[-1].message.content == "完成"
    assert seen[1][0] == {"role": "system", "content": "系统提示"}
    assert seen[1][-1] == {"role": "tool", "tool_call_id": "call-1", "content": "hi"}


def test_meido_provider_merges_complete_tool_snapshot_on_rich_stream(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call-",
            "function": {"name": "sh", "arguments": '{"x":'},
        }]}}]},
        {"choices": [{"message": {"tool_calls": [{
            "index": 0,
            "id": "call-1",
            "type": "function",
            "function": {"name": "shell", "arguments": '{"x":1}'},
        }]}, "finish_reason": "tool_calls"}]},
    ]
    body = "\n".join([*(f"data: {json.dumps(chunk)}" for chunk in chunks), "data: [DONE]", ""])
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda timeout: original_client(timeout=timeout, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        )),
    )
    configuration = ModelConfiguration(
        providerId="custom",
        provider="custom",
        baseUrl="https://compatible.example/v1",
        model="test-model",
    )
    role = RoleInput(name="工具角色", profile=RoleProfile(profile="设定"))
    provider = MeidoProvider(OpenAICompatibleAdapter(), role, configuration)

    async def collect():
        return [event async for event in provider.stream_response(
            model="test-model",
            system="系统提示",
            messages=[UserMessage("运行工具")],
            tools=[{"type": "function", "function": {"name": "shell"}}],
            signal=CancellationToken(),
        )]

    events = asyncio.run(collect())
    completed = next(event.message for event in events if isinstance(event, AssistantDoneEvent))
    tool_events = [event.tool_call for event in events if isinstance(event, ToolCallEndEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].id == "call-1"
    assert tool_events[0].name == "shell"
    assert tool_events[0].arguments == {"x": 1}
    assert completed.tool_calls == tuple(tool_events)
