import asyncio
import json
import struct

import httpx
import pytest

from backend.app.anthropic_adapter import AnthropicMessagesAdapter
from backend.app.bedrock_adapter import BedrockConverseAdapter
from backend.app.google_adapter import GoogleGenerativeAdapter
from backend.app.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.model_types import ModelCost, ModelStreamDone, ModelStreamError, ModelTextBlock, ModelSystemMessage, ModelUserMessage
from backend.app.model_types import (
    ModelAssistantMessage, ModelImageBlock, ModelResponseMetadata, ModelThinkingBlock,
    ModelToolCallBlock, ModelToolResultMessage,
)
from backend.app.mistral_adapter import MistralConversationsAdapter
from backend.app.model_protocol import normalize_tools
from backend.app.message_transform import transform_messages
from backend.app.models import ModelConfiguration
from backend.app.openai_responses_adapter import OpenAIResponsesAdapter
from backend.app.azure_openai_responses_adapter import AzureOpenAIResponsesAdapter
from backend.app.openai_codex_responses_adapter import OpenAICodexResponsesAdapter
from backend.app.google_vertex_adapter import GoogleVertexAdapter
from backend.app.pi_messages_adapter import PiMessagesAdapter, _message, _usage


def configuration(api: str, base_url: str = "https://provider.test", provider: str = "provider"):
    return ModelConfiguration(
        providerId=provider,
        provider=provider,
        api=api,
        baseUrl=base_url,
        model="model-1",
        apiKey="secret-key",
    )


def patch_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout: original(timeout=timeout, transport=httpx.MockTransport(handler)))


def messages():
    return [ModelSystemMessage((ModelTextBlock("system"),)), ModelUserMessage((ModelTextBlock("hello"),))]


def test_anthropic_messages_uses_native_path_and_tool_schema(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        body = "".join([
            'event: message_start\ndata: {"message":{"id":"msg_1","model":"claude"}}\n\n',
            'event: content_block_start\ndata: {"index":0,"content_block":{"type":"text","text":"hi"}}\n\n',
            'event: content_block_delta\ndata: {"index":0,"delta":{"type":"text_delta","text":"!"}}\n\n',
            'event: content_block_stop\ndata: {"index":0}\n\n',
            'event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"}}\n\n',
            'event: message_stop\ndata: {}\n\n',
        ])
        return httpx.Response(200, text=body)

    patch_client(monkeypatch, handler)
    events = asyncio.run(collect(AnthropicMessagesAdapter().stream_messages(
        messages(), configuration("anthropic-messages"),
        [{"name": "todo.write", "description": "write", "parameters": {"type": "object", "properties": {}}}],
    )))
    assert requests[0].url == "https://provider.test/messages"
    payload = json.loads(requests[0].content)
    assert payload["system"] == "system"
    assert payload["tools"][0]["input_schema"]["type"] == "object"
    assert any(isinstance(event, ModelStreamDone) and "hi!" == event.message.content[0].text for event in events)


def test_anthropic_tool_schema_drops_gateway_rejected_annotations():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Lookup input",
        "type": "object",
        "properties": {
            "query": {"type": "string", "default": "", "examples": ["status"]},
        },
    }

    payload, _headers, _aliases = AnthropicMessagesAdapter()._prepare(
        messages(), configuration("anthropic-messages"),
        [{"name": "lookup", "parameters": schema}],
        max_tokens=None, stream=True,
    )

    assert payload["tools"][0]["input_schema"] == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }
    assert schema["properties"]["query"]["default"] == ""


@pytest.mark.parametrize("keyword, value", [
    ("minimum", 1),
    ("maximum", 10),
    ("exclusiveMinimum", 0),
    ("maxItems", 2),
    ("minProperties", 1),
    ("format", "regex"),
])
def test_anthropic_tool_schema_rejects_unsupported_constraints_before_request(keyword, value):
    schema = {"type": "object", "properties": {"query": {"type": "string", keyword: value}}}

    with pytest.raises(ValueError, match=r"\$\.properties\.query"):
        AnthropicMessagesAdapter()._prepare(
            messages(), configuration("anthropic-messages"),
            [{"name": "lookup", "parameters": schema}],
            max_tokens=None, stream=True,
        )


def test_anthropic_replays_thinking_blocks_with_their_signature():
    history = [
        ModelAssistantMessage(
            (ModelThinkingBlock("private reasoning", "anthropic-signature"),),
            ModelResponseMetadata(provider="anthropic", api="anthropic-messages", model="claude"),
        ),
        ModelUserMessage((ModelTextBlock("continue"),)),
    ]
    config = configuration("anthropic-messages", provider="anthropic")
    config.model = "claude"
    payload, _headers, _aliases = AnthropicMessagesAdapter()._prepare(
        history, config, (),
        max_tokens=None, stream=True,
    )

    assert payload["messages"][0]["content"][0] == {
        "type": "thinking", "thinking": "private reasoning", "signature": "anthropic-signature",
    }


def test_anthropic_completion_preserves_custom_provider_identity_for_replay(monkeypatch):
    def handler(request):
        del request
        return httpx.Response(200, json={
            "id": "msg_custom",
            "model": "claude",
            "content": [
                {"type": "thinking", "thinking": "private", "signature": "opaque"},
                {"type": "text", "text": "answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 2, "output_tokens": 3},
        })

    patch_client(monkeypatch, handler)
    config = configuration("anthropic-messages", provider="custom-anthropic")
    config.model = "claude"
    message = asyncio.run(AnthropicMessagesAdapter().complete_messages(
        messages(), config,
    ))

    assert message.metadata.provider == "custom-anthropic"
    replayed = transform_messages(
        (message,),
        target_provider="custom-anthropic",
        target_api="anthropic-messages",
        target_model="claude",
    )
    assert replayed[0].content[0] == ModelThinkingBlock("private", "opaque")


@pytest.mark.parametrize("fragments, expected", [
    (["", '{"count":', "1}"], {"count": 1}),
    ([], {}),
])
def test_anthropic_stream_restores_tool_arguments_after_empty_input(monkeypatch, fragments, expected):
    def handler(request):
        del request
        payloads = [
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "call-1", "name": "lookup", "input": {},
            }},
            *({"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": fragment,
            }} for fragment in fragments),
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, text="".join(f"data: {json.dumps(payload)}\n\n" for payload in payloads))

    patch_client(monkeypatch, handler)
    events = asyncio.run(collect(AnthropicMessagesAdapter().stream_messages(
        messages(), configuration("anthropic-messages"),
        [{"name": "lookup", "parameters": {"properties": {"count": {"type": "integer"}}}}],
    )))
    done = next((event for event in events if isinstance(event, ModelStreamDone)), None)
    assert done is not None, events
    assert done.message.content == (ModelToolCallBlock("call-1", "lookup", expected),)


