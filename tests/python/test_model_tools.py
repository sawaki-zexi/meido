import asyncio
import json

import httpx

from backend.app.agent_runtime import AssistantDoneEvent, CancellationToken, MeidoProvider, ToolCallEndEvent, UserMessage
from backend.app.agent_runtime.types import AssistantMessage, ToolResultMessage
from backend.app.model_adapter import ModelToolCall, ModelTextDelta, OpenAICompatibleAdapter
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
