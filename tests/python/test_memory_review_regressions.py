"""Cross-consumer persistence regressions from the #302 independent review."""
import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.memory_config import MemorySettings, MemorySettingsStore
from backend.app.memory_engine import DefaultMemoryEngine, MemoryMutation, MemoryQuery, MemoryScope
from backend.app.memory_maintenance import MemoryMaintenance
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_store import MemoryStore
from backend.app.models import MemorySourceRef
from memory_fakes import ControlledEmbedding, configured_service
from test_context_memory_parity import transcript


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def publication_fixture(tmp_path, monkeypatch):
    sessions = transcript(tmp_path)
    store = MemoryStore(tmp_path / 'memory.db')
    service = configured_service(store)
    published = threading.Event()
    def publish(event):
        source = MemorySourceRef(kind='consolidation', sessionKey='role:r',
            messageIds=list(event.message_ids), stableSourceKey=event.source_key)
        store.consume_consolidation_event('r', event.source_key,
            [('event', '必须保留的独立消费者事实', source, None)])
        published.set()
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions,
        on_consolidation_committed=publish, defer_consumers=True,
        consolidation_provider=lambda *_: {'history': '共同经历', 'pendingItems': []})
    worker = MemoryWorker(service, maintenance)
    for name, value in {'memory_store': store, 'memory_service': service,
                        'memory_worker': worker, 'memory_maintenance': maintenance}.items():
        monkeypatch.setattr(main, name, value)
    return sessions, store, service, maintenance, worker, published


@pytest.mark.anyio
@pytest.mark.parametrize('entrypoint', ['background', 'resume', 'reenable'])
async def test_publication_ack_survives_concurrent_tool_rollback(tmp_path, monkeypatch, entrypoint):
    sessions, store, service, maintenance, worker, published = publication_fixture(tmp_path, monkeypatch)
    maintenance.maintain('r', 'role:r')
    started, release = asyncio.Event(), asyncio.Event()
    async def failed_tool():
        started.set()  # The host has already taken its role snapshot.
        await release.wait()
        raise TimeoutError('controlled tool failure')
    tool = asyncio.create_task(main._mutate_and_sync_memory('r', failed_tool))
    await started.wait()
    try:
        if entrypoint == 'background':
            worker.retry_consumers('r')
        elif entrypoint == 'resume':
            await worker.resume_pending()
        else:
            settings = MemorySettingsStore(tmp_path / 'settings.json')
            settings.update(enabled=False)
            monkeypatch.setattr(main, 'memory_settings_store', settings)
            monkeypatch.setattr(main, 'memory_optimizer_loop', SimpleNamespace(start=lambda: None))
            await main.update_memory_settings(MemorySettings(enabled=True))
        await asyncio.to_thread(published.wait, .2)
        # The tool's semantic lock never owns host Markdown or read-only recall.
        await asyncio.wait_for(worker._maintain(worker._process_maintenance, 'r', 'role:r'), 1)
        engine = DefaultMemoryEngine(service, role_exists=lambda _: True)
        await asyncio.wait_for(engine.query(MemoryQuery('独立读取', scope=MemoryScope('r', 'role:r'))), 1)
    finally:
        release.set()
        with pytest.raises(TimeoutError):
            await tool
        await worker.drain()
    progress = sessions.maintenance_progress('r', 'role:r')
    assert progress['publishedVersion'] == 1 and not progress['pendingEvents']
    assert [item.summary for item in store.list_active('r')] == ['必须保留的独立消费者事实']
    assert store.has_consolidation_source('r', progress['lastSourceKey'])


@pytest.mark.anyio
async def test_each_publication_releases_semantic_lock_before_next_and_relationship(tmp_path, monkeypatch):
    sessions, store, _, maintenance, worker, _ = publication_fixture(tmp_path, monkeypatch)
    maintenance.maintain('r', 'role:r', force=True, through_sequence=10)
    maintenance.maintain('r', 'role:r', force=True, through_sequence=16)
    started, release, relationship_started, relationship_release = [threading.Event() for _ in range(4)]
    order = []
    original = maintenance.on_consolidation_committed
    def publish(event):
        order.append(event.message_range[1])
        if len(order) == 1:
            started.set()
            assert release.wait(3)
        original(event)
    def relationship(event):
        relationship_started.set()
        assert relationship_release.wait(3)
    maintenance.on_consolidation_committed = publish
    maintenance.relationship_consumer = relationship
    worker.retry_consumers('r')
    assert await asyncio.to_thread(started.wait, 1)
    async def between_entries():
        progress = sessions.maintenance_progress('r', 'role:r')
        assert progress['publishedVersion'] == 1 and len(progress['pendingEvents']) == 1
        order.append('tool')
    tool = asyncio.create_task(main._mutate_and_sync_memory('r', between_entries))
    try:
        await asyncio.sleep(.02)
        release.set()
        await asyncio.wait_for(tool, 1)
        assert await asyncio.to_thread(relationship_started.wait, 1)
        assert await asyncio.wait_for(main._mutate_and_sync_memory('r', lambda: 'relationship does not hold semantic lock'), 1)
    finally:
        release.set()
        relationship_release.set()
        await worker.drain()
    assert order == [10, 'tool', 16]
    assert sessions.maintenance_progress('r', 'role:r')['relationshipVersion'] == 2


