import asyncio

import pytest

from backend.app.model_event_stream import AssistantEventStream
from backend.app.model_types import (
    ModelAssistantMessage,
    ModelResponseMetadata,
    ModelStreamDone,
    ModelStreamError,
    ModelStreamStart,
    ModelTextDelta,
)


def test_assistant_event_stream_preserves_fifo_and_resolves_final_message():
    async def run():
        stream = AssistantEventStream()
        consumer = asyncio.create_task(_collect(stream))
        await stream.push(ModelStreamStart())
        await stream.push(ModelTextDelta("hello"))
        final = ModelAssistantMessage(metadata=ModelResponseMetadata(stop_reason="stop"))
        await stream.push(ModelStreamDone(final))
        return await consumer, await stream.result()

    events, result = asyncio.run(run())
    assert events == [ModelStreamStart(), ModelTextDelta("hello"), ModelStreamDone(result)]
    assert result.metadata.stop_reason == "stop"


def test_assistant_event_stream_converts_unterminated_stream_to_error():
    async def run():
        stream = AssistantEventStream()
        await stream.push(ModelStreamStart())
        await stream.end()
        return [event async for event in stream], await stream.result()

    events, result = asyncio.run(run())
    assert isinstance(events[-1], ModelStreamError)
    assert result.metadata.stop_reason == "error"
    assert "without a terminal event" in result.metadata.error_message


def test_assistant_event_stream_rejects_events_after_terminal():
    async def run():
        stream = AssistantEventStream()
        await stream.push(ModelStreamDone(ModelAssistantMessage()))
        with pytest.raises(RuntimeError, match="already terminal"):
            await stream.push(ModelTextDelta("late"))

    asyncio.run(run())


async def _collect(stream):
    return [event async for event in stream]