def test_routed_native_structured_stream_preserves_tool_history_and_images(monkeypatch):
    from backend.app.model_adapter import ModelTextDelta

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=(
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        ))

    patch_client(monkeypatch, handler)
    history = [
        {"role": "user", "content": [
            {"type": "text", "text": "inspect"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}},
        ]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": '{"count":1}'},
        }]},
        {"role": "tool", "tool_call_id": "call-1", "content": "found"},
    ]
    events = asyncio.run(collect(RoutedModelAdapter(ModelAdapterRegistry()).stream_structured_messages(
        history, configuration("anthropic-messages"), [{"name": "lookup", "parameters": {}}],
    )))
    from backend.app.ai.model_adapter import ModelResponseDone
    assert events == [ModelTextDelta("ok"), ModelResponseDone("stop")]
    user, assistant, result = requests[0]["messages"]
    assert user["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "aW1hZ2U="}
    assert assistant["content"] == [{"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"count": 1}}]
    assert result["content"][0]["tool_use_id"] == "call-1"
    assert result["content"][0]["content"] == [{"type": "text", "text": "found"}]


def test_responses_uses_input_and_function_call_output(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        body = "".join([
            'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","model":"o4"}}\n\n',
            'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n',
        ])
        return httpx.Response(200, text=body)

    patch_client(monkeypatch, handler)
    events = asyncio.run(collect(OpenAIResponsesAdapter().stream_messages(messages(), configuration("openai-responses"))))
    assert requests[0].url == "https://provider.test/responses"
    payload = json.loads(requests[0].content)
    assert payload["input"][0]["role"] == "system"
    assert payload["input"][1]["content"][0]["type"] == "input_text"
    assert any(isinstance(event, ModelStreamDone) for event in events)


def test_responses_stream_restores_ordered_blocks_from_output_item_done(monkeypatch):
    def handler(request):
        del request
        payloads = [
            {"type": "response.output_item.added", "output_index": 0, "item": {
                "id": "rs_1", "type": "reasoning", "summary": [],
            }},
            {"type": "response.reasoning_summary_text.delta", "output_index": 0, "item_id": "rs_1", "delta": "reason"},
            {"type": "response.output_item.done", "output_index": 0, "item": {
                "id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "reason"}],
            }},
            {"type": "response.output_item.added", "output_index": 1, "item": {
                "id": "msg_1", "type": "message", "role": "assistant", "content": [],
            }},
            {"type": "response.output_text.delta", "output_index": 1, "item_id": "msg_1", "delta": "answer"},
            {"type": "response.output_item.done", "output_index": 1, "item": {
                "id": "msg_1", "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            }},
            {"type": "response.output_item.added", "output_index": 2, "item": {
                "id": "fc_1", "call_id": "call_1", "type": "function_call", "name": "lookup",
                "arguments": "{}", "namespace": "functions",
            }},
            {"type": "response.output_item.done", "output_index": 2, "item": {
                "id": "fc_1", "call_id": "call_1", "type": "function_call", "name": "lookup",
                "arguments": "{\"key\":\"name\"}", "namespace": "functions",
            }},
            {"type": "response.completed", "response": {
                "id": "resp_1", "model": "o4", "status": "completed",
            }},
        ]
        body = "".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in payloads)
        return httpx.Response(200, text=body)

    patch_client(monkeypatch, handler)
    events = asyncio.run(collect(OpenAIResponsesAdapter().stream_messages(
        messages(), configuration("openai-responses"),
    )))
    done = next(event for event in events if isinstance(event, ModelStreamDone))

    assert done.message.content == (
        ModelThinkingBlock("reason", '{"id":"rs_1","type":"reasoning","summary":[{"type":"summary_text","text":"reason"}]}'),
        ModelTextBlock("answer", text_signature='{"v":1,"id":"msg_1","phase":"final_answer"}'),
        ModelToolCallBlock("call_1|fc_1", "lookup", {"key": "name"}, namespace="functions"),
    )


def test_responses_replays_reasoning_text_signatures_and_tool_namespace():
    history = (
        ModelAssistantMessage(
            (
                ModelThinkingBlock("reason", '{"type":"reasoning","id":"rs_1","summary":[]}'),
                ModelTextBlock("answer", text_signature='{"v":1,"id":"msg_1","phase":"final_answer"}'),
                ModelToolCallBlock("call_1|fc_1", "lookup", {"count": 1}, namespace="plugin"),
            ),
            ModelResponseMetadata(provider="provider", api="openai-responses", model="model-1"),
        ),
        ModelToolResultMessage((ModelTextBlock("done"),), "call_1|fc_1", "lookup"),
    )
    payload, _headers, _aliases = OpenAIResponsesAdapter()._prepare(
        history, configuration("openai-responses"), (), max_tokens=None, stream=True,
    )

    assert payload["input"] == [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {
            "type": "message", "role": "assistant", "content": [{
                "type": "output_text", "text": "answer", "annotations": [],
            }], "status": "completed", "id": "msg_1", "phase": "final_answer",
        },
        {
            "type": "function_call", "id": "fc_1", "call_id": "call_1",
            "name": "lookup", "arguments": '{"count":1}', "namespace": "plugin",
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "done"},
    ]


def test_responses_replays_unsigned_thinking_as_assistant_text():
    history = (ModelAssistantMessage(
        (ModelThinkingBlock("reason without signature"),),
        ModelResponseMetadata(provider="provider", api="openai-responses", model="model-1"),
    ),)

    payload, _headers, _aliases = OpenAIResponsesAdapter()._prepare(
        history, configuration("openai-responses"), (), max_tokens=None, stream=True,
    )

    assert payload["input"] == [{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "reason without signature", "annotations": []}],
        "status": "completed",
        "id": "msg_pi_0",
    }]


def test_responses_stream_reports_invalid_terminal_tool_arguments(monkeypatch):
    def handler(request):
        del request
        payload = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [{
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": "not-json",
                }],
            },
        }
        body = f"event: response.completed\ndata: {json.dumps(payload)}\n\n"
        return httpx.Response(200, text=body)

    patch_client(monkeypatch, handler)
    events = asyncio.run(collect(OpenAIResponsesAdapter().stream_messages(
        messages(), configuration("openai-responses"),
    )))

    assert isinstance(events[-1], ModelStreamError)
    assert "不是有效 JSON" in events[-1].error


def test_responses_complete_restores_reasoning_and_item_signatures(monkeypatch):
    reasoning = {
        "type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "think"}],
        "encrypted_content": "encrypted-reasoning",
    }
    response_message = {
        "type": "message", "id": "msg_1", "phase": "final_answer", "content": [
            {"type": "output_text", "text": "answer"},
        ],
    }
    function_call = {
        "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup",
        "arguments": '{"count":1}', "namespace": "plugin",
    }

    def handler(request):
        del request
        return httpx.Response(200, json={
            "id": "resp_1", "model": "model-1", "status": "completed",
            "output": [reasoning, response_message, function_call],
        })

    patch_client(monkeypatch, handler)
    result = asyncio.run(OpenAIResponsesAdapter().complete_messages(
        messages(), configuration("openai-responses"),
    ))

    assert result.content == (
        ModelThinkingBlock("think", json.dumps(reasoning, separators=(",", ":"))),
        ModelTextBlock("answer", text_signature='{"v":1,"id":"msg_1","phase":"final_answer"}'),
        ModelToolCallBlock("call_1|fc_1", "lookup", {"count": 1}, namespace="plugin"),
    )


@pytest.mark.parametrize("body, detail", [
    ('{"error":{"message":"invalid tool schema"}}', "invalid tool schema"),
    ('{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens","debug":"secret-key"}}', "max_output_tokens"),
])
def test_responses_complete_rejects_failure_bodies_with_http_200(monkeypatch, body, detail):
    def handler(request):
        del request
        return httpx.Response(200, json=json.loads(body))

    patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match=detail) as error:
        asyncio.run(OpenAIResponsesAdapter().complete_messages(
            messages(), configuration("openai-responses"),
        ))
    assert "secret-key" not in str(error.value)


