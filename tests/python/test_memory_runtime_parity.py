import asyncio
import json
import sqlite3
import threading
import time

import httpx
import pytest
from fastapi import FastAPI

from backend.app.memory_engine import DefaultMemoryEngine, MemoryMutation, MemoryQuery, MemoryScope
from backend.app.memory_service import MemoryService
from backend.app.memory_store import MemoryStore
from backend.app.models import MemorySourceRef
from memory_fakes import ControlledEmbedding, configured_service


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_failed_explicit_embedding_writes_nothing_and_unconfigured_engine_has_no_tools(tmp_path):
    class Failed(ControlledEmbedding):
        def embed(self, *_):
            raise TimeoutError('controlled embedding outage')
    store = MemoryStore(tmp_path / 'memory.db')
    engine = DefaultMemoryEngine(MemoryService(store, Failed()), role_exists=lambda _: True)
    with pytest.raises(TimeoutError):
        await engine.mutate(MemoryMutation('remember', MemoryScope('r', 'role:r'), summary='明确记忆'))
    assert store.list_all('r') == []
    unavailable = DefaultMemoryEngine(MemoryService(store), role_exists=lambda _: True)
    assert unavailable.tool_profile().memorize is None
    assert (await unavailable.query(MemoryQuery('明确记忆', scope=MemoryScope('r', 'role:r')))).trace['disabled']


@pytest.mark.anyio
@pytest.mark.parametrize('lane,deadline', [('hyde', 3), ('embedding', 8)])
async def test_query_timeout_does_not_wait_for_abandoned_provider_thread(tmp_path, lane, deadline):
    release, entered = threading.Event(), threading.Event()
    class Slow(ControlledEmbedding):
        def embed(self, *args):
            if lane == 'embedding':
                entered.set()
                release.wait(deadline + 4)
            return super().embed(*args)
    def hyde(*_):
        entered.set()
        release.wait(deadline + 4)
        return 'late hypothesis'
    engine = DefaultMemoryEngine(MemoryService(MemoryStore(tmp_path / 'memory.db'), Slow()), role_exists=lambda _: True,
        hyde_provider=hyde if lane == 'hyde' else None)
    from backend.app.agent_runtime.memory_write_tools import RoleMemoryAccess
    access = RoleMemoryAccess(engine, None, 'r')
    started = time.monotonic()
    try:
        task = asyncio.create_task(access.query(MemoryQuery('query', intent='answer' if lane == 'hyde' else 'context',
            scope=MemoryScope('r', 'role:r'), effect='read_only')))
        assert await asyncio.to_thread(entered.wait, 1)
        result = await asyncio.wait_for(task, deadline + 1.5)
        assert time.monotonic() - started < deadline + 1.5
        assert result.records == []
        assert not release.is_set()
    finally:
        release.set()


