"""Auxiliary memory calls through the existing multi-protocol rich adapter.

Purpose and per-step deadlines follow fixed Shiori c14152ed Markdown calls.
Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
"""
from __future__ import annotations

import asyncio
import inspect
from contextlib import aclosing

from .ai.model_adapter import supports_auxiliary_thinking_control
from .ai.model_types import (ModelSystemMessage, ModelUserMessage, ModelTextBlock,
    ModelTextDelta, ModelStreamDone, ModelStreamError)


async def complete_auxiliary(adapter, messages, configuration, *, max_tokens, timeout=90):
    async def collect():
        rich = getattr(adapter, 'stream_rich_messages', None)
        if rich is None:
            complete = getattr(adapter, 'complete_messages', None)
            if complete is not None:
                return await complete(messages, configuration, max_tokens=max_tokens)
            return ''.join([part async for part in adapter.stream_messages(messages, configuration, max_tokens=max_tokens)])
        parameters = inspect.signature(rich).parameters
        options = {'max_tokens': max_tokens}
        if 'disable_thinking' in parameters and supports_auxiliary_thinking_control(configuration):
            options['disable_thinking'] = True
        request = [(ModelSystemMessage if item['role'] == 'system' else ModelUserMessage)(
            (ModelTextBlock(item['content']),)) for item in messages]
        parts, final = [], ''
        async with aclosing(rich(request, configuration, [], **options)) as stream:
            async for event in stream:
                if isinstance(event, ModelTextDelta):
                    parts.append(event.text)
                elif isinstance(event, ModelStreamDone):
                    final = ''.join(block.text for block in event.message.content if isinstance(block, ModelTextBlock))
                elif isinstance(event, ModelStreamError):
                    raise RuntimeError(str(event.error))
        return ''.join(parts) or final
    return await asyncio.wait_for(collect(), timeout=timeout)