def test_azure_responses_uses_v1_endpoint_and_api_key_header(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=(
            'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_azure","model":"deployment"}}\n\n'
            'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        ))

    patch_client(monkeypatch, handler)
    config = configuration("azure-openai-responses", "https://meido.openai.azure.com", "azure")
    events = asyncio.run(collect(AzureOpenAIResponsesAdapter().stream_messages(messages(), config)))

    assert requests[0].url == "https://meido.openai.azure.com/openai/v1/responses"
    assert requests[0].headers["api-key"] == "secret-key"
    assert "authorization" not in requests[0].headers
    done = next(event for event in events if isinstance(event, ModelStreamDone))
    assert done.message.metadata.api == "azure-openai-responses"


def test_openai_codex_responses_sends_account_header_and_parses_native_events(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=(
            'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_codex","model":"codex-model"}}\n\n'
            'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        ))

    patch_client(monkeypatch, handler)
    config = configuration("openai-codex-responses", "https://codex.provider.test/backend-api/codex", "openai-codex")
    config.apiOptions = {"accountId": "acct-1"}
    events = asyncio.run(collect(OpenAICodexResponsesAdapter().stream_messages(messages(), config)))

    assert requests[0].url == "https://codex.provider.test/backend-api/codex/responses"
    assert requests[0].headers["authorization"] == "Bearer secret-key"
    assert requests[0].headers["chatgpt-account-id"] == "acct-1"
    done = next(event for event in events if isinstance(event, ModelStreamDone))
    assert done.message.metadata.api == "openai-codex-responses"


def test_vertex_uses_project_location_and_api_key_header(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=(
            'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1}}\n\n'
        ))

    patch_client(monkeypatch, handler)
    config = configuration("google-vertex", "https://us-central1-aiplatform.googleapis.com", "google-vertex")
    config.apiOptions = {"project": "meido-test", "location": "us-central1"}
    events = asyncio.run(collect(GoogleVertexAdapter().stream_messages(messages(), config)))

    assert requests[0].url == "https://us-central1-aiplatform.googleapis.com/v1/projects/meido-test/locations/us-central1/publishers/google/models/model-1:streamGenerateContent?alt=sse"
    assert requests[0].headers["x-goog-api-key"] == "secret-key"
    done = next(event for event in events if isinstance(event, ModelStreamDone))
    assert done.message.metadata.api == "google-vertex"


def test_vertex_uses_adc_when_api_key_is_empty_and_normalizes_v1_base_url(monkeypatch):
    from backend.app import google_vertex_adapter

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=(
            'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]},"finishReason":"STOP"}]}\n\n'
        ))

    patch_client(monkeypatch, handler)
    monkeypatch.setattr(google_vertex_adapter, "_google_adc_token", lambda: "adc-access-token")
    config = configuration("google-vertex", "https://us-central1-aiplatform.googleapis.com/v1", "google-vertex")
    config.apiKey = ""
    config.apiOptions = {"project": "meido-test", "location": "us-central1"}

    events = asyncio.run(collect(GoogleVertexAdapter().stream_messages(messages(), config)))

    assert requests[0].url == "https://us-central1-aiplatform.googleapis.com/v1/projects/meido-test/locations/us-central1/publishers/google/models/model-1:streamGenerateContent?alt=sse"
    assert requests[0].headers["authorization"] == "Bearer adc-access-token"
    assert requests[0].headers["x-goog-user-project"] == "meido-test"
    assert any(isinstance(event, ModelStreamDone) for event in events)


def test_google_uses_contents_parts_and_native_sse(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        body = 'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1}}\n\n'
        return httpx.Response(200, text=body)

    patch_client(monkeypatch, handler)
    events = asyncio.run(collect(GoogleGenerativeAdapter().stream_messages(messages(), configuration("google-generative"))))
    assert requests[0].url == "https://provider.test/models/model-1:streamGenerateContent?alt=sse"
    payload = json.loads(requests[0].content)
    assert payload["contents"][0]["role"] == "user"
    assert payload["contents"][0]["parts"][0]["text"] == "hello"
    assert any(isinstance(event, ModelStreamDone) for event in events)


def test_routed_openai_legacy_stream_preserves_text_deltas(monkeypatch):
    def handler(request):
        del request
        body = 'data: {"choices":[{"delta":{"content":"legacy"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body)

    patch_client(monkeypatch, handler)
    routed = RoutedModelAdapter(ModelAdapterRegistry())
    events = asyncio.run(collect(routed.stream_messages(
        [{"role": "user", "content": "hello"}], configuration("openai-completions"),
    )))

    assert events == ["legacy"]


def test_pi_messages_preserves_done_and_routes_from_registry(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        body = "".join([
            'data: {"type":"start"}\n\n',
            'data: {"type":"text_start","contentIndex":0}\n\n',
            'data: {"type":"text_delta","contentIndex":0,"delta":"ok"}\n\n',
            'data: {"type":"text_end","contentIndex":0,"content":"ok"}\n\n',
            'data: {"type":"done","reason":"stop","usage":{"input":1,"output":1}}\n\n',
        ])
        return httpx.Response(200, text=body)

    patch_client(monkeypatch, handler)
    registry = ModelAdapterRegistry()
    routed = RoutedModelAdapter(registry)
    events = asyncio.run(collect(routed.stream_messages(
        [{"role": "user", "content": "hello"}], configuration("pi-messages"),
    )))
    assert requests[0].url == "https://provider.test/messages"
    payload = json.loads(requests[0].content)
    assert payload["context"]["messages"][0]["role"] == "user"
    assert any(event == "ok" for event in events)


def test_pi_messages_puts_tools_in_system_transcript(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text='data: {"type":"error","reason":"error","errorMessage":"stop"}\n\n')

    patch_client(monkeypatch, handler)
    asyncio.run(collect(PiMessagesAdapter().stream_messages(
        messages(), configuration("pi-messages"),
        [{"name": "todo.write", "description": "write", "parameters": {"type": "object", "properties": {}}}],
    )))
    payload = json.loads(requests[0].content)
    assert payload["context"]["messages"][0]["role"] == "system"
    assert payload["context"]["messages"][0]["toolsAdded"][0]["name"] == "todo.write"
    assert "tools" not in payload["context"]


def test_pi_messages_preserves_tool_result_diagnostics_without_sending_nested_calls():
    result = ModelToolResultMessage(
        content=(ModelTextBlock("done"),),
        tool_call_id="call-1",
        tool_name="parent",
        details={"status": "succeeded"},
        usage=_usage({"input": 2, "output": 1, "cost": {"input": 0.01, "total": 0.02}}),
        nested_calls=(ModelToolCallBlock("nested-1", "child", {"key": "value"}),),
        timestamp=1234,
    )

    wire = _message(result)

    assert wire["details"] == {"status": "succeeded"}
    assert wire["usage"]["cost"] == {
        "input": 0.01,
        "output": 0.0,
        "cache_read": 0.0,
        "cache_write": 0.0,
        "total": 0.02,
    }
    assert wire["timestamp"] == 1234
    assert "nestedCalls" not in wire
    assert _usage({"cost": {"input": 0.01}}).cost == ModelCost(input=0.01)


def test_bedrock_converse_stream_uses_event_stream_endpoint(monkeypatch):
    requests = []

    def frame(payload):
        encoded = json.dumps(payload).encode()
        return struct.pack(">III", 16 + len(encoded), 0, 0) + encoded + struct.pack(">I", 0)

    def handler(request):
        requests.append(request)
        body = b"".join([
            frame({"messageStart": {"role": "assistant"}}),
            frame({"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"reasoningText": {"text": "reason", "signature": "sig"}}}}}),
            frame({"contentBlockStart": {"contentBlockIndex": 1, "start": {}}}),
            frame({"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"text": "ok"}}}),
            frame({"messageStop": {"stopReason": "end_turn"}}),
        ])
        return httpx.Response(200, content=body)

    patch_client(monkeypatch, handler)
    events = asyncio.run(collect(BedrockConverseAdapter().stream_messages(messages(), configuration("bedrock-converse"))))
    assert requests[0].url == "https://provider.test/model/model-1/converse-stream"
    done = next(event for event in events if isinstance(event, ModelStreamDone))
    assert done.message.content == (ModelThinkingBlock("reason", "sig"), ModelTextBlock("ok"))


def test_bedrock_complete_reads_top_level_stop_reason(monkeypatch):
    def handler(request):
        del request
        return httpx.Response(200, json={
            "output": {"message": {"role": "assistant", "content": [{"text": "use tool"}]}},
            "stopReason": "tool_use",
        })

    patch_client(monkeypatch, handler)
    result = asyncio.run(BedrockConverseAdapter().complete_messages(
        messages(), configuration("bedrock-converse"),
    ))
    assert result.metadata.stop_reason == "tool_use"


def test_mistral_builds_its_own_tool_wire_format_and_restores_internal_names(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        name = payload["tools"][0]["function"]["name"]
        chunk = {"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call-1",
            "function": {"name": name, "arguments": "{\"title\":\"x\"}"},
        }]}, "finish_reason": "tool_calls"}]}
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=body)

    patch_client(monkeypatch, handler)
    events = asyncio.run(collect(MistralConversationsAdapter().stream_messages(
        messages(), configuration("mistral-conversations", "https://provider.test"),
        [{"name": "todo.write", "description": "write", "parameters": {"type": "object", "properties": {}}}],
    )))
    assert requests[0].url == "https://provider.test/v1/chat/completions"
    payload = json.loads(requests[0].content)
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] != "todo.write"
    assert payload["messages"][0]["role"] == "system"
    done = next(event for event in events if isinstance(event, ModelStreamDone))
    call = next(block for block in done.message.content if isinstance(block, ModelToolCallBlock))
    assert call.name == "todo.write"
    assert call.arguments == {"title": "x"}


def test_mistral_only_sends_images_to_vision_models():
    history = [ModelUserMessage((ModelImageBlock("image/png", "aW1hZ2U="),))]

    _configuration, text_model, _headers, _aliases = MistralConversationsAdapter()._prepare(
        history, configuration("mistral-conversations"), (), stream=True, max_tokens=None,
    )
    assert text_model["messages"][0]["content"] == "(image omitted: model does not support images)"

    vision_config = configuration("mistral-conversations")
    vision_config.model = "pixtral-large-2411"
    _configuration, vision_model, _headers, _aliases = MistralConversationsAdapter()._prepare(
        history, vision_config, (), stream=True, max_tokens=None,
    )
    assert vision_model["messages"][0]["content"][0]["type"] == "image_url"


@pytest.mark.parametrize("adapter, api", [
    (AnthropicMessagesAdapter(), "anthropic-messages"),
    (MistralConversationsAdapter(), "mistral-conversations"),
    (GoogleGenerativeAdapter(), "google-generative"),
    (BedrockConverseAdapter(), "bedrock-converse"),
])
def test_native_non_streaming_provider_errors_are_not_empty_successes(monkeypatch, adapter, api):
    def handler(request):
        del request
        return httpx.Response(200, json={"error": {"message": "invalid tool schema"}})

    patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="invalid tool schema"):
        asyncio.run(adapter.complete_messages(messages(), configuration(api)))


@pytest.mark.parametrize("adapter, api, payload", [
    (
        GoogleGenerativeAdapter(),
        "google-generative",
        {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
    ),
    (
        AnthropicMessagesAdapter(),
        "anthropic-messages",
        {"content": [{"type": "text", "text": "ok"}]},
    ),
    (
        BedrockConverseAdapter(),
        "bedrock-converse",
        {"output": {"message": {"content": [{"text": "ok"}]} }},
    ),
])
def test_native_non_streaming_requires_provider_finish_reason(monkeypatch, adapter, api, payload):
    def handler(request):
        del request
        return httpx.Response(200, json=payload)

    patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="finishReason|stop_reason|stopReason"):
        asyncio.run(adapter.complete_messages(messages(), configuration(api)))


@pytest.mark.parametrize("adapter, api, payload", [
    (
        GoogleGenerativeAdapter(),
        "google-generative",
        {"candidates": [{"content": {"parts": [{"text": "blocked"}]}, "finishReason": "SAFETY"}]},
    ),
    (
        AnthropicMessagesAdapter(),
        "anthropic-messages",
        {"content": [{"type": "text", "text": "refused"}], "stop_reason": "refusal"},
    ),
    (
        BedrockConverseAdapter(),
        "bedrock-converse",
        {"output": {"message": {"content": [{"text": "blocked"}]}}, "stopReason": "guardrail_intervened"},
    ),
    (
        MistralConversationsAdapter(),
        "mistral-conversations",
        {"choices": [{"message": {"content": "blocked"}, "finish_reason": "error"}]},
    ),
])
def test_native_non_streaming_rejects_provider_error_finish_reason(monkeypatch, adapter, api, payload):
    def handler(request):
        del request
        return httpx.Response(200, json=payload)

    patch_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="finish|stop_reason|stopReason"):
        asyncio.run(adapter.complete_messages(messages(), configuration(api)))


def test_pi_messages_repairs_tool_history_and_serializes_slotted_metadata():
    call = ModelToolCallBlock("bad|tool/id", "lookup", {"key": "value"})
    history = (
        ModelAssistantMessage(
            (ModelThinkingBlock("reason", "signature"), call),
            ModelResponseMetadata(model="model-1", provider="source", stop_reason="tool_use"),
        ),
        ModelToolResultMessage((ModelTextBlock("found"),), call.id, call.name),
        ModelToolResultMessage((ModelTextBlock("orphan"),), "unknown", "lookup"),
    )
    _, _, payload = PiMessagesAdapter()._prepare(history, configuration("pi-messages"), (), stream=True, max_tokens=None)
    wire = payload["context"]["messages"]
    assistant, result = wire
    wire_call = next(block for block in assistant["content"] if block["type"] == "toolCall")
    assert len(wire_call["id"]) <= 64
    assert result["toolCallId"] == wire_call["id"]
    assert result["content"][0]["text"] == "found"
    assert assistant["metadata"]["provider"] == "source"
    assert assistant["metadata"]["model"] == "model-1"


def test_bedrock_repairs_history_and_preserves_supported_reasoning(monkeypatch):
    from backend.app.bedrock_adapter import BedrockConverseAdapter

    call = ModelToolCallBlock("bad|tool/id", "lookup", {"key": "value"})
    history = (
        ModelAssistantMessage((ModelThinkingBlock("reason", "signature"), call), ModelResponseMetadata(
            provider="anthropic", api="bedrock-converse", model="anthropic.claude-3-7-sonnet",
        )),
        ModelToolResultMessage((ModelImageBlock("image/tiff", "aW1hZ2U="),), call.id, call.name),
        ModelToolResultMessage((ModelTextBlock("orphan"),), "unknown", "lookup"),
    )
    config = configuration("bedrock-converse", provider="anthropic")
    config.model = "anthropic.claude-3-7-sonnet"
    _, _, payload, _ = BedrockConverseAdapter()._prepare(history, config, (), None)
    assistant, result = payload["messages"]
    reasoning = next(block for block in assistant["content"] if "reasoningContent" in block)
    tool = next(block for block in assistant["content"] if "toolUse" in block)
    tool_result = result["content"][0]["toolResult"]
    assert reasoning["reasoningContent"]["reasoningText"]["signature"] == "signature"
    assert len(tool["toolUse"]["toolUseId"]) <= 64
    assert tool_result["toolUseId"] == tool["toolUse"]["toolUseId"]
    assert tool_result["content"][0]["text"] == "[image omitted: image/tiff]"


def test_bedrock_groups_consecutive_tool_results_and_omits_empty_assistant(monkeypatch):
    from backend.app.bedrock_adapter import BedrockConverseAdapter

    first = ModelToolCallBlock("call-1", "first", {"nested": {"kept": 2}})
    second = ModelToolCallBlock("call-2", "second", {})
    history = (
        ModelAssistantMessage((first, second)),
        ModelToolResultMessage((ModelTextBlock("one"),), first.id, first.name),
        ModelToolResultMessage((ModelTextBlock("two"),), second.id, second.name),
        ModelAssistantMessage(()),
    )

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
        })

    patch_client(monkeypatch, handler)
    asyncio.run(BedrockConverseAdapter().complete_messages(
        history, configuration("bedrock-converse"), (),
    ))
    payload = requests[0]

    assert len(payload["messages"]) == 2
    assert payload["messages"][1]["role"] == "user"
    assert payload["messages"][0]["content"][0]["toolUse"]["input"] == {"nested": {"kept": 2}}
    assert [item["toolResult"]["toolUseId"] for item in payload["messages"][1]["content"]] == [
        "call-1", "call-2",
    ]


