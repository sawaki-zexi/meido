"""Adapt the fixed Shiori retriever to Meido's role-isolated records.

Ranking/formatting live in memory_retriever; this module only adapts storage,
engine contracts and configured defaults from plugins/default_memory/config.py.
Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
from datetime import datetime, timezone

from .memory_retriever import Retriever


def hotness(item, half_life=14.0):
    weight = max(0, min(10, item.emotionalWeight))
    effective = max(half_life * (1 + .5 * weight / 10), .1)
    frequency = 1 / (1 + math.exp(-math.log1p(max(0, item.reinforcement))))
    age = max((datetime.now(timezone.utc) - item.updatedAt).total_seconds() / 86400, 0)
    return frequency * math.exp(-math.log(2) / effective * age)


def hit(item):
    return {'id': item.id, 'memory_type': item.memoryType, 'summary': item.summary,
        'extra_json': {**item.extra, '_emotional_weight': item.emotionalWeight,
            '_reinforcement': item.reinforcement, '_updated_at': item.updatedAt.isoformat()},
        'happened_at': item.happenedAt.isoformat() if item.happenedAt else '',
        'source_ref': json.dumps(item.sourceRef.messageIds, ensure_ascii=False) if item.sourceRef.messageIds else item.sourceRef.stableSourceKey}


class RetrievalStore:
    def __init__(self, service, snapshot, request):
        self.service, self.snapshot, self.request = service, snapshot, request

    def filtered(self, memory_types=None, memory_domains=None, time_start=None, time_end=None, **kwargs):
        for item, vector in self.snapshot:
            if memory_types and item.memoryType not in memory_types:
                continue
            if memory_domains and item.extra.get('memory_domain', '') not in memory_domains:
                continue
            if time_start is not None and (item.happenedAt is None or item.happenedAt < time_start):
                continue
            if time_end is not None and (item.happenedAt is None or item.happenedAt >= time_end):
                continue
            yield item, vector

    def vector_search_batch(self, vectors, **kwargs):
        return [self.vector_search(vector, **kwargs) for vector in vectors]

    def vector_search(self, query_vec, *, top_k=8, score_threshold=.45,
                      hotness_alpha=.2, hotness_half_life_days=14, **filters):
        results = []
        indexed = None
        store = self.service.store
        if self.request is not None and store.vector_index is not None and store.vector_index_available and not (filters.get('time_start') or filters.get('time_end')):
            try:
                indexed = dict(store.vector_index.search(self.request.scope.role_id, query_vec, max(top_k * 2, 20)))
            except Exception:
                store.vector_index_available = False
                store.vector_index_error = 'vector index query failed'
        for item, vector in self.filtered(**filters):
            if indexed is not None and item.id not in indexed or indexed is None and not vector:
                continue
            semantic = indexed[item.id] if indexed is not None else store._cosine(query_vec, vector)
            if semantic < score_threshold:
                continue
            heat = hotness(item, hotness_half_life_days)
            score = (1 - hotness_alpha) * semantic + hotness_alpha * heat
            results.append({**hit(item), 'score': round(score, 4),
                '_score_debug': {'semantic': round(semantic, 4), 'hotness': round(heat, 4), 'final': round(score, 4)}})
        return sorted(results, key=lambda value: value['score'], reverse=True)[:top_k]

    def keyword_search_summary(self, terms, *, limit=30, **filters):
        results = []
        for item, _ in self.filtered(**filters):
            count = sum(term.casefold() in item.summary.casefold() for term in terms)
            if count:
                results.append({**hit(item), 'keyword_score': count / len(terms), '_updated_at': item.updatedAt.isoformat()})
        return sorted(results, key=lambda value: (value['keyword_score'], value['_updated_at']), reverse=True)[:limit]


class QueryEmbedder:
    def __init__(self, service, role_id):
        self.service, self.role_id = service, role_id
        self.provider, self.model_id = service._embedding_session()

    async def embed(self, text):
        embed_async = getattr(self.provider, 'embed_async', None)
        vector = (await embed_async(self.role_id, text) if embed_async is not None
            else await asyncio.to_thread(self.provider.embed, self.role_id, text))
        if not vector:
            raise ValueError('查询向量不可用')
        await asyncio.to_thread(self.service.store.validate_embedding_space, self.role_id, len(vector), self.model_id)
        return vector


async def query(engine, request):
    from .memory_engine import MemoryQueryResult
    from .memory_events import RetrievalCompleted
    scope = engine._validate_scope(request.scope)
    if 'shared' in request.filters.domains:
        return MemoryQueryResult(trace={'denied_domains': list(request.filters.domains),
            'denied_reason': 'memory_domain_unauthorized'}, raw={'items': []})
    if request.limit < 0:
        raise ValueError('limit must be non-negative')
    ready, reason = await asyncio.to_thread(engine.service.availability, scope.role_id)
    if not ready or not request.limit:
        return MemoryQueryResult(trace={'intent': request.intent, 'disabled': not ready, 'reason': reason}, raw={'items': []})
    snapshot = await asyncio.to_thread(engine.store.retrieval_snapshot, scope.role_id)
    storage = RetrievalStore(engine.service, snapshot, request)
    # The constructor's historical helper fallbacks are deliberately overridden
    # with the fixed plugin's current configuration defaults.
    retriever = Retriever(storage, QueryEmbedder(engine.service, scope.role_id), top_k=8,
        score_threshold=.45, score_thresholds={'procedure': .66, 'preference': .5, 'event': .5, 'profile': .5},
        relative_delta=.2, inject_max_chars=6000, inject_max_forced=3,
        inject_max_procedure_preference=4, inject_max_event_profile=4, inject_line_max=600,
        procedure_guard_enabled=True)
    types = (['preference', 'profile'] if request.intent == 'interest' else list(request.filters.kinds) or
        (['procedure', 'preference'] if request.intent == 'procedure' else None))
    filters = dict(memory_types=types, memory_domains=list(request.filters.domains) or None,
        role_id=scope.role_id, time_start=request.filters.time_start, time_end=request.filters.time_end)
    hypotheses = []
    if request.intent == 'timeline':
        if request.filters.time_start is None or request.filters.time_end is None:
            return MemoryQueryResult(trace={'intent': 'timeline_missing_time', 'effect': request.effect})
        def timeline():
            entries = [item for item, _ in storage.filtered(**{**filters, 'memory_types': types or ['event']})]
            entries.sort(key=lambda item: item.happenedAt or item.updatedAt, reverse=True)
            return [hit(item) for item in entries[:request.limit]]
        hits = await asyncio.to_thread(timeline)
    else:
        if request.intent == 'answer':
            supplied = request.filters.hints.get('hyde_queries')
            if supplied:
                hypotheses = [str(value).strip() for value in supplied if str(value).strip()][:2]
            elif engine.hyde_provider is not None:
                async def hypothesis(style):
                    try:
                        count = len(inspect.signature(engine.hyde_provider).parameters)
                        args = (scope.role_id, request.text, style) if count >= 3 else (scope.role_id, request.text) if count == 2 else (request.text,)
                        result = await asyncio.wait_for(asyncio.to_thread(engine.hyde_provider, *args), 3)
                        return result.strip() if isinstance(result, str) else (result[0 if style == 'event' else min(1, len(result)-1)] if isinstance(result, list) and result else '')
                    except Exception:
                        return ''
                hypotheses = [text for text in await asyncio.gather(hypothesis('event'), hypothesis('general')) if text]
        elif request.intent in {'context', 'procedure'}:
            queries = request.filters.hints.get('queries')
            if isinstance(queries, list):
                hypotheses = [str(value).strip() for value in queries[1:] if str(value).strip()]
        hits = await retriever.retrieve(request.text, aux_queries=hypotheses,
            top_k=max(request.limit, 15) if request.intent == 'answer' else request.limit,
            score_threshold=.35 if request.intent == 'answer' else None, **filters)
        hits = hits[:request.limit]
    text, injected = '', []
    if request.intent in {'context', 'procedure'}:
        text, injected = retriever.build_injection_block(hits)
    elif request.intent == 'interest':
        text = '\n---\n'.join(item['summary'] for item in hits)
    by_id = {item.id: item for item, _ in snapshot}
    records = []
    for index, value in enumerate(hits):
        record = engine._record(by_id[value['id']], index, score=value.get('score', 0),
            signals={'rrf_score': value.get('rrf_score', 0), **value.get('_score_debug', {})})
        record.injected = record.id in injected
        records.append(record)
    result = MemoryQueryResult(text_block=text, records=records,
        trace={'intent': request.intent, 'effect': request.effect, 'role_id': scope.role_id,
            'hyde_hypotheses': hypotheses, 'injection': {'budget_chars': 6000, 'injected_count': len(injected)}},
        raw={'items': hits, 'candidate_count': len(hits)})
    if engine.event_bus is not None:
        engine.event_bus.publish(RetrievalCompleted(scope.role_id, scope.session_key, request.text,
            request.intent, len(records), len(injected), result.trace))
    return result
