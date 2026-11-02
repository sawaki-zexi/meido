"""Behavioral regressions against the fixed Shiori context/memory contract."""
import json
import pytest

from backend.app.memory_maintenance import MemoryMaintenance
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_backlog_failure_only_blocks_when_current_retention_threshold_is_reached(tmp_path):
    from backend.app.memory_service import MemoryWorker, MemoryService
    from backend.app.memory_store import MemoryStore
    sessions = transcript(tmp_path, count=6)
    keep = 8
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, recent_limit_provider=lambda: keep,
        consolidation_provider=lambda *_: {'history': '事实', 'pendingItems': []})
    worker = MemoryWorker(MemoryService(MemoryStore(tmp_path / 'memory.db')), maintenance)
    maintenance.failures['r'] = 'controlled previous extraction failure'
    assert maintenance.minimum_new_messages == 5
    await worker.before_turn('r', 'role:r')  # Twelve pending messages < eight + five.
    sessions.append_message('role:r', 'user', 'more')
    sessions.append_message('role:r', 'assistant', 'reply')
    with pytest.raises(ValueError, match='controlled previous'):
        await worker.before_turn('r', 'role:r')
    maintenance.failures['r'] = ''
    with pytest.raises(ValueError, match='整理请求失败'):
        await worker.before_turn('r', 'role:r')
    keep = 40
    assert maintenance.minimum_new_messages == 20
    await worker.before_turn('r', 'role:r')
    keep = 8
    sessions.delete_role_session('r')
    sessions.open_role_session('r')
    await worker.before_turn('r', 'role:r')
    assert not worker._tasks


@pytest.mark.parametrize('error,reason', [(TimeoutError(), 'TimeoutError'), (RuntimeError('HTTP 401'), 'HTTP 401')])
def test_model_failure_is_recorded_with_a_nonempty_host_reason(tmp_path, error, reason):
    sessions = transcript(tmp_path)
    def timeout(*_):
        raise error
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, consolidation_provider=timeout)
    maintenance.maintain('r', 'role:r')
    assert maintenance.failures['r'] == reason
    assert sessions.maintenance_progress('r', 'role:r')['lastSequence'] == 0


@pytest.mark.anyio
async def test_fixed_model_credentials_resume_failed_markdown_without_restart(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend.app import main
    from backend.app.model_config import ModelConfigurationStore
    from backend.app.models import ModelConfigurationInput
    from backend.app.memory_service import MemoryWorker, MemoryService
    from backend.app.memory_store import MemoryStore
    sessions = transcript(tmp_path)
    configurations = ModelConfigurationStore(tmp_path / 'models.json')
    config = configurations.save(ModelConfigurationInput(providerId='custom', provider='custom', baseUrl='https://model.test', model='fixture', apiKey='old'))
    role = _memory_role()
    monkeypatch.setattr(main, 'model_configuration_store', configurations)
    monkeypatch.setattr(main, 'store', SimpleNamespace(get=lambda role_id: role if role_id == role.id else None))
    def model(*_):
        configuration = main._role_model_configuration(role.id)
        assert configuration is not None and configuration.id == config.id
        if configuration.apiKey == 'old':
            raise TimeoutError()
        return {'history': '恢复后整理的事实', 'pendingItems': []}
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, consolidation_provider=model,
        role_exists=lambda role_id: main.store.get(role_id) is not None,
        configuration_generation=main._memory_configuration_generation)
    worker = MemoryWorker(MemoryService(MemoryStore(tmp_path / 'memory.db')), maintenance)
    await worker.before_turn('r', 'role:r')
    await worker.drain()
    with pytest.raises(ValueError, match='TimeoutError'):
        await worker.before_turn('r', 'role:r')
    failed_generation = main._memory_configuration_generation(role.id)
    configurations.update(config.id, config.model_copy(update={'apiKey': 'fixed'}))
    assert main._memory_configuration_generation(role.id) != failed_generation
    await worker.before_turn('r', 'role:r')
    await worker.drain()
    assert not maintenance.failures
    assert sessions.maintenance_progress('r', 'role:r')['lastSequence'] > 0


def _memory_role(**binding):
    from datetime import datetime, timezone
    from backend.app.models import Role
    now = datetime.now(timezone.utc)
    return Role(id='r', name='fixture', profile={'profile': 'fixture'},
        createdAt=now, updatedAt=now, **binding)


