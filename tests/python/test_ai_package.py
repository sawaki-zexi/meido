from __future__ import annotations


def test_ai_package_is_the_public_model_core_seam():
    from backend.app import ai
    from backend.app.ai.model_adapter import OpenAICompatibleAdapter
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry
    from backend.app.ai.model_types import ModelAssistantMessage

    assert ai.ModelAdapterRegistry is ModelAdapterRegistry
    assert ai.OpenAICompatibleAdapter is OpenAICompatibleAdapter
    assert ai.ModelAssistantMessage is ModelAssistantMessage
    assert callable(ai.transform_messages)


def test_legacy_model_modules_are_aliases_not_second_implementations():
    import backend.app.message_transform as legacy_transform
    import backend.app.model_adapter as legacy_adapter
    import backend.app.ai.message_transform as core_transform
    import backend.app.ai.model_adapter as core_adapter

    assert legacy_transform is core_transform
    assert legacy_adapter is core_adapter
