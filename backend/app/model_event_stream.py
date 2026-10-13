"""FIFO event stream with a separately awaitable final assistant message."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from .model_types import (
    ModelAssistantMessage,
    ModelResponseMetadata,
    ModelStreamDone,
    ModelStreamError,
    ModelStreamEvent,
)

_END = object()


class AssistantEventStream:
    """A single-producer stream consumed by async iteration or ``result()``."""

    def __init__(self) -> None:
        loop = asyncio.get_running_loop()
        self._events: asyncio.Queue[ModelStreamEvent | object] = asyncio.Queue()
        self._result: asyncio.Future[ModelAssistantMessage] = loop.create_future()
        self._closed = False

    async def push(self, event: ModelStreamEvent) -> None:
        if self._closed:
            raise RuntimeError("assistant event stream is already terminal")
        await self._events.put(event)
        if isinstance(event, ModelStreamDone):
            self._finish(event.message)
        elif isinstance(event, ModelStreamError):
            message = event.message or ModelAssistantMessage(
                metadata=ModelResponseMetadata(
                    stop_reason="error",
                    error_message=event.error,
                )
            )
            self._finish(message)

    async def end(self) -> None:
        if self._closed:
            return
        error = ModelStreamError("model stream ended without a terminal event")
        await self._events.put(error)
        self._finish(ModelAssistantMessage(
            metadata=ModelResponseMetadata(
                stop_reason="error",
                error_message=error.error,
            )
        ))

    async def result(self) -> ModelAssistantMessage:
        return await self._result

    def __aiter__(self) -> AsyncIterator[ModelStreamEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[ModelStreamEvent]:
        while True:
            event = await self._events.get()
            if event is _END:
                return
            yield event  # type: ignore[misc]

    def _finish(self, message: ModelAssistantMessage) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._result.done():
            self._result.set_result(message)
        self._events.put_nowait(_END)