@pytest.mark.parametrize('binding', [None, {'modelBindingRequired': True}, {'modelConfigurationId': 'deleted'}],
    ids=['missing-role', 'unresolved-legacy', 'missing-canonical'])
def test_unavailable_role_memory_configuration_never_inherits_active_key_changes(tmp_path, monkeypatch, binding):
    from types import SimpleNamespace
    from backend.app import main
    from backend.app.model_config import ModelConfigurationStore
    from backend.app.models import ModelConfigurationInput
    configurations = ModelConfigurationStore(tmp_path / 'models.json')
    config = configurations.save(ModelConfigurationInput(providerId='custom', provider='custom',
        baseUrl='https://model.test', model='fixture', apiKey='old'))
    role = _memory_role(**binding) if binding is not None else None
    monkeypatch.setattr(main, 'model_configuration_store', configurations)
    monkeypatch.setattr(main, 'store', SimpleNamespace(get=lambda role_id: role if role_id == 'r' else None))
    maintenance = MemoryMaintenance(tmp_path / 'roles', transcript(tmp_path),
        configuration_generation=main._memory_configuration_generation)
    generation = maintenance.refresh_configuration('r')
    maintenance.failures['r'] = '模型连接不可用，请重新选择模型'
    assert main._role_model_configuration('r') is None

    configurations.update(config.id, config.model_copy(update={'apiKey': 'fixed'}))

    assert main._role_model_configuration('r') is None
    assert maintenance.refresh_configuration('r') == generation
    assert maintenance.failures['r'] == '模型连接不可用，请重新选择模型'


@pytest.mark.parametrize('old_success', [False, True])
def test_old_markdown_generation_cannot_replace_or_clear_new_failure(tmp_path, old_success):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    sessions = transcript(tmp_path)
    generation = 'old'
    started, release = threading.Event(), threading.Event()
    def model(*_):
        started.set()
        assert release.wait(3)
        if old_success:
            return {'history': '有效来源的旧配置草稿', 'pendingItems': []}
        raise ValueError('old failure')
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, consolidation_provider=model,
        configuration_generation=lambda _: generation)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(maintenance.maintain, 'r', 'role:r')
        assert started.wait(1)
        generation = 'new'
        maintenance.refresh_configuration('r')
        maintenance.failures['r'] = 'new failure'
        release.set()
        pending.result(timeout=2)
    assert maintenance.failures['r'] == 'new failure'
    assert (sessions.maintenance_progress('r', 'role:r')['lastSequence'] > 0) == old_success


@pytest.mark.anyio
async def test_stopped_coverage_waiter_leaves_independent_background_draft_running(tmp_path, monkeypatch):
    import asyncio
    import threading
    from backend.app import main
    from backend.app.memory_service import MemoryWorker, MemoryService
    from backend.app.memory_store import MemoryStore
    from backend.app.memory_engine import MemoryScope, MemoryCoverageRequest
    sessions = transcript(tmp_path)
    started, closed, release = threading.Event(), threading.Event(), threading.Event()
    def model(source, messages, *, commit_guard):
        async def request():
            started.set()
            try:
                while not release.is_set():
                    await asyncio.sleep(.01)
                return {'history': '后台事实', 'pendingItems': []}
            finally:
                closed.set()
        return asyncio.run(commit_guard.run(request()))
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, consolidation_provider=model)
    worker = MemoryWorker(MemoryService(MemoryStore(tmp_path / 'memory.db')), maintenance)
    monkeypatch.setattr(main, 'memory_worker', worker)
    monkeypatch.setattr(main, 'memory_maintenance', maintenance)
    await worker.before_turn('r', 'role:r')
    assert await asyncio.to_thread(started.wait, 1)
    ids = tuple(message.id for message in sessions.context_messages('role:r')[:16])
    coverage = asyncio.create_task(main._cover_markdown_window(MemoryCoverageRequest(MemoryScope('r', 'role:r'), 'role:r', ids)))
    unrelated = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(.02)
    assert worker.role_lock_for('r').locked() and not coverage.done()
    coverage.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(coverage, .5)
    assert not closed.is_set()
    assert not unrelated.done()
    unrelated.cancel()
    await asyncio.gather(unrelated, return_exceptions=True)
    release.set()
    await asyncio.wait_for(worker.drain(), 1)
    assert closed.is_set() and not maintenance.failures
    assert sessions.maintenance_progress('r', 'role:r')['lastSequence'] > 0
    assert '后台事实' in (tmp_path / 'roles/r/memory/HISTORY.md').read_text(encoding='utf-8')


