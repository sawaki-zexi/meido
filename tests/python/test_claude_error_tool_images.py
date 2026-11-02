"""Synthetic request contracts; these tests never construct a transport."""

import base64
from copy import deepcopy
import struct
import zlib

import httpx
import pytest

from backend.app.ai.anthropic_adapter import AnthropicMessagesAdapter
from backend.app.ai.bedrock_adapter import BedrockConverseAdapter
from backend.app.ai.model_types import (
    ModelAssistantMessage, ModelImageBlock, ModelSystemMessage, ModelTextBlock,
    ModelThinkingBlock, ModelToolCallBlock, ModelToolResultMessage, ModelUserMessage,
)
from backend.app.models import ModelConfiguration


def image(pixel):
    def chunk(kind, value):
        data = kind + value
        return struct.pack(">I", len(value)) + data + struct.pack(">I", zlib.crc32(data))

    png = b"\x89PNG\r\n\x1a\n" + b"".join((
        chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)),
        chunk(b"IDAT", zlib.compress(b"\x00" + bytes(pixel))),
        chunk(b"IEND", b""),
    ))
    return ModelImageBlock("image/png", base64.b64encode(png).decode("ascii"))


FIRST_IMAGE = image((255, 0, 0, 255))
SECOND_IMAGE = image((0, 255, 0, 255))
SUCCESS_IMAGE = image((0, 0, 255, 255))
LAST_IMAGE = image((255, 255, 0, 255))


@pytest.fixture(autouse=True)
def forbid_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Request preparation must not construct an HTTP client")

    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)


@pytest.fixture(params=["anthropic-messages", "bedrock-converse"])
def api(request):
    return request.param


def prepare(api, history, *, model="anthropic.claude-sonnet-4-5"):
    config = ModelConfiguration(providerId="synthetic", provider="anthropic", api=api,
        baseUrl="https://provider.test", model=model, apiKey="synthetic-key")
    original = deepcopy(history)
    if api == "anthropic-messages":
        adapter = AnthropicMessagesAdapter()
        payload, _, _ = adapter._prepare(history, config, (), max_tokens=None, stream=True)
    else:
        adapter = BedrockConverseAdapter()
        _, _, payload, _ = adapter._prepare(history, config, (), None)
    projected = adapter.prepare_input(history, config)
    assert [item for item in projected["messages"] if item["role"] != "system"] == payload["messages"]
    assert history == original
    return payload["messages"]


def kind(api, block):
    return block["type"] if api == "anthropic-messages" else next(iter(block))


def result(api, block):
    return block if api == "anthropic-messages" else block["toolResult"]


def call_id(api, block):
    return block["id"] if api == "anthropic-messages" else block["toolUse"]["toolUseId"]


def call_name(api, block):
    return block["name"] if api == "anthropic-messages" else block["toolUse"]["name"]


def image_bytes(api, block):
    return block["source"]["data"] if api == "anthropic-messages" else block["image"]["source"]["bytes"]


def assert_error(api, block, expected_id):
    value = result(api, block)
    assert value["tool_use_id" if api == "anthropic-messages" else "toolUseId"] == expected_id
    assert value["is_error" if api == "anthropic-messages" else "status"] == (
        True if api == "anthropic-messages" else "error"
    )
    assert value["content"]
    assert all(kind(api, item) == "text" for item in value["content"])
    return "".join(item["text"] for item in value["content"])


def test_error_images_follow_the_entire_mixed_tool_batch(api):
    first = ModelToolCallBlock("unsafe|first/id", "browser.capture", {})
    success = ModelToolCallBlock("second", "success", {})
    last = ModelToolCallBlock("last", "last", {})
    history = (
        ModelAssistantMessage((first, success, last)),
        ModelToolResultMessage((ModelTextBlock("capture failed\n"), FIRST_IMAGE,
            ModelTextBlock("retry detail\n"), SECOND_IMAGE), first.id, first.name, True),
        ModelSystemMessage((ModelTextBlock("synthetic tool update"),)),
        ModelToolResultMessage((ModelTextBlock("success"), SUCCESS_IMAGE), success.id, success.name),
        ModelToolResultMessage((LAST_IMAGE,), last.id, last.name, True),
    )

    assistant, response = prepare(api, history)
    content = response["content"]
    result_kind = "tool_result" if api == "anthropic-messages" else "toolResult"
    assert response["role"] == "user"
    assert [kind(api, item) for item in content] == [result_kind] * 3 + [
        "text", "image", "image", "text", "image",
    ]
    first_id = call_id(api, assistant["content"][0])
    error_text = assert_error(api, content[0], first_id)
    assert "capture failed\n" in error_text and "retry detail\n" in error_text
    assert assert_error(api, content[2], "last").strip()
    assert first_id in content[3]["text"]
    assert call_name(api, assistant["content"][0]) in content[3]["text"]
    assert "last" in content[6]["text"]
    assert [image_bytes(api, content[i]) for i in (4, 5, 7)] == [
        FIRST_IMAGE.data_base64, SECOND_IMAGE.data_base64, LAST_IMAGE.data_base64,
    ]
    success_result = result(api, content[1])
    assert [kind(api, item) for item in success_result["content"]] == ["text", "image"]
    assert image_bytes(api, success_result["content"][1]) == SUCCESS_IMAGE.data_base64
    if api == "anthropic-messages":
        assert "is_error" not in success_result
    else:
        assert success_result["status"] == "success"