def test_bedrock_rejects_empty_tool_argument_keys_before_request(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
        })

    patch_client(monkeypatch, handler)
    history = (ModelAssistantMessage((ModelToolCallBlock("call-1", "first", {"": "value"}),)),)

    with pytest.raises(ValueError, match="对象键不能为空"):
        asyncio.run(BedrockConverseAdapter().complete_messages(
            history, configuration("bedrock-converse"), (),
        ))
    assert requests == []


def test_bedrock_omits_reasoning_signature_for_non_anthropic_models(monkeypatch):
    from backend.app.bedrock_adapter import BedrockConverseAdapter

    history = (ModelAssistantMessage(
        (ModelThinkingBlock("reason", "opaque-signature"),),
        ModelResponseMetadata(provider="provider", api="bedrock-converse", model="model-1"),
    ),)

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
        })

    patch_client(monkeypatch, handler)
    asyncio.run(BedrockConverseAdapter().complete_messages(
        history, configuration("bedrock-converse"), (),
    ))
    payload = requests[0]

    reasoning = payload["messages"][0]["content"][0]["reasoningContent"]["reasoningText"]
    assert reasoning == {"text": "reason"}


def test_google_replays_thinking_as_native_thought_part():
    from backend.app.google_adapter import GoogleGenerativeAdapter

    history = (ModelAssistantMessage(
        (
            ModelThinkingBlock("reason", "thinking-signature"),
            ModelTextBlock("answer", text_signature="text-signature"),
            ModelToolCallBlock("call-1", "lookup", {"count": 1}, thought_signature="tool-signature"),
        ),
        ModelResponseMetadata(provider="provider", api="google-generative", model="model-1"),
    ),)
    _, _, payload, _ = GoogleGenerativeAdapter()._prepare(history, configuration("google-generative"), (), stream=True, max_tokens=None)
    parts = payload["contents"][0]["parts"]
    assert parts == [
        {"text": "reason", "thought": True, "thoughtSignature": "thinking-signature"},
        {"text": "answer", "thoughtSignature": "text-signature"},
        {"functionCall": {"name": "lookup", "args": {"count": 1}, "id": "call-1"}, "thoughtSignature": "tool-signature"},
    ]