def transcript(tmp_path, count=18):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('r')
    for index in range(count):
        sessions.append_message('role:r', 'user', f'我喜欢茶 {index}')
        sessions.append_message('role:r', 'assistant', f'记下了 {index}')
    return sessions


def test_covered_memory_survives_consumer_failure_and_restart(tmp_path):
    sessions = transcript(tmp_path)
    extracted, published, relationships = [], [], []

    def extract(source, messages):
        extracted.append(source)
        return {'history': '共同经历', 'pendingItems': []}

    def relationship(event):
        relationships.append(event.source_key)
        raise RuntimeError('relationship unavailable')

    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions,
        on_consolidation_committed=published.append, consolidation_provider=extract,
        relationship_consumer=relationship)
    ids = [m.id for m in sessions.context_messages('role:r')[:16]]
    assert maintenance.ensure_coverage('r', 'role:r', ids)
    assert len(extracted) == len(published) == len(relationships) == 1
    progress = sessions.maintenance_progress('r', 'role:r')
    assert progress['memoryVersion'] == progress['publishedVersion'] == 1
    assert progress.get('relationshipVersion', 0) == 0
    resumed = MemoryMaintenance(tmp_path / 'roles', SessionStore(tmp_path / 'sessions.db'),
        on_consolidation_committed=published.append, consolidation_provider=extract,
        relationship_consumer=lambda event: None)
    resumed.resume_pending()
    assert resumed.ensure_coverage('r', 'role:r', ids)
    assert len(extracted) == len(published) == 1
    assert sessions.maintenance_progress('r', 'role:r')['relationshipVersion'] == 1
    assert not (tmp_path / 'roles/r/memory/.maintenance.json').exists()


def test_maintenance_accepts_tail_append_but_rejects_changed_prefix(tmp_path):
    sessions = transcript(tmp_path)
    original = sessions.context_messages('role:r')

    def append(source, messages):
        sessions.append_message('role:r', 'user', '追加')
        sessions.append_message('role:r', 'assistant', '保留')
        return {'history': '共同经历', 'pendingItems': []}

    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, consolidation_provider=append)
    assert maintenance.ensure_coverage('r', 'role:r', [m.id for m in original[:16]])
    assert len(sessions.context_messages('role:r')) == len(original) + 2
    assert sessions.maintenance_progress('r', 'role:r')['lastSequence'] == 16
    import sqlite3
    with sqlite3.connect(sessions.database_path) as db:
        db.execute('UPDATE messages SET content=? WHERE message_id=?', ('已编辑', original[0].id))
    maintenance.consolidation_provider = lambda *_: None
    assert not maintenance.ensure_coverage('r', 'role:r', [original[0].id])


def test_legacy_pending_import_is_once_and_preserves_original(tmp_path):
    sessions = transcript(tmp_path, count=2)
    memory = tmp_path / 'roles/r/memory'
    memory.mkdir(parents=True)
    original = json.dumps({'lastSequence': 0, 'pendingEvent': {
        'roleId': 'r', 'sessionKey': 'role:r', 'sourceKey': 'legacy',
        'messageIds': [], 'messageRange': None, 'candidates': []}})
    (memory / '.maintenance.json').write_text(original, encoding='utf-8')
    published = []
    first = MemoryMaintenance(tmp_path / 'roles', sessions, published.append)
    first.resume_pending()
    first.resume_pending()
    assert not published
    assert sessions.maintenance_progress('r', 'role:r')['unverifiedLegacyEvent']['sourceKey'] == 'legacy'
    assert (memory / '.maintenance.json.migrated').read_text(encoding='utf-8') == original
    assert not (memory / '.maintenance.json').exists()


