"""Host-owned source sets; model references never grant provenance or authority.

Durable summaries keep their original IDs. Only the model input/output projection
uses a fixed-size set reference, with message aliases for conversation relations.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from .summary_validation import SummaryValidationError


SOURCE_ID_FIELDS = frozenset({
    'sourceMessageId', 'quotedFromSourceMessageId', 'triggerSourceMessageId',
    'targetSourceMessageId', 'targetQuotedFromSourceMessageId',
    'topicSourceMessageIds', 'requestedQuoteSourceMessageId',
})


def _reference(context, identifiers):
    value = json.dumps([context, identifiers], ensure_ascii=False, separators=(',', ':'))
    return 'sources:' + hashlib.sha256(value.encode()).hexdigest()[:24]


def summary_projection(summary, context):
    """The same bounded, deterministic view for send, inspection and restart."""
    return {**summary, 'source_message_ids': [_reference(context, summary['source_message_ids'])]}


@dataclass(frozen=True)
class SourceBinding:
    reference: str
    identifiers: tuple[str, ...]

    def project(self, payload):
        aliases = {identifier: f'm{index}' for index, identifier in enumerate(self.identifiers, 1)}

        def source_id(value):
            if isinstance(value, list):
                return [source_id(item) for item in value]
            if not isinstance(value, str) or not value:
                return value
            # Retain distinct out-of-window relations as explicitly unavailable;
            # they are not added to the admitted set or accepted model output.
            return aliases.get(value) or 'unavailable:' + hashlib.sha256(value.encode()).hexdigest()[:12]

        def relations(value):
            if isinstance(value, dict):
                return {key: source_id(item) if key in SOURCE_ID_FIELDS else relations(item)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [relations(item) for item in value]
            return value

        previous = payload['previous_summary']
        records = []
        for record in payload['messages']:
            projected = {**record, 'source_message_ids': [self.reference],
                         'message_refs': [aliases[item] for item in record['source_message_ids']]}
            if 'conversation_source' in record:
                projected['conversation_source'] = relations(record['conversation_source'])
            records.append(projected)
        return {'previous_summary': self.visible(previous) if previous else None, 'messages': records}

    def visible(self, summary):
        return {**summary, 'source_message_ids': [self.reference]}

    def restore(self, summary):
        # Called only after output is validated against this exact reference.
        return {**summary, 'source_message_ids': list(self.identifiers)}


class SummarySources:
    def __init__(self, context, admitted):
        self.context = (*context, uuid.uuid4().hex)
        self.admitted = frozenset(admitted)

    def bind(self, payload, batch):
        previous = payload['previous_summary'] or {}
        identifiers = tuple(dict.fromkeys([
            *previous.get('source_message_ids', []),
            *(identifier for record in payload['messages'] for identifier in record['source_message_ids']),
        ]))
        if not identifiers or not self.admitted.issuperset(identifiers):
            raise SummaryValidationError('invalid_sources', '压缩摘要包含无效来源')
        # Binding includes only prior valid state and this displayed batch. A
        # different request or batch has no such lease.
        return SourceBinding(_reference((*self.context, batch), identifiers), identifiers)