def test_google_groups_consecutive_tool_results_with_output_and_error_fields(monkeypatch):
    from backend.app.google_adapter import GoogleGenerativeAdapter

    history = (
        ModelAssistantMessage((ModelToolCallBlock("call-1", "lookup", {}), ModelToolCallBlock("call-2", "lookup", {}))),
        ModelToolResultMessage((ModelTextBlock("ok"),), "call-1", "lookup"),
        ModelToolResultMessage((ModelTextBlock("failed"),), "call-2", "lookup", is_error=True),
    )

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "candidates": [{
                "content": {"parts": [{"text": "ok"}]},
                "finishReason": "STOP",
            }],
        })

    patch_client(monkeypatch, handler)
    asyncio.run(GoogleGenerativeAdapter().complete_messages(
        history, configuration("google-generative"), (),
    ))
    payload = requests[0]

    assert len(payload["contents"]) == 2
    responses = [part["functionResponse"]["response"] for part in payload["contents"][1]["parts"]]
    assert responses == [{"output": "ok"}, {"error": "failed"}]


def test_google_stream_preserves_order_and_thought_signatures(monkeypatch):
    def handler(request):
        del request
        return httpx.Response(200, text=(
            'data: {"candidates":[{"content":{"parts":[{"text":"think","thought":true,"thoughtSignature":"sig-think"}]}}]}\n\n'
            'data: {"candidates":[{"content":{"parts":[{"text":"answer","thoughtSignature":"sig-text"}]}}]}\n\n'
            'data: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"lookup","args":{"count":1},"id":"call-1"},"thoughtSignature":"sig-tool"}]},"finishReason":"STOP"}]}\n\n'
        ))

    patch_client(monkeypatch, handler)
    events = asyncio.run(collect(GoogleGenerativeAdapter().stream_messages(
        messages(), configuration("google-generative"),
    )))
    done = next(event for event in events if isinstance(event, ModelStreamDone))

    assert done.message.content == (
        ModelThinkingBlock("think", "sig-think"),
        ModelTextBlock("answer", text_signature="sig-text"),
        ModelToolCallBlock("call-1", "lookup", {"count": 1}, thought_signature="sig-tool"),
    )


