"""Background listening upkeep is timely, retryable and source scoped."""
import asyncio
import json
from datetime import datetime
from time import perf_counter
from types import SimpleNamespace

import pytest

from backend.app.channels.consolidation import ChannelMemoryMaintenance
from backend.app.multichannel import MultichannelStore
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app import main
from backend.app.models import RoleInput, RoleProfile
from backend.app.owner_channel_memory import OwnerChannelMemory
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from test_consolidation_parity import _maintenance


class Clock:
    def __init__(self):
        self.now = datetime.now().astimezone().timestamp()
        self.waiters = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, future))
        await future

    async def advance(self, seconds):
        self.now += seconds
        for until, future in self.waiters:
            if until <= self.now and not future.done():
                future.set_result(None)
        for _ in range(12):
            await asyncio.sleep(0)


def cursor(store, thread):
    with store._connect() as db:
        row = db.execute('SELECT sequence FROM channel_listening_cursors WHERE thread_id=?',
            (thread.thread_id,)).fetchone()
    return row[0] if row else 0


def hear(store, thread, clock, number):
    return store.listening.hear(thread, f'群友事实 {number}', str(number),
        {'senderId': 'visitor', 'senderIsOwner': False},
        timestamp=datetime.fromtimestamp(clock(), tz=datetime.now().astimezone().tzinfo))


def test_same_day_small_backlog_is_consolidated_after_idle(tmp_path):
    calls = []

    async def summarize(_thread, messages, *_):
        calls.append([message['id'] for message in messages])
        return {'groupNote': '空闲后已整理', 'members': []}

    store, thread, groups, worker, _ = _maintenance(tmp_path, summarize)
    clock = Clock()
    worker.clock, worker.sleep = clock, clock.sleep
    hear(store, thread, clock, 1)

    async def run():
        try:
            await worker.start()
            await clock.advance(0)
            assert not calls
            await clock.advance(90)
            assert not calls
            await clock.advance(30)
            assert len(calls) == 1 and cursor(store, thread) == 1
            assert groups.read_context('role', thread.thread_id)['groupNote'] == '空闲后已整理'
            await clock.advance(120)
            assert len(calls) == 1
        finally:
            await worker.close()

    asyncio.run(run())


def test_recent_window_eviction_consolidates_under_a_full_batch(tmp_path):
    calls = []

    async def summarize(_thread, messages, *_):
        calls.append(messages)
        return {'summary': '窗口之前的事实已整理', 'members': []}

    store, thread, _, worker, _ = _maintenance(tmp_path, summarize)
    clock = Clock()
    worker.clock, worker.sleep = clock, clock.sleep

    async def run():
        try:
            for number in range(31):
                hear(store, thread, clock, number)
                worker.heard(thread)
            await clock.advance(0)
            assert len(calls) == 1 and len(calls[0]) == 31
            assert cursor(store, thread) == 31
        finally:
            await worker.close()

    asyncio.run(run())


def test_addressed_conversation_can_push_listening_out_of_recent_window(tmp_path):
    calls = []

    async def summarize(_thread, messages, *_):
        calls.append(messages)
        return {'members': []}

    store, thread, _, worker, _ = _maintenance(tmp_path, summarize)
    clock = Clock()
    worker.clock = clock
    original = hear(store, thread, clock, 0)
    for number in range(30):
        store.append_event(thread, direction='inbound', content='当前群对话',
            external_message_id=f'addressed-{number}', metadata={'senderId': 'visitor'})
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert [item['id'] for item in calls[0]] == [original['id']]
    assert cursor(store, thread) == 1


def test_disable_and_reenable_during_model_wait_rejects_old_commit(tmp_path):
    owners = []

    async def summarize(*_):
        store.set_group_listening('role', 'qq', 'group', False)
        store.set_group_listening('role', 'qq', 'group', True)
        return {'groupNote': '已失效的提取', 'members': []}

    async def owner_statements(*args, authorized):
        assert authorized()
        owners.append(args)

    store, thread, groups, worker, _ = _maintenance(tmp_path, summarize, owner_statements)
    for number in range(50):
        store.listening.hear(thread, '主人事实', str(number),
            {'senderId': 'owner', 'senderIsOwner': True})
    assert not asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    assert not owners and cursor(store, thread) == 0
    assert groups.read_context('role', thread.thread_id)['groupNote'] == ''