@pytest.mark.anyio
async def test_large_corpus_fallback_scores_off_http_loop_and_never_backfills(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / 'memory.db')
    service = configured_service(store)
    source = MemorySourceRef(kind='manual', sessionKey='role:r', stableSourceKey='seed')
    seed = store.add_or_reinforce('r', 'profile', '检索词 seed', source)
    vector = ControlledEmbedding().embed('r', seed.summary)
    store.set_embedding('r', seed.id, vector, ControlledEmbedding.model_id)
    with sqlite3.connect(store.database_path) as db:
        columns = [row[1] for row in db.execute('PRAGMA table_info(memory_items)')]
        row = list(db.execute('SELECT * FROM memory_items WHERE id=?', (seed.id,)).fetchone())
        for index in range(12000):
            clone = list(row)
            clone[columns.index('id')] = f'bulk-{index}'
            clone[columns.index('content_hash')] = f'bulk-hash-{index}'
            clone[columns.index('summary')] = f'检索词 source-{index}'
            db.execute('INSERT INTO memory_items VALUES (' + ','.join('?' for _ in clone) + ')', clone)
    entered = threading.Event()
    original = store._cosine
    def cosine(a, b):
        entered.set()
        return original(a, b)
    monkeypatch.setattr(store, '_cosine', cosine)
    monkeypatch.setattr(store, 'set_embedding', lambda *_: pytest.fail('query must not repair vectors'))
    engine = DefaultMemoryEngine(service, role_exists=lambda _: True)
    app = FastAPI()
    @app.get('/query')
    async def query():
        return {'count': len((await engine.query(MemoryQuery('检索词', scope=MemoryScope('r', 'role:r')))).records)}
    @app.get('/other-page')
    async def other_page():
        return {'ready': True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
        task = asyncio.create_task(client.get('/query'))
        assert await asyncio.to_thread(entered.wait, 4)
        started = time.monotonic()
        assert (await client.get('/other-page')).json() == {'ready': True}
        assert time.monotonic() - started < .5
        assert (await task).json()['count'] == 8


def test_legacy_colon_model_identity_and_vectors_survive_import_and_restart(tmp_path):
    path = tmp_path / 'memory.db'
    store = MemoryStore(path)
    item = store.add_or_reinforce('r', 'profile', 'legacy', MemorySourceRef(kind='manual', sessionKey='role:r', stableSourceKey='legacy'))
    old_id = 'custom:http://127.0.0.1:1234/v1:nomic-embed-text:latest:dimensions=2'
    store.set_embedding('r', item.id, [1.0, 0.0], old_id)
    (tmp_path / 'embedding-config.json').write_text(json.dumps({'provider': 'custom', 'baseUrl': 'http://127.0.0.1:1234/v1',
        'model': 'nomic-embed-text:latest', 'dimensions': 2}), encoding='utf-8')
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=1')
    for _ in range(2):
        imported = MemoryStore(path)
        imported.validate_embedding_space('r', 2, 'nomic-embed-text:latest')
        assert imported.embedding_for('r', item.id) == [1.0, 0.0]


def test_loose_json_matches_fixed_memory_parser():
    from backend.app.memory_json import load_json_array_loose, load_json_object_loose
    fence = chr(96) * 3
    assert load_json_array_loose(f'{fence}json\n["old-id",]\n{fence}') == ['old-id']
    assert load_json_object_loose('{profile: [{summary: "稳定事实",}],}') == {'profile': [{'summary': '稳定事实'}]}
    assert load_json_object_loose('[]') is None


@pytest.mark.anyio
async def test_markdown_auxiliary_can_exceed_embedding_deadline_without_forcing_json(monkeypatch):
    import json
    import time
    import httpx
    from backend.app.memory_model import complete_auxiliary
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    from backend.app.models import ModelConfiguration
    seen = []
    async def respond(request):
        seen.append(json.loads(request.content))
        await asyncio.sleep(8.1)
        chunk = {'choices': [{'delta': {'content': '{history_entries: []}'}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, text='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    configuration = ModelConfiguration(id='aux', providerId='deepseek', provider='DeepSeek',
        baseUrl='https://model.test', apiKey='', model='controlled', api='openai-completions')
    start = time.monotonic()
    result = await complete_auxiliary(RoutedModelAdapter(ModelAdapterRegistry()),
        [{'role': 'user', 'content': 'summarize host memory'}], configuration, max_tokens=1024, timeout=300)
    assert time.monotonic() - start >= 8 and result == '{history_entries: []}'
    assert seen[0]['max_tokens'] == 1024 and seen[0]['thinking'] == {'type': 'disabled'}
    assert 'response_format' not in seen[0] and not seen[0].get('tools')


@pytest.mark.anyio
async def test_embedding_http_has_independent_write_budget_and_query_cancels_connection(tmp_path, monkeypatch):
    from backend.app.embeddings import MemoryEmbeddingProvider
    from backend.app.models import EmbeddingConfiguration
    configuration = EmbeddingConfiguration(provider='local', baseUrl='https://embedding.test',
        model='controlled', apiKey='fixture', dimensions=2)
    provider = MemoryEmbeddingProvider(lambda: configuration)
    requests, closed = [], threading.Event()
    mode = 'write'
    async def respond(request):
        requests.append(request)
        try:
            if mode == 'write':
                await asyncio.sleep(8.1)
                return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1., 0.]}]})
            await asyncio.Event().wait()
        finally:
            closed.set()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    assert await provider.embed_async('r', 'explicit write can exceed eight seconds') == [1., 0.]
    assert requests[0].extensions['timeout']['read'] == 30
    mode = 'query'; closed.clear()
    service = MemoryService(MemoryStore(tmp_path / 'memory.db'), provider)
    engine = DefaultMemoryEngine(service, role_exists=lambda _: True)
    start = time.monotonic()
    await engine.query(MemoryQuery('query deadline', scope=MemoryScope('r', 'role:r')))
    assert 7.5 < time.monotonic() - start < 9 and closed.is_set()
    provider.TOTAL_TIMEOUT_SECONDS = .05
    closed.clear()
    with pytest.raises(TimeoutError):
        await provider.embed_async('r', 'bounded write deadline')
    assert closed.is_set() and provider.last_error == 'TimeoutError'


@pytest.mark.anyio
@pytest.mark.parametrize('merging', [False, True])
async def test_memory_tool_timeout_closes_embedding_and_never_writes_late(tmp_path, monkeypatch, merging):
    from backend.app.embeddings import MemoryEmbeddingProvider
    from backend.app.models import EmbeddingConfiguration, MemorySourceRef
    from backend.app.agent_runtime.memory_write_tools import RoleMemoryAccess
    from backend.app.agent_runtime import ToolContext, CancellationToken
    closed = threading.Event()
    requests = []
    async def respond(request):
        requests.append(request)
        if merging and len(requests) == 1:
            return httpx.Response(200, json={'data': [{'embedding': [1., 0.]}]})
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    config = EmbeddingConfiguration(provider='local', baseUrl='https://embedding.test', model='controlled', apiKey='fixture', dimensions=2)
    service = MemoryService(MemoryStore(tmp_path / 'memory.db'), MemoryEmbeddingProvider(lambda: config))
    if merging:
        original_item = service.store.add_or_reinforce('r', 'procedure', 'existing instruction',
            MemorySourceRef(kind='manual', sessionKey='role:r', stableSourceKey='existing'),
            extra={'tool_requirement': 'read_file', 'steps': ['existing step']})
        service.store.set_embedding('r', original_item.id, [1., 0.], 'controlled')
    before = service.store.snapshot_role('r')
    engine = DefaultMemoryEngine(service, role_exists=lambda _: True)
    access = RoleMemoryAccess(engine, None, 'r', io_timeout_seconds=.3)
    context = ToolContext('r', 'role:r', 'fixture', CancellationToken())
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await access.mutate(MemoryMutation('remember', MemoryScope('r', 'role:r'), summary='must not commit',
            memory_kind='procedure' if merging else 'fact',
            metadata={'tool_requirement': 'read_file', 'steps': ['new step']} if merging else {}), context)
    assert time.monotonic() - start < 1.5 and closed.is_set()
    assert len(requests) == (2 if merging else 1)
    assert service.store.snapshot_role('r') == before