def test_native_protocols_alias_restricted_tool_names_in_schema_and_history():
    from backend.app.anthropic_adapter import AnthropicMessagesAdapter
    from backend.app.bedrock_adapter import BedrockConverseAdapter
    from backend.app.google_adapter import GoogleGenerativeAdapter
    from backend.app.openai_responses_adapter import OpenAIResponsesAdapter

    tool = {"name": "todo.write", "description": "write", "parameters": {"type": "object", "properties": {}}}
    call = ModelToolCallBlock("call-1", "todo.write", {})
    history = (
        ModelSystemMessage((ModelTextBlock("system"),)),
        ModelAssistantMessage((call,)),
        ModelToolResultMessage((ModelTextBlock("done"),), call.id, call.name),
    )

    responses, _headers, _aliases = OpenAIResponsesAdapter()._prepare(
        history, configuration("openai-responses"), [tool], max_tokens=None, stream=True,
    )
    responses_name = responses["tools"][0]["name"]
    assert responses_name != tool["name"]
    assert responses["input"][1]["name"] == responses_name

    anthropic, _headers, _aliases = AnthropicMessagesAdapter()._prepare(
        history, configuration("anthropic-messages"), [tool], max_tokens=None, stream=True,
    )
    anthropic_name = anthropic["tools"][0]["name"]
    assert anthropic_name != tool["name"]
    assert anthropic["messages"][0]["content"][0]["name"] == anthropic_name

    _url, _headers, google, _aliases = GoogleGenerativeAdapter()._prepare(
        history, configuration("google-generative"), [tool], stream=True, max_tokens=None,
    )
    google_name = google["tools"][0]["functionDeclarations"][0]["name"]
    assert google_name != tool["name"]
    assert google["contents"][0]["parts"][0]["functionCall"]["name"] == google_name

    _url, _headers, bedrock, _aliases = BedrockConverseAdapter()._prepare(
        history, configuration("bedrock-converse"), [tool], None,
    )
    bedrock_name = bedrock["toolConfig"]["tools"][0]["toolSpec"]["name"]
    assert bedrock_name != tool["name"]
    bedrock_call = next(block["toolUse"] for block in bedrock["messages"][0]["content"] if "toolUse" in block)
    assert bedrock_call["name"] == bedrock_name

    _config, mistral, _headers, _aliases = MistralConversationsAdapter()._prepare(
        history, configuration("mistral-conversations"), [tool], stream=True, max_tokens=None,
    )
    mistral_name = mistral["tools"][0]["function"]["name"]
    assert mistral_name != tool["name"]
    assert mistral["messages"][1]["tool_calls"][0]["function"]["name"] == mistral_name