@pytest.mark.parametrize('failure', ['timeout', 'edit', 'cancel'])
def test_failed_batch_retries_after_cooldown_without_new_messages(tmp_path, failure):
    calls = []

    async def summarize(_thread, messages, *_):
        calls.append(messages)
        if len(calls) == 1:
            if failure == 'timeout':
                raise TimeoutError('controlled model timeout')
            if failure == 'cancel':
                raise asyncio.CancelledError()
            groups.update_context('role', thread.thread_id, group_note='主人刚改过的笔记')
        return {'groupNote': '有效整理', 'members': []}

    store, thread, groups, worker, _ = _maintenance(tmp_path, summarize)
    clock = Clock()
    worker.clock, worker.sleep = clock, clock.sleep
    for number in range(31):
        hear(store, thread, clock, number)

    async def run():
        try:
            await worker.start()
            await clock.advance(0)
            assert len(calls) == 1 and cursor(store, thread) == 0
            expected = '主人刚改过的笔记' if failure == 'edit' else ''
            assert groups.read_context('role', thread.thread_id)['groupNote'] == expected
            for _ in range(5):
                worker.heard(thread)
            await clock.advance(29)
            assert len(calls) == 1
            await clock.advance(1)
            assert len(calls) == 2 and cursor(store, thread) == 31
            assert groups.read_context('role', thread.thread_id)['groupNote'] == '有效整理'
        finally:
            await worker.close()

    asyncio.run(run())


def test_concurrent_arrivals_share_one_model_and_cooldown_starts_after_failure(tmp_path):
    entered, release = None, None
    calls = []

    async def summarize(_thread, messages, *_):
        calls.append(messages)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            raise TimeoutError('slow model failed')
        return {'members': []}

    store, thread, _, worker, _ = _maintenance(tmp_path, summarize)
    clock = Clock()
    worker.clock, worker.sleep = clock, clock.sleep
    for number in range(31):
        hear(store, thread, clock, number)

    async def run():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            await worker.start()
            await entered.wait()
            await clock.advance(60)
            hear(store, thread, clock, 31)
            for _ in range(5):
                worker.heard(thread)
            assert len(worker._tasks) == 1 and len(calls) == 1
            release.set()
            await clock.advance(0)
            worker.heard(thread)
            await clock.advance(29)
            assert len(calls) == 1 and cursor(store, thread) == 0
            await clock.advance(1)
            assert len(calls) == 2 and cursor(store, thread) == 32
        finally:
            await worker.close()

    asyncio.run(run())


def test_restart_resumes_after_successful_prefix_and_failed_later_batch(tmp_path, record_property):
    calls = []

    async def summarize(_thread, messages, *_):
        calls.append([message['sequence'] for message in messages])
        if len(calls) == 2:
            raise TimeoutError('second batch failed')
        return {'summary': f'已整理到 {messages[-1]["sequence"]}', 'members': []}

    store, thread, groups, worker, _ = _maintenance(tmp_path, summarize)
    clock = Clock()
    worker.clock, worker.sleep = clock, clock.sleep
    for number in range(100):
        hear(store, thread, clock, number)

    async def run():
        started = perf_counter()
        await worker.start()
        await clock.advance(0)
        assert cursor(store, thread) == 50 and calls == [list(range(1, 51)), list(range(51, 101))]
        assert groups.read_context('role', thread.thread_id)['summary'] == '已整理到 50'
        await worker.close()
        restarted = MultichannelStore(store.sessions.database_path, store.sessions)
        resumed = ChannelMemoryMaintenance(lambda: restarted, lambda: MultichannelGroupRepository(restarted), summarize, lambda *_: None)
        resumed.clock, resumed.sleep = clock, clock.sleep
        try:
            await resumed.start()
            await clock.advance(0)
            assert cursor(restarted, thread) == 100 and calls[-1] == list(range(51, 101))
            assert len(calls) == 3
            record_property('listening_replay', json.dumps({'messages': 100, 'batch_size': worker.BATCH_SIZE,
                'idle_seconds': worker.IDLE_SECONDS, 'retry_seconds': worker.RETRY_SECONDS,
                'consolidation_calls': len(calls), 'successful_batches': 2, 'remaining_backlog': 0,
                'elapsed_seconds': round(perf_counter() - started, 3)}))
        finally:
            await resumed.close()

    asyncio.run(run())


@pytest.mark.parametrize('invalidate', ['clear', 'delete', 'disable'])
def test_inflight_batch_cannot_commit_after_source_invalidation(tmp_path, invalidate):
    entered, release = None, None

    async def summarize(*_):
        entered.set()
        await release.wait()
        return {'groupNote': '失效资料', 'members': []}

    store, thread, groups, worker, _ = _maintenance(tmp_path, summarize)
    for number in range(50):
        store.listening.hear(thread, '待整理', str(number), {'senderId': 'visitor'})

    async def run():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        worker.request(thread.thread_id)
        await entered.wait()
        if invalidate == 'clear':
            store.listening.clear('role', thread.thread_id)
        elif invalidate == 'disable':
            store.set_group_listening('role', 'qq', 'group', False)
        else:
            await worker.begin_role_deletion('role')
        release.set()
        await asyncio.gather(*tuple(worker._tasks.values()), return_exceptions=True)
        assert cursor(store, thread) == 0
        assert groups.read_context('role', thread.thread_id)['groupNote'] == ''
        await worker.close()

    asyncio.run(run())