@pytest.mark.anyio
@pytest.mark.parametrize('listening', [False, True])
@pytest.mark.parametrize('revoked', [False, True])
async def test_owner_publication_uses_same_rollback_lane_and_rechecks_source(tmp_path, monkeypatch, listening, revoked):
    sessions, store, _, _, worker, published = publication_fixture(tmp_path, monkeypatch)
    owner = main.owner_channel_memory
    proof = 'authorized'
    thread = SimpleNamespace(thread_id='group', role_id='r', session_key='thread:group')
    for name, value in {'sessions': sessions, 'roles_root': tmp_path / 'roles', 'tasks': {}, 'errors': [],
        'extract': lambda *_: {'history': '主人群聊事实', 'pending': '候选', 'history_entries': [], 'conversation': '事实'},
        'publish': worker.maintenance.on_consolidation_committed, 'verify': lambda *_: proof}.items():
        monkeypatch.setattr(owner, name, value)
    entered, release = asyncio.Event(), asyncio.Event()
    async def failed_tool():
        entered.set()
        await release.wait()
        raise TimeoutError('controlled rollback')
    tool = asyncio.create_task(main._mutate_and_sync_memory('r', failed_tool))
    await entered.wait()
    try:
        await owner.commit(thread, [{'id': 'owner-message', 'sequence': 1, 'createdAt': '2026-11-02T00:00:00+00:00'}], listening=listening)
        await asyncio.to_thread(published.wait, .2)
        if revoked:
            proof = None
    finally:
        release.set()
        with pytest.raises(TimeoutError):
            await tool
        await owner.drain()
    key = ('listening:' if listening else 'group-owner:') + 'group'
    progress = sessions.maintenance_progress('r', key)
    assert not progress['pendingEvents']
    if revoked:
        assert not published.is_set() and progress['invalidatedEvents']
        assert store.list_active('r') == []
    else:
        assert published.is_set() and len(store.list_active('r')) == 1
        assert store.has_consolidation_source('r', progress['lastSourceKey'])


@pytest.mark.anyio
@pytest.mark.parametrize('model_id', [None, ''])
@pytest.mark.parametrize('version', [1, 2])
async def test_legacy_unknown_vector_identity_is_unavailable_without_rebinding(tmp_path, model_id, version):
    path = tmp_path / 'memory.db'
    store = MemoryStore(path)
    item = store.add_or_reinforce('r', 'preference', '必须保留的旧记忆',
        MemorySourceRef(kind='manual', sessionKey='role:r', stableSourceKey='legacy'))
    store.set_embedding('r', item.id, [1., 0.], 'legacy')
    with sqlite3.connect(path) as db:
        db.execute('UPDATE memory_embedding_spaces SET model_id=? WHERE role_id=?', (model_id, 'r'))
        db.execute(f'PRAGMA user_version={version}')
    class Unrelated(ControlledEmbedding):
        model_id, dimensions = 'unrelated-model', 2
        def embed(self, *_):
            pytest.fail('Unavailable legacy space must not call embedding')
    for _ in range(2):
        imported = MemoryStore(path)
        service = configured_service(imported, Unrelated())
        assert not service.availability('r')[0]
        engine = DefaultMemoryEngine(service, role_exists=lambda _: True)
        assert (await engine.query(MemoryQuery('旧记忆', scope=MemoryScope('r', 'role:r')))).trace['disabled']
        with pytest.raises(ValueError, match='模型身份'):
            await engine.mutate(MemoryMutation('remember', MemoryScope('r', 'role:r'), summary='新记忆'))
        assert imported.embedding_for('r', item.id) == [1., 0.]
        assert [entry.id for entry in imported.list_all('r')] == [item.id]
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT model_id FROM memory_embedding_spaces WHERE role_id=?', ('r',)).fetchone()[0] == model_id


@pytest.mark.anyio
@pytest.mark.parametrize('model_id', [None, ''])
async def test_empty_unidentified_space_can_accept_its_first_configured_vector(tmp_path, model_id):
    path = tmp_path / 'memory.db'
    MemoryStore(path)
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO memory_embedding_spaces(role_id,dimension,model_id) VALUES (?,64,?)', ('r', model_id))
    store = MemoryStore(path)
    engine = DefaultMemoryEngine(configured_service(store), role_exists=lambda _: True)
    saved = await engine.mutate(MemoryMutation('remember', MemoryScope('r', 'role:r'), summary='第一次真实写入'))
    assert saved.accepted and store.embedding_for('r', saved.item_id)


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['preference', 'procedure'])
async def test_old_memory_supersede_and_procedure_merge_use_semantic_similarity(tmp_path, kind):
    class Embedding(ControlledEmbedding):
        dimensions = 2
        def embed(self, *_):
            return [1., 0.]
    store = MemoryStore(tmp_path / 'memory.db')
    metadata = {'tool_requirement': 'read_file', 'steps': ['核对来源']} if kind == 'procedure' else {}
    old = store.add_or_reinforce('r', kind, '旧的长期信息',
        MemorySourceRef(kind='manual', sessionKey='role:r', stableSourceKey='old'), extra=metadata)
    store.set_embedding('r', old.id, [.8, .6] if kind == 'procedure' else [1., 0.], Embedding.model_id)
    with sqlite3.connect(store.database_path) as db:
        db.execute('UPDATE memory_items SET updated_at=? WHERE id=?',
            ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), old.id))
    engine = DefaultMemoryEngine(configured_service(store, Embedding()), role_exists=lambda _: True)
    updated = await engine.mutate(MemoryMutation('remember', MemoryScope('r', 'role:r'),
        summary='新的长期信息', memory_kind=kind, metadata=metadata))
    if kind == 'procedure':
        assert updated.status == 'merged' and updated.item_id == old.id
        assert '旧的长期信息' in store.get('r', old.id).summary
        assert '新的长期信息' in store.get('r', old.id).summary
    else:
        assert updated.item_id != old.id
        assert store.get('r', old.id).status == 'superseded'
