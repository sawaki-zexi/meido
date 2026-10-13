from backend.app.message_transform import transform_messages
from backend.app.model_types import (
    ModelAssistantMessage,
    ModelCapabilities,
    ModelImageBlock,
    ModelResponseMetadata,
    ModelSystemMessage,
    ModelTextBlock,
    ModelThinkingBlock,
    ModelToolCallBlock,
    ModelToolResultMessage,
    ModelUserMessage,
)


def test_transform_downgrades_thinking_and_deduplicates_unsupported_images():
    messages = (
        ModelUserMessage((
            ModelTextBlock("看图"),
            ModelImageBlock("image/png", "a"),
            ModelImageBlock("image/png", "b"),
        )),
        ModelAssistantMessage((ModelThinkingBlock("先想想", "opaque"),)),
    )

    transformed = transform_messages(
        messages,
        capabilities=ModelCapabilities(supports_images=False, supports_reasoning=False),
        source_model="deepseek-reasoner",
        target_model="deepseek-chat",
    )

    assert transformed[0].content == (
        ModelTextBlock("看图"),
        ModelTextBlock("(image omitted: model does not support images)"),
    )
    assert transformed[1].content == (ModelTextBlock("先想想"),)


def test_transform_drops_redacted_thinking_when_replaying_other_model():
    messages = (ModelAssistantMessage((ModelThinkingBlock(signature="redacted"),)),)
    assert transform_messages(messages, source_model="a", target_model="b") == (
        ModelAssistantMessage(()),
    )


def test_transform_preserves_same_model_thinking_signature():
    message = ModelAssistantMessage((ModelThinkingBlock("reason", "opaque"),))
    same_model = transform_messages(
        (message,),
        source_model="same",
        target_model="same",
    )
    assert same_model[0].content == message.content


def test_transform_preserves_all_provider_signatures_only_for_same_connection():
    call = ModelToolCallBlock("bad|id", "lookup", {}, thought_signature="tool-signature")
    message = ModelAssistantMessage(
        (
            ModelTextBlock("answer", text_signature="text-signature"),
            ModelThinkingBlock("", "opaque", redacted=True),
            call,
        ),
        ModelResponseMetadata(provider="source", api="source-api", model="source-model"),
    )
    result = ModelToolResultMessage((ModelTextBlock("done"),), call.id, call.name)
    capabilities = ModelCapabilities(tool_call_id_pattern=r"[A-Za-z0-9_-]+", max_tool_call_id_length=16)

    same = transform_messages(
        (message, result),
        capabilities=capabilities,
        target_provider="source",
        target_api="source-api",
        target_model="source-model",
    )
    cross = transform_messages(
        (message, result),
        capabilities=capabilities,
        target_provider="target",
        target_api="source-api",
        target_model="source-model",
    )

    assert same[0].content == message.content
    assert same[1].tool_call_id == "bad|id"
    assert cross[0].content[0] == ModelTextBlock("answer")
    assert cross[0].content[1:] == (ModelToolCallBlock(cross[0].content[1].id, "lookup", {}),)
    assert cross[0].content[1].id != "bad|id"
    assert cross[1].tool_call_id == cross[0].content[1].id


def test_transform_uses_distinct_placeholder_text_for_user_and_tool_images():
    messages = (
        ModelUserMessage((ModelImageBlock("image/png", "first"), ModelImageBlock("image/png", "second"))),
        ModelAssistantMessage((ModelToolCallBlock("call-1", "inspect", {}),)),
        ModelToolResultMessage((ModelImageBlock("image/png", "first"), ModelImageBlock("image/png", "second")), "call-1", "inspect"),
    )
    transformed = transform_messages(messages, capabilities=ModelCapabilities(supports_images=False))

    assert transformed[0].content == (ModelTextBlock("(image omitted: model does not support images)"),)
    assert transformed[-1].content == (ModelTextBlock("(tool image omitted: model does not support images)"),)


def test_transform_normalizes_tool_ids_and_keeps_results_paired():
    call = ModelToolCallBlock("bad|id/long", "todo.write", {"title": "x"})
    result = ModelToolResultMessage(
        (ModelTextBlock("done"),), "bad|id/long", "todo.write"
    )
    transformed = transform_messages(
        (ModelAssistantMessage((call,)), result),
        capabilities=ModelCapabilities(tool_call_id_pattern=r"[A-Za-z0-9_-]+", max_tool_call_id_length=16),
        target_provider="target",
        target_api="target-api",
        target_model="target-model",
    )
    sent_call = transformed[0].content[0]
    assert isinstance(sent_call, ModelToolCallBlock)
    assert len(sent_call.id) <= 16
    assert transformed[1].tool_call_id == sent_call.id
    assert transformed[1].content == (ModelTextBlock("done"),)
    assert transformed[1].is_error is False


