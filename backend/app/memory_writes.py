"""Embedding-first writes and post-response adapters for the default engine.

Adapted from Shiori c14152ed semantic/memorizer.py and engine/mutation.py.
Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from types import SimpleNamespace

from .memory_rule_schema import build_procedure_rule_schema
from .memory_retrieval import RetrievalStore, QueryEmbedder
from .memory_retriever import Retriever


def normalize_ingest(content):
    if isinstance(content, dict):
        chain = content.get('tool_chain')
        return {'user_message': str(content.get('user_message') or ''),
            'assistant_response': str(content.get('assistant_response') or ''),
            'tool_chain': [item for item in chain if isinstance(item, dict)] if isinstance(chain, list) else [],
            'source_ref': str(content.get('source_ref') or '')}
    if not isinstance(content, list):
        return None
    result = {'user_message': '', 'assistant_response': '', 'tool_chain': [], 'source_ref': ''}
    for message in content:
        if not isinstance(message, dict):
            continue
        role, body = str(message.get('role') or ''), str(message.get('content') or '')
        if role == 'user' and body:
            result['user_message'] = body
        elif role == 'assistant' and body:
            result['assistant_response'] = body
            chain = message.get('tool_chain')
            if isinstance(chain, list):
                result['tool_chain'] = [item for item in chain if isinstance(item, dict)]
    return result if result['user_message'] or result['assistant_response'] else None


async def tag_procedure(service, role_id, summary):
    if service.auxiliary_provider is None:
        return None
    from .memory_procedure_tagger import ProcedureTagger
    class Provider:
        async def chat(self, *, messages, max_tokens, **kwargs):
            value = await asyncio.to_thread(service.auxiliary_provider, role_id, messages, max_tokens)
            return SimpleNamespace(content=value)
    return await ProcedureTagger(Provider(), '', lambda: []).tag(summary)


def save_implicit(service, role_id, source, payload, *, protected_ids=()):
    from .models import MemorySourceRef
    if not isinstance(payload, dict):
        raise ValueError('隐式长期记忆提取格式无效')
    saved = []
    protected = {item.summary for item in service.store.get_many(role_id, list(protected_ids))}
    for kind in ('profile', 'preference', 'procedure'):
        for candidate in payload.get(kind, []):
            if not isinstance(candidate, dict) or not isinstance(candidate.get('summary'), str):
                continue
            summary = candidate['summary'].strip()
            if not summary or summary in protected:
                continue
            extra = {key: value for key, value in candidate.items() if key not in {'summary', 'happened_at'}}
            extra.setdefault('memory_domain', 'relationship')
            key = hashlib.sha1(summary.encode()).hexdigest()[:12]
            item_source = source.model_copy(update={'stableSourceKey': f'{source.stableSourceKey}#{kind}:{key}'})
            happened = candidate.get('happened_at')
            happened_at = datetime.fromisoformat(happened) if happened else None
            item, _ = save(service, role_id, kind, summary, item_source, extra=extra,
                happened_at=happened_at, protected_ids=protected_ids, explicit=False)
            saved.append(item)
    return saved


def save(service, role_id, kind, summary, source, *, extra=None, happened_at=None, protected_ids=(), explicit=True):
    return asyncio.run(save_async(service, role_id, kind, summary, source, extra=extra,
        happened_at=happened_at, protected_ids=protected_ids, explicit=explicit))


async def save_async(service, role_id, kind, summary, source, *, extra=None, happened_at=None, protected_ids=(), explicit=True):
    extra = dict(extra or {})
    steps = [str(step).strip() for step in extra.get('steps', []) if str(step).strip()]
    tool = str(extra.get('tool_requirement') or '').strip()
    if explicit and kind == 'procedure' and not (steps or tool):
        kind = 'preference'
    if explicit and kind == 'procedure':
        extra['rule_schema'] = build_procedure_rule_schema(summary, tool, steps, extra.get('rule_schema'))
    vector, model = await service.embed_for_write_async(role_id, summary)
    snapshot = await asyncio.to_thread(service.store.retrieval_snapshot, role_id)
    similar = await asyncio.to_thread(RetrievalStore(service, snapshot, None).vector_search, vector, top_k=5,
        memory_types=[kind], score_threshold=.7 if kind in {'procedure', 'preference'} else .9)
    similar = [item for item in similar if item['id'] not in protected_ids]
    if kind == 'procedure' and tool:
        target = next((item for item in similar if item['score'] >= .7 and item['extra_json'].get('tool_requirement') == tool), None)
        if target:
            old = target['summary'].strip()
            merged = old if summary in old else summary if old in summary else old.rstrip('。；;，, ') + '；' + summary
            # The merged text needs its own vector before changing any record.
            vector, model = await service.embed_for_write_async(role_id, merged)
            extra = {**target['extra_json'], **extra, '_merge_note': merged,
                'steps': list(dict.fromkeys([*target['extra_json'].get('steps', []), *steps]))}
            extra['rule_schema'] = build_procedure_rule_schema(merged, tool, extra['steps'], extra.get('rule_schema'))
            extra.pop('trigger_tags', None)
            item = service.store.update(role_id, target['id'], merged, kind, happened_at)
            service.store.update_metadata(role_id, item.id, extra_json=extra)
            service.store.set_embedding(role_id, item.id, vector, model)
            return service.store.get(role_id, item.id), 'merged'
    retire = []
    if kind in {'preference', 'procedure'}:
        retire = [item['id'] for item in similar if item['score'] >= .9]
    elif kind == 'profile' and extra.get('category') in {'status', 'purchase'}:
        retire = [item['id'] for item in similar if item['extra_json'].get('category') == extra['category']
            and item['score'] >= (.92 if int(item['extra_json'].get('_emotional_weight', 0)) >= 7 else .9)]
    for item_id in retire:
        service.store.set_status(role_id, item_id, 'superseded')
    before = {item.id for item, _ in snapshot}
    item = service.store.add_or_reinforce(role_id, kind, summary, source, extra=extra, happened_at=happened_at)
    if item.status == 'superseded':
        item = service.store.set_status(role_id, item.id, 'active')
    service.store.set_embedding(role_id, item.id, vector, model)
    weight = extra.get('emotional_weight', 0)
    try:
        weight = max(0, min(10, int(weight)))
    except (TypeError, ValueError):
        weight = 0
    if weight:
        service.store.update_metadata(role_id, item.id, emotional_weight=max(item.emotionalWeight, weight))
    return service.store.get(role_id, item.id), 'reinforced' if item.id in before else 'new'


async def post_response(service, role_id, session_key, user, assistant, protected_ids, tool_metadata=None, *, source_key=None):
    from .memory_post_response import PostResponseMemoryWorker
    from .models import MemorySourceRef
    source_key = source_key or f'turn:{session_key}:{user.id}:{assistant.id}'
    saved = []

    class Provider:
        async def chat(self, *, messages, max_tokens, **kwargs):
            if service.auxiliary_provider is None:
                return SimpleNamespace(content='[]')
            value = await asyncio.to_thread(service.auxiliary_provider, role_id, messages, max_tokens)
            return SimpleNamespace(content=value)

    class Memorizer:
        def supersede_batch(self, ids):
            for item_id in ids:
                if item_id not in protected_ids:
                    service.store.set_status(role_id, item_id, 'superseded')

    class Publisher:
        async def fanout(self, event):
            if service.event_bus is not None:
                service.event_bus.publish(event)

    async def extract(**kwargs):
        if not service.implicit_extraction_enabled or service.post_response_provider is None:
            return
        active = await asyncio.to_thread(service.store.list_active, role_id)
        result = await asyncio.to_thread(service.post_response_provider, role_id, session_key, user, assistant,
            [{'memoryType': item.memoryType, 'summary': item.summary} for item in active])
        if not isinstance(result, dict):
            raise ValueError('隐式长期记忆提取格式无效')
        protected_summaries = {item.summary for item in service.store.get_many(role_id, list(protected_ids))}
        for kind in ('profile', 'preference', 'procedure'):
            for candidate in result.get(kind, []):
                if not isinstance(candidate, dict) or not isinstance(candidate.get('summary'), str):
                    continue
                summary = candidate['summary'].strip()
                if not summary or summary in protected_summaries:
                    continue
                extra = {key: value for key, value in candidate.items() if key not in {'summary', 'happened_at'}}
                source = MemorySourceRef(kind='turn', sessionKey=f'role:{role_id}', messageIds=[user.id, assistant.id],
                    messageRange=(user.sequence, assistant.sequence),
                    stableSourceKey=f'{source_key}#{kind}:{len(saved)}')
                happened = candidate.get('happened_at')
                happened_at = datetime.fromisoformat(happened) if happened else None
                item, _ = await asyncio.to_thread(save, service, role_id, kind, summary, source,
                    extra=extra, happened_at=happened_at, protected_ids=protected_ids, explicit=False)
                saved.append(item)

    snapshot = await asyncio.to_thread(service.store.retrieval_snapshot, role_id)
    retriever = Retriever(RetrievalStore(service, snapshot, None), QueryEmbedder(service, role_id), top_k=8,
        score_threshold=.45, relative_delta=.2, inject_max_chars=6000, inject_max_event_profile=4, inject_line_max=600)
    worker = PostResponseMemoryWorker(Memorizer(), retriever, Provider(), '', Publisher(), extract)
    chain = list((tool_metadata or {}).get('tool_chain', []))
    chain.append({'calls': [{'name': 'memorize', 'arguments': {}, 'result': f'new:{identifier}'} for identifier in protected_ids]})
    await worker.run(user.content, assistant.content, chain, source_key, session_key=session_key, role_id=role_id)
    service._publish_written(role_id, session_key, source_key, [item.id for item in saved], 'post_response')
    return saved