def test_tool_result_images_are_preserved_as_placeholders_in_text_only_protocol_fields():
    call = ModelToolCallBlock("call-1", "inspect", {})
    history = (
        ModelSystemMessage((ModelTextBlock("system"),)),
        ModelAssistantMessage((call,)),
        ModelToolResultMessage(
            (ModelTextBlock("inspection complete"), ModelImageBlock("image/png", "aW1hZ2U=")),
            call.id,
            call.name,
        ),
    )

    _configuration, mistral, _headers, _aliases = MistralConversationsAdapter()._prepare(
        history, configuration("mistral-conversations"), (), stream=True, max_tokens=None,
    )
    assert "[image omitted: image/png]" in mistral["messages"][-1]["content"]

    _url, _headers, google, _aliases = GoogleGenerativeAdapter()._prepare(
        history, configuration("google-generative"), (), stream=True, max_tokens=None,
    )
    google_result = google["contents"][-1]["parts"][0]["functionResponse"]["response"]["output"]
    assert "[image omitted: image/png]" in google_result

    responses, _headers, _aliases = OpenAIResponsesAdapter()._prepare(
        history, configuration("openai-responses"), (), max_tokens=None, stream=True,
    )
    assert "[image omitted: image/png]" in responses["input"][-1]["output"]


def test_tool_schema_normalization_rejects_invalid_provider_neutral_shapes():
    with pytest.raises(ValueError, match="类型必须是 function"):
        normalize_tools([{"type": "computer", "name": "shell"}])
    with pytest.raises(ValueError, match="properties 成员必须是对象"):
        normalize_tools([{"name": "nested", "parameters": {
            "properties": {"options": {"properties": {"count": "integer"}}},
        }}])
    with pytest.raises(ValueError, match="required 必须引用 properties"):
        normalize_tools([{"name": "nested", "parameters": {
            "properties": {"options": {"properties": {}, "required": ["missing"]}},
        }}])


def test_tool_schema_normalization_accepts_boolean_property_schemas():
    normalized = normalize_tools([{"name": "flags", "parameters": {
        "type": "object",
        "properties": {"enabled": True, "disabled": False},
    }}])

    assert normalized[0]["parameters"]["properties"] == {"enabled": True, "disabled": False}


def test_all_registered_protocols_reject_invalid_tool_schemas_before_http(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    patch_client(monkeypatch, handler)
    routed = RoutedModelAdapter(ModelAdapterRegistry())
    invalid_tools = [{
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {"type": "object", "properties": {"count": "integer"}},
        },
    }]
    apis = (
        "openai-completions",
        "openai-responses",
        "azure-openai-responses",
        "openai-codex-responses",
        "anthropic-messages",
        "google-generative",
        "google-vertex",
        "mistral-conversations",
        "bedrock-converse",
        "pi-messages",
    )

    for api in apis:
        with pytest.raises(ValueError, match="schema.properties 成员必须是对象"):
            asyncio.run(collect(routed.stream_rich_messages(
                messages(), configuration(api), invalid_tools,
            )))

    assert requests == []


