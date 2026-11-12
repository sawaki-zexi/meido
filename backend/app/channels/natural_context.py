"""Bound natural conversation requests without discarding required source facts."""
from dataclasses import replace

from ..agent_runtime.meido_provider import MeidoProvider, RequestContext
from ..agent_runtime.types import UserMessage


def excerpt(text, limit):
    text = str(text)
    marker = '…（摘录）'
    return text if len(text) <= limit else text[:max(0, limit - len(marker))] + marker[:limit]


class NaturalContextProvider(MeidoProvider):
    def __init__(self, *args, background_ids, required_ids, **kwargs):
        super().__init__(*args, **kwargs)
        self.background_ids = frozenset(background_ids) - frozenset(required_ids)

    def prepare_window(self, system, messages, tools, *, context=RequestContext()):
        # Only original background rows can leave. Plans, actual input, tool
        # calls/results and correction messages retain their original pairing.
        background = [message for message in messages if isinstance(message, UserMessage)
            and message.source_message_ids and set(message.source_message_ids) <= self.background_ids]
        selected = list(messages)
        capacity = (self.configuration.contextWindowTokens if self.configuration else None) or 32768
        available = capacity - (self.max_response_tokens or 2048) - self.context_safety_margin
        current_context = replace(context, blocks=tuple(block for block in context.blocks
            if block.startswith('[当前')))
        mandatory = [message for message in selected if message not in background]
        minimum = super().prepare_request(system, mandatory, tools, context=current_context)
        # Current-group materials have bounded, independent blocks. Reserve
        # those that fit alongside mandatory input before admitting old rows.
        for message in [None, *background]:
            if message is not None:
                selected.remove(message)
            prepared = super().prepare_request(system, selected, tools, context=current_context)
            if self.estimate_prepared(prepared).tokens < available and prepared.context == minimum.context:
                break
        # Other-thread activities use only the remaining capacity.
        return super().prepare_request(system, selected, tools, context=context), selected

    async def stream_response(self, *, model, system, messages, tools, signal, session_id=None):
        context = await self.prepare_context(signal)
        prepared, selected = self.prepare_window(system, messages, tools, context=context)
        async for event in self.stream_prepared(prepared, system=system, messages=selected, signal=signal):
            yield event