def test_error_images_stay_with_their_own_tool_batch(api):
    first = ModelToolCallBlock("first", "first", {})
    second = ModelToolCallBlock("second", "second", {})
    history = (
        ModelAssistantMessage((first,)),
        ModelToolResultMessage((ModelTextBlock("first failed"), FIRST_IMAGE), first.id, first.name, True),
        ModelAssistantMessage((ModelTextBlock("continue"),)),
        ModelUserMessage((ModelTextBlock("next request"),)),
        ModelAssistantMessage((second,)),
        ModelToolResultMessage((ModelTextBlock("second failed"), SECOND_IMAGE), second.id, second.name, True),
    )

    messages = prepare(api, history)
    assert len(messages) == 6
    for index, expected_id, expected_image in ((1, "first", FIRST_IMAGE), (5, "second", SECOND_IMAGE)):
        content = messages[index]["content"]
        assert len(content) == 3
        assert_error(api, content[0], expected_id)
        assert expected_id in content[1]["text"]
        assert image_bytes(api, content[2]) == expected_image.data_base64
    assert messages[3]["content"] == ([{"type": "text", "text": "next request"}]
        if api == "anthropic-messages" else [{"text": "next request"}])


def test_success_images_and_text_only_errors_keep_original_layout(api):
    first = ModelToolCallBlock("first", "first", {})
    second = ModelToolCallBlock("second", "second", {})
    history = (
        ModelAssistantMessage((first, second)),
        ModelToolResultMessage((ModelTextBlock("success"), FIRST_IMAGE), first.id, first.name),
        ModelToolResultMessage((ModelTextBlock("error unchanged"),), second.id, second.name, True),
    )

    _, response = prepare(api, history)
    assert len(response["content"]) == 2
    content = result(api, response["content"][0])["content"]
    assert [kind(api, item) for item in content] == ["text", "image"]
    assert content[0]["text"] == "success"
    assert image_bytes(api, content[1]) == FIRST_IMAGE.data_base64
    assert assert_error(api, response["content"][1], "second") == "error unchanged"


def test_error_image_result_contains_only_text_even_with_thinking_block(api):
    call = ModelToolCallBlock("first", "first", {})
    history = (
        ModelAssistantMessage((call,)),
        ModelToolResultMessage((ModelTextBlock("failed"),
            ModelThinkingBlock("diagnostic detail", "opaque"), FIRST_IMAGE), call.id, call.name, True),
    )
    _, response = prepare(api, history)
    assert "failed" in assert_error(api, response["content"][0], "first")
    assert image_bytes(api, response["content"][-1]) == FIRST_IMAGE.data_base64


@pytest.mark.parametrize("model", [
    "anthropic.claude-3-7-sonnet-20250219-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic/claude-sonnet-4-5",
    "claude-sonnet-4-5",
    "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-4-5",
])
def test_bedrock_lifts_error_images_only_for_recognizable_claude_models(model):
    call = ModelToolCallBlock("first", "first", {})
    _, response = prepare("bedrock-converse", (
        ModelAssistantMessage((call,)),
        ModelToolResultMessage((FIRST_IMAGE,), call.id, call.name, True),
    ), model=model)
    assert len(response["content"]) == 3
    assert_error("bedrock-converse", response["content"][0], "first")
    assert image_bytes("bedrock-converse", response["content"][2]) == FIRST_IMAGE.data_base64


@pytest.mark.parametrize("model", [
    "amazon.nova-pro-v1:0",
    "my-claude-alias",
    "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123",
])
def test_bedrock_other_or_opaque_models_keep_error_images_nested(model):
    call = ModelToolCallBlock("first", "first", {})
    _, response = prepare("bedrock-converse", (
        ModelAssistantMessage((call,)),
        ModelToolResultMessage((ModelTextBlock("failed"), FIRST_IMAGE), call.id, call.name, True),
    ), model=model)
    assert response["content"] == [{"toolResult": {
        "toolUseId": "first", "status": "error", "content": [
            {"text": "failed"},
            {"image": {"format": "png", "source": {"bytes": FIRST_IMAGE.data_base64}}},
        ],
    }}]