@pytest.mark.parametrize("schema, message", [
    ({"properties": {"value": {"type": "unknown"}}}, "type 必须是有效类型"),
    ({"properties": {"value": {"type": "string", "enum": "single"}}}, "enum 必须是非空数组"),
])
def test_tool_schema_normalization_rejects_invalid_keyword_values(schema, message):
    with pytest.raises(ValueError, match=message):
        normalize_tools([{"name": "invalid", "parameters": schema}])


def test_google_tool_schema_drops_only_unrepresentable_false_additional_properties():
    schema = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }

    _url, _headers, payload, _aliases = GoogleGenerativeAdapter()._prepare(
        messages(), configuration("google-generative"), [{"name": "lookup", "parameters": schema}],
        stream=True, max_tokens=None,
    )

    parameters = payload["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "additionalProperties" not in parameters
    assert "additionalProperties" not in parameters["properties"]["nested"]
    assert schema["additionalProperties"] is False


def test_google_tool_schema_converts_nullable_types_without_mutating_input():
    schema = {
        "type": "object",
        "properties": {"date": {"type": ["string", "null"], "format": "date"}},
    }

    _url, _headers, payload, _aliases = GoogleGenerativeAdapter()._prepare(
        messages(), configuration("google-generative"), [{"name": "lookup", "parameters": schema}],
        stream=True, max_tokens=None,
    )

    parameters = payload["tools"][0]["functionDeclarations"][0]["parameters"]
    assert parameters["properties"]["date"] == {"type": "string", "format": "date", "nullable": True}
    assert schema["properties"]["date"]["type"] == ["string", "null"]


@pytest.mark.parametrize("property_schema, message", [
    (True, "不支持布尔子 schema"),
    ({"type": "array", "items": [{"type": "string"}]}, "不支持 items 元组"),
    ({"type": "string", "enum": [1, 2]}, "enum 只支持字符串"),
])
def test_google_tool_schema_rejects_unrepresentable_shapes(property_schema, message):
    with pytest.raises(ValueError, match=message):
        GoogleGenerativeAdapter()._prepare(
            messages(), configuration("google-generative"), [{
                "name": "lookup",
                "parameters": {"type": "object", "properties": {"value": property_schema}},
            }],
            stream=True, max_tokens=None,
        )


def test_google_tool_schema_rejects_unsupported_keywords_before_request():
    with pytest.raises(ValueError, match=r"Google.*\$ref"):
        GoogleGenerativeAdapter()._prepare(
            messages(), configuration("google-generative"), [{
                "name": "lookup",
                "parameters": {"type": "object", "properties": {"value": {"$ref": "#/$defs/value"}}},
            }],
            stream=True, max_tokens=None,
        )


@pytest.mark.parametrize("nested", [
    {"type": "array", "items": {"properties": {"count": "integer"}}},
    {"additionalProperties": {"properties": {"count": "integer"}}},
    {"anyOf": [{"properties": {"count": "integer"}}]},
    {"oneOf": [{"properties": {"count": "integer"}}]},
    {"allOf": [{"properties": {"count": "integer"}}]},
    {"$defs": {"options": {"properties": {"count": "integer"}}}},
])
def test_tool_schema_rejects_invalid_properties_in_schema_children(nested):
    with pytest.raises(ValueError, match="properties 成员必须是对象"):
        normalize_tools([{"name": "nested", "parameters": {
            "properties": {"options": nested},
        }}])


def test_native_adapter_does_not_hide_invalid_tool_names_before_validation():
    with pytest.raises(ValueError, match="有效名称"):
        GoogleGenerativeAdapter()._prepare(
            messages(), configuration("google-generative"), [{"parameters": {}}],
            stream=True, max_tokens=None,
        )


def test_tool_schema_keeps_boolean_children_and_data_values():
    schema = {
        "properties": {
            "enabled": True,
            "disabled": False,
            "options": {
                "items": True, "additionalProperties": False,
                "anyOf": [True, {"type": "object", "additionalProperties": False}],
                "default": {"properties": {"count": "integer"}},
            },
        },
    }
    result = normalize_tools([{"name": "lookup", "parameters": schema}])
    assert result[0]["parameters"]["properties"] == schema["properties"]


def test_native_adapter_rejects_duplicate_tools_and_invalid_system_additions():
    with pytest.raises(ValueError, match="名称重复"):
        GoogleGenerativeAdapter()._prepare(
            messages(), configuration("google-generative"), [{"name": "lookup"}, {"name": "lookup"}],
            stream=True, max_tokens=None,
        )
    with pytest.raises(ValueError, match="有效名称"):
        GoogleGenerativeAdapter()._prepare(
            [ModelSystemMessage(tools_added=({"parameters": {}},))], configuration("google-generative"), (),
            stream=True, max_tokens=None,
        )


def test_routed_openai_rich_completion_keeps_tool_arguments():
    class RichAdapter:
        async def complete_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None):
            return messages, configuration, tools, max_tokens

        async def complete_messages(self, messages, configuration, *, max_tokens=None):
            raise AssertionError("legacy text completion must not handle rich messages")

    config = configuration("openai-completions")
    registry = ModelAdapterRegistry()
    registry._adapters["openai-completions"] = RichAdapter()
    tools = [{"name": "todo.write", "parameters": {"type": "object", "properties": {}}}]
    messages_in = [ModelUserMessage((ModelTextBlock("hello"),))]

    result = asyncio.run(RoutedModelAdapter(registry).complete_rich_messages(
        messages_in,
        config,
        tools,
        max_tokens=64,
    ))

    assert result == (messages_in, config, tools, 64)


async def collect(source):
    return [event async for event in source]