def test_transform_disambiguates_colliding_short_tool_ids(monkeypatch):
    from backend.app import message_transform

    monkeypatch.setattr(message_transform, "_normalize_id", lambda _value, _capabilities: "collision")
    first = ModelToolCallBlock("provider-call-1", "first")
    second = ModelToolCallBlock("provider-call-2", "second")
    messages = (
        ModelAssistantMessage((first, second)),
        ModelToolResultMessage((ModelTextBlock("one"),), first.id, first.name),
        ModelToolResultMessage((ModelTextBlock("two"),), second.id, second.name),
    )

    transformed = transform_messages(
        messages,
        capabilities=ModelCapabilities(tool_call_id_pattern=r"[A-Za-z0-9_-]+", max_tool_call_id_length=9),
        target_provider="target",
        target_api="target-api",
        target_model="target-model",
    )

    calls = transformed[0].content
    results = transformed[1:]
    assert calls[0].id != calls[1].id
    assert [result.tool_call_id for result in results] == [call.id for call in calls]


def test_transform_synthesizes_missing_results_and_drops_orphans():
    call = ModelToolCallBlock("call-1", "shell")
    orphan = ModelToolResultMessage((ModelTextBlock("orphan"),), "unknown", "shell")
    transformed = transform_messages((ModelAssistantMessage((call,)), orphan))
    assert len(transformed) == 2
    missing = transformed[1]
    assert isinstance(missing, ModelToolResultMessage)
    assert missing.tool_call_id == "call-1"
    assert missing.is_error is True


def test_transform_defers_system_updates_until_tool_result():
    call = ModelToolCallBlock("call-1", "shell")
    system = ModelSystemMessage((ModelTextBlock("updated"),))
    result = ModelToolResultMessage((ModelTextBlock("done"),), "call-1", "shell")
    transformed = transform_messages((ModelAssistantMessage((call,)), system, result))
    assert transformed == (ModelAssistantMessage((call,)), result, system)


def test_transform_normalizes_none_and_string_content():
    assert transform_messages((ModelUserMessage(None),))[0].content == ()
    assert transform_messages((ModelUserMessage("hello"),))[0].content == (ModelTextBlock("hello"),)
    assert transform_messages((ModelSystemMessage(None),))[0].content == ()


def test_transform_uses_each_assistant_model_for_cross_model_reasoning():
    message = ModelAssistantMessage(
        (ModelThinkingBlock("reason", "opaque"),),
        ModelResponseMetadata(model="source-model"),
    )
    transformed = transform_messages(
        (message,),
        target_model="target-model",
        capabilities=ModelCapabilities(preserve_thinking_signatures=True),
    )
    assert transformed[0].content == (ModelTextBlock("reason"),)


def test_transform_downgrades_thinking_when_provider_or_api_changes_with_same_model():
    message = ModelAssistantMessage(
        (ModelThinkingBlock("reason", "opaque-signature"),),
        ModelResponseMetadata(provider="source-provider", api="source-api", model="shared-model"),
    )
    capabilities = ModelCapabilities(preserve_thinking_signatures=True)

    by_provider = transform_messages(
        (message,),
        capabilities=capabilities,
        target_provider="target-provider",
        target_api="source-api",
        target_model="shared-model",
    )
    by_api = transform_messages(
        (message,),
        capabilities=capabilities,
        target_provider="source-provider",
        target_api="target-api",
        target_model="shared-model",
    )

    assert by_provider[0].content == (ModelTextBlock("reason"),)
    assert by_api[0].content == (ModelTextBlock("reason"),)


def test_transform_downgrades_signatures_when_source_protocol_metadata_is_missing():
    message = ModelAssistantMessage(
        (ModelThinkingBlock("reason", "opaque-signature"),),
        ModelResponseMetadata(model="shared-model"),
    )
    transformed = transform_messages(
        (message,),
        capabilities=ModelCapabilities(preserve_thinking_signatures=True),
        target_provider="target-provider",
        target_api="target-api",
        target_model="shared-model",
    )

    assert transformed[0].content == (ModelTextBlock("reason"),)


def test_transform_skips_incomplete_assistant_turns():
    failed = ModelAssistantMessage(
        (ModelTextBlock("partial"),),
        ModelResponseMetadata(stop_reason="aborted"),
    )
    assert transform_messages((ModelSystemMessage(), failed, ModelUserMessage(()))) == (
        ModelSystemMessage(), ModelUserMessage(())
    )