@pytest.mark.anyio
async def test_memory_coverage_precedes_summary_and_hard_failures_propagate(tmp_path):
    from test_context_windows import setup_window
    from backend.app.agent_runtime.context_provider import ContextWindowProvider
    from backend.app.agent_runtime.provider import CancellationToken
    sessions, adapter, base, windows, settings = setup_window(tmp_path, 'invalid')
    base.configuration = base.configuration.model_copy(update={'contextWindowTokens': 4000})
    order = []
    original = adapter.stream_rich_messages
    async def recording(*args, **kwargs):
        order.append('model')
        async for item in original(*args, **kwargs):
            yield item
    adapter.stream_rich_messages = recording
    async def coverage(ids):
        order.append('memory')
        return True
    window = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r', scope='owner',
        authority='a', authorized=lambda: True, ensure_coverage=coverage)
    with pytest.raises(ValueError):
        _ = [event async for event in window.stream_response(model='fixture', system='',
            messages=sessions.runtime_messages('role:r'), tools=[], signal=CancellationToken())]
    assert order[0] == 'memory'
    assert adapter.summaries == len(adapter.calls)
    assert windows.load('r', 'role:r', 'owner', 'a')['removed'] == []


def test_stable_documents_and_history_precede_dynamic_frame(tmp_path):
    from test_context_windows import setup_window
    sessions, _, base, _, _ = setup_window(tmp_path)
    base.stable_memory = '稳定记忆' * 2000
    base.memory_context = '动态召回'
    messages = sessions.runtime_messages('role:r')
    first = base.prepare_request('', messages, [])
    assert base.stable_memory in first.messages[0].content[0].text
    base.memory_context = '另一条动态召回'
    second = base.prepare_request('', messages, [])
    assert first.messages[:-2] == second.messages[:-2]
    assert first.messages[-1].content[0].text == '继续'
    assert '动态召回' in first.messages[-2].content[0].text


@pytest.mark.anyio
async def test_stop_cancels_markdown_draft_without_waiting_or_late_commit(tmp_path, monkeypatch):
    import asyncio
    import threading
    from backend.app import main
    from backend.app.memory_service import MemoryWorker
    from backend.app.memory_engine import MemoryScope, MemoryCoverageRequest
    from memory_fakes import configured_service
    from backend.app.memory_store import MemoryStore
    sessions = transcript(tmp_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def model(*_):
        started.set()
        release.wait(5)
        finished.set()
        return {'history': '迟到结果', 'pendingItems': []}
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, consolidation_provider=model)
    worker = MemoryWorker(configured_service(MemoryStore(tmp_path / 'memory.db')), maintenance)
    monkeypatch.setattr(main, 'memory_worker', worker)
    monkeypatch.setattr(main, 'memory_maintenance', maintenance)
    ids = tuple(m.id for m in sessions.context_messages('role:r')[:16])
    task = asyncio.create_task(main._cover_markdown_window(MemoryCoverageRequest(MemoryScope('r', 'role:r'), 'role:r', ids)))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, .5)
        assert not (tmp_path / 'roles/r/memory/HISTORY.md').exists()
        assert not sessions.maintenance_progress('r', 'role:r').get('lastSequence')
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(.05)
    assert not (tmp_path / 'roles/r/memory/HISTORY.md').exists()
    assert not sessions.maintenance_progress('r', 'role:r').get('lastSequence')
    assert not maintenance.failures


def test_optimizer_archive_during_draft_preserves_both_commits(tmp_path):
    import asyncio
    from backend.app.host_memory_optimizer import HostMemoryOptimizer
    sessions = transcript(tmp_path)
    folder = tmp_path / 'roles/r/memory'
    folder.mkdir(parents=True)
    (folder / 'PENDING.md').write_text('- [identity] 已有候选\n', encoding='utf-8')
    def request(role, system, prompt, tokens):
        return ('# 我的长期记忆\n## 关于你\n- 已有候选' if tokens == 16384 else
                '# 我是谁\n## 我的性格与形象\n- 温和\n## 我对你的理解\n## 我们的关系\n')
    optimizer = HostMemoryOptimizer(tmp_path / 'roles', request, step_delay_seconds=0)
    def model(*_):
        asyncio.run(optimizer.optimize('r'))
        return {'history': '新一轮经历', 'pending': '- [identity] 新一轮候选', 'pendingItems': []}
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions, consolidation_provider=model)
    assert maintenance.ensure_coverage('r', 'role:r', [m.id for m in sessions.context_messages('role:r')[:16]])
    assert '已有候选' in (folder / 'MEMORY.md').read_text(encoding='utf-8')
    assert '新一轮候选' in (folder / 'PENDING.md').read_text(encoding='utf-8')
    assert '新一轮经历' in (folder / 'HISTORY.md').read_text(encoding='utf-8')
    assert sessions.maintenance_progress('r', 'role:r')['lastSequence'] == 16