def test_owner_and_members_share_daily_cap_and_only_durable_rows_are_consolidated(tmp_path):
    consolidated, owner_rows = [], []

    async def summarize(_thread, messages, *_):
        consolidated.extend(messages)
        return {'members': []}

    async def owner_statements(_thread, messages, *, authorized):
        assert authorized()
        owner_rows.extend(messages)

    store, thread, _, worker, _ = _maintenance(tmp_path, summarize, owner_statements)
    for number in range(231):
        owner = number % 2 == 0
        result = store.listening.hear(thread, str(number), str(number),
            {'senderId': 'owner' if owner else 'visitor', 'senderIsOwner': owner})
        assert (result['sequence'] is not None) == (number < 200)
    recent = store.listening.recent('role', thread.thread_id)
    assert len(recent) == 30 and recent[0]['content'] == '201'
    late = store.listening.hear(thread, '超额成员资料也不落盘', 'late-member',
        {'senderId': 'late-member', 'senderName': '超额成员', 'senderIsOwner': False})
    assert late['sequence'] is None
    with store._connect() as db:
        assert db.execute(
            'SELECT 1 FROM channel_external_member_sources s JOIN channel_external_members m '
            'ON m.role_id=s.role_id AND m.member_key=s.member_key '
            'WHERE s.thread_id=? AND m.sender_id=?',
            (thread.thread_id, 'late-member')).fetchone() is None

    async def run():
        try:
            worker.heard(thread)
            await asyncio.gather(*tuple(worker._tasks.values()))
            assert len(consolidated) == 200 and cursor(store, thread) == 200
            assert len(owner_rows) == 100
            assert all(message['sequence'] is not None for message in consolidated)
        finally:
            await worker.close()

    asyncio.run(run())


def owner_setup(tmp_path, monkeypatch, extract):
    roles = RoleStore(tmp_path / 'roles')
    role = roles.create(RoleInput(name='角色', profile=RoleProfile(profile='设定')))
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    channels.register_channel_account(role.id, 'qq', account_id='qq', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread(role.id, 'qq', 'qq', 'group', 'group')
    channels.set_group_listening(role.id, 'qq', 'group', True)
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id=role.id, channel='qq', scope='platform',
        platform_user_id='owner', account_id='qq', chat_id='group')
    for name, value in {'store': roles, 'session_store': sessions, 'multichannel_store': channels,
            'memory_worker': SimpleNamespace(role_deletion_blocked=lambda _: False)}.items():
        monkeypatch.setattr(main, name, value)
    owner = OwnerChannelMemory(sessions, roles.root, extract, lambda *_: None, main._verify_owner_channel_memory)
    monkeypatch.setattr(main, 'owner_channel_memory', owner)
    return channels, thread, owner, roles.root / role.id / 'memory'


@pytest.mark.parametrize('invalidate', ['clear', 'toggle'])
def test_owner_document_commit_rechecks_listening_generation_after_extraction(tmp_path, monkeypatch, invalidate):
    def extract(*_):
        if invalidate == 'clear':
            channels.listening.clear(thread.role_id, thread.thread_id)
        else:
            channels.set_group_listening(thread.role_id, 'qq', 'group', False)
            channels.set_group_listening(thread.role_id, 'qq', 'group', True)
        return {'history': '不能落盘的主人事实', 'pending': '- 不能落盘的主人事实',
            'history_entries': [('不能落盘的主人事实', 0)], 'conversation': '主人原话'}

    channels, thread, owner, folder = owner_setup(tmp_path, monkeypatch, extract)
    message = channels.listening.hear(thread, '主人原话', 'owner-1', {'senderId': 'owner', 'senderIsOwner': True})

    async def run():
        with pytest.raises(PermissionError):
            await main._ingest_owner_listening_memory(thread, [message])
        await owner.drain()
        assert '不能落盘' not in (folder / 'HISTORY.md').read_text(encoding='utf-8')
        assert '不能落盘' not in (folder / 'PENDING.md').read_text(encoding='utf-8')
        assert channels.sessions.maintenance_progress(thread.role_id, 'listening:' + thread.thread_id) == {'lastSequence': 0}

    asyncio.run(run())


