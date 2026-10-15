"""Owned schedule behavior across real SQLite instances and delivery receipts."""
import asyncio
from dataclasses import asdict
from threading import Thread

import pytest

from backend.app.schedules.application import ScheduleApplication
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskOrigin, TaskRuntime


@pytest.fixture
def system(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('role')
    tasks = TaskRuntime(tmp_path / 'tasks')
    now, allowed = [1780000000.0], [True]
    origin = TaskOrigin('role', 'role:role', 'parent', 'call', source_thread_id='desktop:role')
    async def execute(plan):
        raise AssertionError('instant cannot execute a model')
    async def deliver(plan, occurrence, authorized):
        raise AssertionError('desktop cannot send externally')
    def create_app(**kwargs):
        return ScheduleApplication(sessions, tasks, clock=lambda: now[0], authorized=lambda _: allowed[0],
            target_resolver=lambda o, args: {'channel': 'desktop', 'sessionKey': o.session_key},
            execute=execute, deliver=deliver, receipt=lambda p, o: None, **kwargs)
    yield create_app, sessions, now, allowed, origin
    tasks.close()


def create(app, origin, now, **arguments):
    return app.create(origin, {'tier': 'instant', 'trigger': 'after', 'when': '10s', 'message': 'literal-proof', **arguments},
        received_at=now[0], received_source='hostReceivedAt', frozen={})


def test_two_workers_and_repeated_ticks_deliver_once_and_count_once(system):
    factory, sessions, now, _, origin = system
    first, second = factory(), factory()
    create(first, origin, now)
    now[0] += 10
    threads = [Thread(target=lambda app=app: asyncio.run(app.tick())) for app in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    asyncio.run(second.tick())
    assert len(sessions.list_messages(origin.session_key)) == 1
    assert first.list(origin)[0]['deliveredRuns'] == 1
    assert first.list(origin)[0]['latestOccurrence']['deliveryStatus'] == 'sent'


def test_cancelled_plan_survives_restart_and_never_delivers(system):
    factory, sessions, now, _, origin = system
    app = factory()
    plan = create(app, origin, now)
    assert app.cancel(origin, identifier=plan['id'][:8])['cancelled'] == 1
    assert app.cancel(origin, identifier=plan['id'])['cancelled'] == 1
    now[0] += 100
    reopened = factory()
    asyncio.run(reopened.tick())
    assert sessions.list_messages(origin.session_key) == []
    assert reopened.list(origin)[0]['planStatus'] == 'cancelled'
    for foreign in [TaskOrigin('other', 'role:other'), TaskOrigin('role', 'thread:other')]:
        assert reopened.list(foreign) == []
        with pytest.raises(ValueError, match='unavailable'):
            reopened.cancel(foreign, identifier=plan['id'])


def test_replayed_host_call_returns_original_plan_after_nominal_grace(system):
    factory, _, now, _, origin = system
    app = factory()
    received = now[0]
    first = create(app, origin, now)
    now[0] += 3600
    replay = app.create(origin, {'tier': 'instant', 'trigger': 'after', 'when': '10s', 'message': 'literal-proof'},
        received_at=received, received_source='hostReceivedAt', frozen={})
    assert replay['id'] == first['id']
    assert len(app.list(origin)) == 1


def test_name_cancellation_is_scoped_and_prefix_ambiguity_is_rejected(system):
    import sqlite3
    factory, sessions, now, _, origin = system
    app = factory()
    first = create(app, origin, now, name='daily')
    second = create(app, TaskOrigin(**{**asdict(origin), 'tool_call_id': 'second'}), now, name='daily')
    with sqlite3.connect(sessions.database_path) as db:
        db.execute('UPDATE schedules SET id=? WHERE id=?', ('12345678' + 'a' * 24, first['id']))
        db.execute('UPDATE schedules SET id=? WHERE id=?', ('12345678' + 'b' * 24, second['id']))
    with pytest.raises(ValueError, match='unambiguous'):
        app.cancel(origin, identifier='1234')
    with pytest.raises(ValueError, match='unavailable'):
        app.cancel(origin, identifier='12345678')
    with pytest.raises(ValueError, match='unavailable'):
        app.cancel(origin, identifier='12345678' + 'a' * 24, name='daily')
    assert app.cancel(origin, name='daily')['cancelled'] == 2


def test_restart_skips_repeat_slots_and_consumes_count_without_burst(system):
    factory, sessions, now, _, origin = system
    app = factory()
    create(app, origin, now, trigger='every', when='10s', count=4)
    now[0] += 35
    app = factory()
    asyncio.run(app.tick())
    plan = app.list(origin)[0]
    assert plan['remainingRuns'] == 1 and plan['skippedRuns'] == 3
    assert plan['nextAt'] == now[0] + 5
    assert sessions.list_messages(origin.session_key) == []
    now[0] += 5
    asyncio.run(app.tick())
    assert len(sessions.list_messages(origin.session_key)) == 1
    assert app.list(origin)[0]['remainingRuns'] == 0


def test_normal_tick_uses_latest_slot_and_fixed_count_and_clock_rollback(system):
    factory, sessions, now, _, origin = system
    app = factory()
    create(app, origin, now, trigger='every', when='10s', count=3)
    now[0] += 25
    asyncio.run(app.tick())
    assert len(sessions.list_messages(origin.session_key)) == 1
    assert app.list(origin)[0]['remainingRuns'] == 1
    assert app.list(origin)[0]['skippedRuns'] == 1
    now[0] -= 20
    asyncio.run(app.tick())
    assert len(sessions.list_messages(origin.session_key)) == 1
    now[0] += 25
    asyncio.run(app.tick())
    assert len(sessions.list_messages(origin.session_key)) == 2
    assert app.list(origin)[0]['remainingRuns'] == 0


def test_delayed_model_registration_uses_received_time_and_expiration(system):
    factory, _, now, _, origin = system
    app = factory()
    received = now[0]
    now[0] += 30
    plan = app.create(origin, {'tier': 'instant', 'trigger': 'after', 'when': '10s', 'message': 'received'},
        received_at=received, received_source='hostReceivedAt', frozen={})
    assert plan['nextAt'] == received + 10
    now[0] += 301
    asyncio.run(app.tick())
    assert app.list(origin)[0]['latestOccurrence']['deliveryStatus'] == 'expired'
    with pytest.raises(ValueError, match='expired'):
        app.create(TaskOrigin(**{**asdict(origin), 'tool_call_id': 'late'}),
            {'tier': 'instant', 'trigger': 'after', 'when': '10s', 'message': 'expired'},
            received_at=received, received_source='hostReceivedAt', frozen={})


def test_cancel_before_claim_never_enters_the_final_desktop_append(system, monkeypatch):
    from threading import Event
    factory, sessions, now, _, origin = system
    app = factory()
    plan = create(app, origin, now)
    now[0] += 10
    entered, release = Event(), Event()
    original = sessions.append_schedule_message_once
    def before_gate(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(sessions, 'append_schedule_message_once', before_gate)
    # Cancellation wins the database gate before the worker starts its append.
    app.cancel(origin, identifier=plan['id'])
    asyncio.run(app.tick())
    assert not entered.is_set()
    assert sessions.list_messages(origin.session_key) == []


def test_offline_before_dispatch_retries_within_original_grace_and_then_expires(system):
    factory, sessions, now, _, origin = system
    offline = [True]
    app = factory(offline=lambda _: offline[0])
    create(app, origin, now)
    now[0] += 10
    asyncio.run(app.tick())
    assert app.list(origin)[0]['latestOccurrence']['deliveryStatus'] == 'waiting'
    now[0] += 301
    offline[0] = False
    asyncio.run(app.tick())
    assert app.list(origin)[0]['latestOccurrence']['deliveryStatus'] == 'expired'
    assert sessions.list_messages(origin.session_key) == []


def test_final_desktop_commit_wins_before_cancel_and_is_preserved(system, monkeypatch):
    from threading import Event
    factory, sessions, now, _, origin = system
    app = factory()
    plan = create(app, origin, now)
    now[0] += 10
    entered, release, cancelled = Event(), Event(), Event()
    original = sessions.append_schedule_message_once
    def append(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(sessions, 'append_schedule_message_once', append)
    worker = Thread(target=lambda: asyncio.run(app.tick()))
    worker.start()
    assert entered.wait(3)
    def cancel():
        try:
            app.cancel(origin, identifier=plan['id'])
            cancelled.set()
        except Exception:
            # The public tool can retry a short SQLite busy wait.
            pass
    pending = Thread(target=cancel)
    pending.start()
    release.set()
    worker.join(3)
    pending.join(3)
    if not cancelled.is_set():
        app.cancel(origin, identifier=plan['id'])
    assert len(sessions.list_messages(origin.session_key)) == 1
    assert app.list(origin)[0]['deliveredRuns'] == 1
    assert app.list(origin)[0]['planStatus'] == 'cancelled'


def test_soft_handover_crash_finds_retained_real_execution_without_running_again(system, monkeypatch, tmp_path):
    import sys
    from backend.app.task_runtime.process import ProcessExecutor
    factory, sessions, now, _, origin = system
    app = factory()
    class Executor(ProcessExecutor):
        def is_authorized(self):
            return True
    constructors = []
    async def execute(plan):
        constructors.append(plan['id'])
        return Executor([sys.executable, '-c', 'print("handover-proof-158")'], tmp_path, {})
    monkeypatch.setattr(app, 'execute', execute)
    app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'real fixture'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    submit, interrupted = app.tasks.submit, [False]
    def submit_then_lose_receipt(*args, **kwargs):
        task = submit(*args, **kwargs)
        app.tasks.query(task, origin, wait_seconds=3)
        if not interrupted[0]:
            interrupted[0] = True
            raise OSError('process lost schedule taskId acknowledgement')
        return task
    monkeypatch.setattr(app.tasks, 'submit', submit_then_lose_receipt)
    now[0] += 1
    asyncio.run(app.tick())
    assert sessions.list_messages(origin.session_key) == []
    reopened = factory()
    async def forbidden(plan):
        raise AssertionError('the original execution must be recovered, not reconstructed')
    monkeypatch.setattr(reopened, 'execute', forbidden)
    asyncio.run(reopened.tick())
    assert len(constructors) == 1
    assert len(reopened.tasks.list(origin, kind='schedule')) == 1
    assert len(sessions.list_messages(origin.session_key)) == 1
    assert 'handover-proof-158' in sessions.list_messages(origin.session_key)[0].content
    assert reopened.list(origin)[0]['deliveredRuns'] == 1


def test_cancel_during_soft_setup_prevents_task_submission(system, monkeypatch):
    factory, sessions, now, _, origin = system
    app = factory()
    plan = app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'cancel before launch'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class NeverExecutor:
            def is_authorized(self):
                return True
            def run(self, execution):
                raise AssertionError('cancelled setup cannot execute')
            def cancel(self):
                pass
        async def execute(plan):
            entered.set()
            await release.wait()
            return NeverExecutor()
        monkeypatch.setattr(app, 'execute', execute)
        now[0] += 1
        pending = asyncio.create_task(app.tick())
        await entered.wait()
        app.cancel(origin, identifier=plan['id'])
        release.set()
        await pending
    asyncio.run(run())
    assert app.tasks.list(origin, kind='schedule') == ()
    assert sessions.list_messages(origin.session_key) == []
    assert app.list(origin)[0]['latestOccurrence']['deliveryStatus'] == 'cancelled'


def test_restart_interrupts_real_soft_process_and_does_not_reexecute(system, monkeypatch, tmp_path):
    import sys
    from backend.app.task_runtime.process import ProcessExecutor
    factory, sessions, now, _, origin = system
    app = factory()
    class Executor(ProcessExecutor):
        def is_authorized(self):
            return True
    async def execute(plan):
        return Executor([sys.executable, '-u', '-c', 'import time; print("running-158"); time.sleep(20)'], tmp_path, {})
    monkeypatch.setattr(app, 'execute', execute)
    app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'controlled process'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    now[0] += 1
    asyncio.run(app.tick())
    app.tasks.close()
    restarted = TaskRuntime(app.tasks.root)
    try:
        reopened = ScheduleApplication(sessions, restarted, clock=app.clock, authorized=app.authorized,
            target_resolver=app.target_resolver, execute=app.execute, deliver=app.deliver, receipt=app.receipt)
        asyncio.run(reopened.tick())
        assert reopened.list(origin)[0]['latestOccurrence']['executionStatus'] == 'interrupted'
        assert reopened.list(origin)[0]['latestOccurrence']['deliveryStatus'] == 'interrupted'
        assert len(restarted.list(origin, kind='schedule')) == 1
        assert sessions.list_messages(origin.session_key) == []
    finally:
        restarted.close()


def test_shutdown_waits_for_cross_thread_handover_before_runtime_joins_process(system, monkeypatch, tmp_path):
    import sys
    from threading import Event
    from backend.app.task_runtime.process import ProcessExecutor
    factory, _, now, _, origin = system
    app = factory()
    class Executor(ProcessExecutor):
        def is_authorized(self):
            return True
    async def execute(plan):
        return Executor([sys.executable, '-c', 'import time; time.sleep(20)'], tmp_path, {})
    monkeypatch.setattr(app, 'execute', execute)
    app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'owned shutdown'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    submit, entered, release, finished = app.tasks.submit, Event(), Event(), Event()
    def delayed_submit(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        try:
            return submit(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(app.tasks, 'submit', delayed_submit)
    now[0] += 1
    async def run():
        app.start()
        assert await asyncio.to_thread(entered.wait, 3)
        first = asyncio.create_task(app.close())
        await asyncio.sleep(.01)
        second = asyncio.create_task(app.close())
        await asyncio.sleep(.01)
        assert not first.done() and not second.done() and not finished.is_set()
        release.set()
        await asyncio.gather(first, second)
        assert finished.is_set()
        await asyncio.to_thread(app.tasks.close)
    asyncio.run(run())
    assert app.tasks.list(origin, kind='schedule')[0].status == 'interrupted'


def test_thirty_day_cleanup_preserves_unconfirmed_occurrences(system):
    factory, _, now, _, origin = system
    app = factory(offline=lambda _: True)
    create(app, origin, now)
    now[0] += 10
    asyncio.run(app.tick())
    # A waiting occurrence is kept, even when its age exceeds retention;
    # the worker records its real expiry before it becomes reclaimable.
    now[0] += 31 * 86400
    asyncio.run(app.tick())
    assert app.list(origin)[0]['latestOccurrence']['deliveryStatus'] == 'expired'
    asyncio.run(app.tick())
    assert app.list(origin) == []


def test_archived_history_is_bounded_to_one_thousand_per_role(system):
    import sqlite3
    factory, sessions, now, _, origin = system
    app = factory()
    first = create(app, origin, now)
    sessions.open_role_session('other')
    other = TaskOrigin('other', 'role:other', 'other-run', 'other-call', source_thread_id='desktop:other')
    second = create(app, other, now)
    # A persisted fixture represents archived receipts from previous weeks;
    # the public tick is the retention entry point being exercised.
    with sqlite3.connect(sessions.database_path) as db:
        db.executemany('INSERT INTO schedule_occurrences(id,schedule_id,nominal,execution,delivery,synced) VALUES(?,?,?,?,?,1)',
            [(f'archived-{i}', first['id'], now[0] - i, 'ready', 'sent') for i in range(1005)]
            + [(f'other-{i}', second['id'], now[0] - i, 'ready', 'sent') for i in range(3)])
    asyncio.run(app.tick())
    with sqlite3.connect(sessions.database_path) as db:
        assert db.execute('SELECT COUNT(*) FROM schedule_occurrences WHERE schedule_id=?', (first['id'],)).fetchone()[0] == 1000
        assert db.execute('SELECT COUNT(*) FROM schedule_occurrences WHERE schedule_id=?', (second['id'],)).fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM schedule_occurrences WHERE id='archived-1004'").fetchone()[0] == 0


@pytest.mark.parametrize('status', ['sent', 'pending', 'failed', 'uncertain'])
@pytest.mark.parametrize('local_failure', [False, True])
def test_real_channel_host_receipts_do_not_resend_after_restart(tmp_path, monkeypatch, status, local_failure):
    from backend.app.channels import ChannelAdapter, ChannelHost, DeliveryResult, DeliveryStatus, TargetKind
    from backend.app.channels.models import target_from_chat
    from backend.app.multichannel import MultichannelStore
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    account = channels.register_channel_account('role', 'qq', account_id='fixture-only', enabled=True)
    target = target_from_chat(account_id=account.account_id, chat_id='local-target', kind=TargetKind.PRIVATE)
    thread = channels.ensure_channel_thread('role', 'qq', account.account_id, target.chat_id, 'private')
    class Adapter(ChannelAdapter):
        channel = 'qq'
        account_id = account.account_id
        sent = []
        def parse_event(self, payload):
            return None
        async def list_targets(self):
            return [target]
        async def send(self, message):
            self.sent.append(message)
            await asyncio.sleep(.01)
            return DeliveryResult(DeliveryStatus(status), message.target, 'platform-local-158' if status == 'sent' else None)
    adapter = Adapter()
    host = ChannelHost(channels)
    host.register(adapter)
    asyncio.run(host.account_targets('role', account.account_id))
    tasks = TaskRuntime(tmp_path / 'tasks')
    now = [1780000000.0]
    origin = TaskOrigin('role', 'role:role', 'parent', 'call', source_thread_id='desktop:role')
    async def deliver(plan, occurrence, authorized):
        receipt = await host.account_send('role', account.account_id, target.id, occurrence['content'], actor='owner',
            source_tool_call_id=occurrence['id'], source_kind='schedule', record_message=False, authorize_source=authorized)
        return receipt.status.value, receipt.attempt_id
    async def execute(plan):
        raise AssertionError('instant cannot execute')
    def factory():
        return ScheduleApplication(sessions, tasks, clock=lambda: now[0], authorized=lambda p: True,
            target_resolver=lambda o, a: {'channel': 'qq', 'sessionKey': thread.session_key}, execute=execute,
            deliver=deliver, receipt=lambda p, o: channels.delivery_for_source('role', source_kind='schedule', source_tool_call_id=o['id']))
    try:
        app = factory()
        peer = factory()
        create(app, origin, now)
        original, failed, fail_ack = sessions.append_schedule_message_once, [False], [local_failure]
        def append(*args, **kwargs):
            if fail_ack[0] and kwargs.get('attempt_id'):
                failed[0] = True
                raise OSError('controlled local acknowledgement failure')
            return original(*args, **kwargs)
        monkeypatch.setattr(sessions, 'append_schedule_message_once', append)
        now[0] += 10
        async def concurrent_tick():
            await asyncio.gather(app.tick(), peer.tick())
        asyncio.run(concurrent_tick())
        if local_failure:
            assert failed[0]
            assert sessions.runtime_messages(thread.session_key) == []
            assert app.list(origin)[0]['latestOccurrence']['deliveryStatus'] == status
        fail_ack[0] = False
        reopened = factory()
        asyncio.run(reopened.tick())
        asyncio.run(reopened.tick())
        assert len(adapter.sent) == 1
        assert reopened.list(origin)[0]['latestOccurrence']['deliveryStatus'] == status
        assert reopened.list(origin)[0]['deliveredRuns'] == int(status == 'sent')
        assert len(channels.list_deliveries('role')) == 1
        messages = sessions.list_messages(thread.session_key)
        assert len(messages) == 1 and messages[0].metadata['deliveryStatus'] == status
        assert [m.content for m in sessions.runtime_messages(thread.session_key)] == (['literal-proof'] if status == 'sent' else [])
        if status in {'pending', 'uncertain'}:
            now[0] += 31 * 86400
            asyncio.run(reopened.tick())
            assert reopened.list(origin)[0]['latestOccurrence']['deliveryStatus'] == status
            assert len(adapter.sent) == 1
        if status == 'pending':
            attempt = channels.list_deliveries('role')[0]['attemptId']
            channels.finish_delivery(attempt, status='sent', platform_message_id='late-platform-receipt')
            asyncio.run(reopened.tick())
            assert len(adapter.sent) == 1
            assert reopened.list(origin)[0]['deliveredRuns'] == 1
            assert sessions.runtime_messages(thread.session_key)[0].content == 'literal-proof'
    finally:
        tasks.close()