def test_session_deletion_and_rollback_include_independent_maintenance(tmp_path):
    sessions = transcript(tmp_path, count=2)
    progress = {'memoryVersion': 1, 'pendingEvents': [{'version': 1, 'payload': {'source': 'saved'}}]}
    sessions.save_maintenance_progress('r', 'role:r', progress)
    sessions.save_maintenance_progress('r', 'listening:thread', {'lastSource': 'listening-saved'})
    snapshot = sessions.snapshot_role_session('r')
    sessions.delete_role_session('r')
    assert sessions.maintenance_progress_entries('r') == []
    sessions.restore_role_session('r', snapshot)
    assert sessions.maintenance_progress('r', 'role:r') == progress
    assert sessions.maintenance_progress('r', 'listening:thread')['lastSource'] == 'listening-saved'
    assert len(sessions.context_messages('role:r')) == 4
    sessions.delete_role_session('r')
    sessions.open_role_session('r')
    assert sessions.maintenance_progress_entries('r') == []


def test_repeated_legacy_restore_preserves_both_original_files(tmp_path):
    sessions = transcript(tmp_path, count=1)
    folder = tmp_path / 'roles/r/memory'
    folder.mkdir(parents=True)
    path = folder / '.maintenance.json'
    original = json.dumps({'lastSequence': 0, 'pendingEvent': {'sourceKey': 'first'}})
    restored = json.dumps({'lastSequence': 0, 'pendingEvent': {'sourceKey': 'restored'}})
    path.write_text(original, encoding='utf-8')
    maintenance = MemoryMaintenance(tmp_path / 'roles', sessions)
    maintenance.resume_pending()
    path.write_text(restored, encoding='utf-8')
    maintenance.resume_pending()
    saved = [entry.read_text(encoding='utf-8') for entry in folder.glob('.maintenance.json*')]
    assert original in saved and restored in saved
    assert not path.exists()


def test_migration_recovers_interrupted_copy_and_preserves_documents_and_database(tmp_path, monkeypatch):
    import sqlite3
    from backend.app import context_memory_migration as migration
    sessions = transcript(tmp_path, count=1)
    messages = sessions.context_messages('role:r')
    folder = tmp_path / 'roles/r/memory'
    folder.mkdir(parents=True)
    original = '手工段落\n<!-- meido-optimizer:start -->旧托管文本<!-- meido-optimizer:end -->\n'
    (folder / 'MEMORY.md').write_text(original, encoding='utf-8')
    (folder / 'PENDING.snapshot.md').write_text('待恢复候选', encoding='utf-8')
    (tmp_path / 'embedding-config.json').write_text('{"model":"test:latest"}', encoding='utf-8')
    replace = migration.os.replace
    def interrupted(source, target):
        if str(target).endswith('context-memory-v2.json'):
            raise OSError('controlled interruption before final marker')
        replace(source, target)
    monkeypatch.setattr(migration.os, 'replace', interrupted)
    with pytest.raises(OSError, match='interruption'):
        migration.migrate_context_memory(tmp_path, tmp_path / 'roles')
    monkeypatch.setattr(migration.os, 'replace', replace)
    migration.migrate_context_memory(tmp_path, tmp_path / 'roles')
    migration.migrate_context_memory(tmp_path, tmp_path / 'roles')
    backup = tmp_path / 'backups/context-memory-v1'
    assert (backup / 'roles/r/memory/MEMORY.md').read_text(encoding='utf-8') == original
    assert (folder / 'MEMORY.md').read_text(encoding='utf-8') == '手工段落\n旧托管文本\n'
    assert (folder / 'PENDING.snapshot.md').read_text(encoding='utf-8') == '待恢复候选'
    assert (tmp_path / 'embedding-config.json').read_bytes() == (tmp_path / 'memory-embedding-config.json').read_bytes()
    with sqlite3.connect(backup / 'sessions.db') as db:
        assert {row[0] for row in db.execute('SELECT message_id FROM messages')} == {m.id for m in messages}
