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
        ModelTextBlock("[image omitted: image/png]"),
    )
    assert transformed[1].content == (ModelTextBlock("先想想"),)


def test_transform_drops_redacted_thinking_when_replaying_other_model():
    messages = (ModelAssistantMessage((ModelThinkingBlock(signature="redacted"),)),)
    assert transform_messages(messages, source_model="a", target_model="b") == (
        ModelAssistantMessage(()),
    )


def test_transform_preserves_same_model_thinking_only_when_capability_allows_signature():
    message = ModelAssistantMessage((ModelThinkingBlock("reason", "opaque"),))
    without_signature = transform_messages(
        (message,),
        source_model="same",
        target_model="same",
    )
    with_signature = transform_messages(
        (message,),
        capabilities=ModelCapabilities(preserve_thinking_signatures=True),
        source_model="same",
        target_model="same",
    )
    assert without_signature[0].content == (ModelThinkingBlock("reason"),)
    assert with_signature[0].content == message.content


def test_transform_normalizes_tool_ids_and_keeps_results_paired():
    call = ModelToolCallBlock("bad|id/long", "todo.write", {"title": "x"})
    result = ModelToolResultMessage(
        (ModelTextBlock("done"),), "bad|id/long", "todo.write"
    )
    transformed = transform_messages(
        (ModelAssistantMessage((call,)), result),
        capabilities=ModelCapabilities(tool_call_id_pattern=r"[A-Za-z0-9_-]+", max_tool_call_id_length=16),
    )
    sent_call = transformed[0].content[0]
    assert isinstance(sent_call, ModelToolCallBlock)
    assert len(sent_call.id) <= 16
    assert transformed[1].tool_call_id == sent_call.id
    assert transformed[1].content == (ModelTextBlock("done"),)
    assert transformed[1].is_error is False


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


def test_transform_skips_incomplete_assistant_turns():
    failed = ModelAssistantMessage(
        (ModelTextBlock("partial"),),
        ModelResponseMetadata(stop_reason="aborted"),
    )
    assert transform_messages((ModelSystemMessage(), failed, ModelUserMessage(()))) == (
        ModelSystemMessage(), ModelUserMessage(())
    )