def test_larger_retry_does_not_extract_successful_owner_prefix_again(tmp_path, monkeypatch):
    extracted = []

    def extract(_role, messages):
        extracted.append([message['sequence'] for message in messages])
        text = '、'.join(message['content'] for message in messages)
        return {'history': text, 'pending': text, 'history_entries': [(text, 0)], 'conversation': text}

    channels, thread, owner, folder = owner_setup(tmp_path, monkeypatch, extract)
    messages = [channels.listening.hear(thread, f'主人事实{number}', str(number),
        {'senderId': 'owner', 'senderIsOwner': True}) for number in range(3)]

    async def run():
        await owner.commit(thread, messages[:2])
        await owner.drain()
        await owner.commit(thread, messages)
        await owner.drain()
        await owner.commit(thread, messages)
        await owner.drain()
        assert extracted == [[1, 2], [3]]
        assert (folder / 'PENDING.md').read_text(encoding='utf-8').count('主人事实0') == 1
        assert channels.sessions.maintenance_progress(thread.role_id, 'listening:' + thread.thread_id)['lastSequence'] == 3

    asyncio.run(run())


@pytest.mark.parametrize('change', ['toggle', 'edit'])
def test_owner_callback_keeps_original_batch_generation(tmp_path, monkeypatch, change):
    extracted = []

    def extract(*_):
        extracted.append(True)
        return {'history': '过期事实', 'pending': '过期事实', 'history_entries': [], 'conversation': ''}

    channels, thread, owner, folder = owner_setup(tmp_path, monkeypatch, extract)

    async def summarize(*_):
        return {'members': []}

    async def owner_statements(thread, messages, *, authorized):
        if change == 'toggle':
            channels.set_group_listening(thread.role_id, 'qq', 'group', False)
            channels.set_group_listening(thread.role_id, 'qq', 'group', True)
        else:
            with channels._connect() as db:
                db.execute('UPDATE channel_listening_records SET content=? WHERE id=?',
                    ('已修正的主人原话', messages[0]['id']))
        await main._ingest_owner_listening_memory(thread, messages, authorized=authorized)

    worker = ChannelMemoryMaintenance(lambda: channels, lambda: MultichannelGroupRepository(channels),
        summarize, lambda *_: None, owner_statements=owner_statements)
    for number in range(31):
        channels.listening.hear(thread, '主人事实', str(number), {'senderId': 'owner', 'senderIsOwner': True})

    async def run():
        with pytest.raises(PermissionError):
            await worker.consolidate(thread.thread_id, listening=True)
        await owner.drain()
        assert not extracted and cursor(channels, thread) == 0
        assert '过期事实' not in (folder / 'PENDING.md').read_text(encoding='utf-8')

    asyncio.run(run())


def test_explicit_conversation_coverage_can_still_flush_a_short_prefix(tmp_path):
    observed = []

    async def summarize(_thread, messages, *_):
        observed.extend(message['id'] for message in messages)
        return {'members': []}

    store, thread, _, worker, _ = _maintenance(tmp_path, summarize)
    messages = [store.append_event(thread, direction='inbound', content='需要覆盖的对话',
        external_message_id=str(number), metadata={'senderId': 'visitor'})[1] for number in range(2)]
    ids = [message.id for message in messages]
    assert asyncio.run(worker.ensure_coverage('role', thread.session_key, ids))
    assert observed == ids and cursor(store, thread) == 0


@pytest.mark.parametrize('revoked', [False, True])
def test_new_batch_routes_owner_candidates_using_admitted_binding(tmp_path, monkeypatch, revoked):
    extracted, profiles_seen = [], []

    def extract(*_):
        extracted.append(True)
        assert not revoked
        return {'history': '配对期间的主人原话', 'pending': '配对期间的主人原话',
            'history_entries': [('配对期间的主人原话', 0)], 'conversation': '配对期间的主人原话'}

    channels, thread, owner, folder = owner_setup(tmp_path, monkeypatch, extract)
    channels.listening.hear(thread, '配对期间的主人原话', 'owner', {'senderId': 'owner', 'senderIsOwner': True})
    if revoked:
        channels.unbind_identity(str(channels.list_identities()[0]['identityId']))
    channels.listening.hear(thread, '群友自己的事实', 'visitor', {'senderId': 'visitor', 'senderIsOwner': False})

    async def summarize(_thread, messages, _context, profiles):
        assert len(messages) == 2
        profiles_seen.extend(profiles)
        return {'groupNote': '合法群资料可继续整理', 'members': []}

    groups = MultichannelGroupRepository(channels)
    worker = ChannelMemoryMaintenance(lambda: channels, lambda: groups, summarize, lambda *_: None,
        owner_statements=main._ingest_owner_listening_memory)
    worker.owner_documents = owner
    worker.clock = lambda: datetime.now().timestamp() + worker.IDLE_SECONDS + 1

    async def run():
        assert await worker.consolidate(thread.thread_id, listening=True)
        await owner.drain()
        assert bool(extracted) is not revoked and profiles_seen == ['visitor']
        assert cursor(channels, thread) == 2
        assert groups.read_context(thread.role_id, thread.thread_id)['groupNote'] == '合法群资料可继续整理'
        assert ('配对期间的主人原话' in (folder / 'PENDING.md').read_text(encoding='utf-8')) is not revoked

    asyncio.run(run())
