"""Terminal plans retain ownership of task cleanup across handover failures."""
import asyncio
import sqlite3
import sys

import pytest

from test_schedules import system as system
from backend.app.task_runtime.process import ProcessExecutor


@pytest.mark.parametrize('terminal', ['expired', 'revoked', 'cancel_status_busy', 'cancel_link_busy',
                                    'submit_permission', 'query_missing', 'source_exception'])
def test_terminal_handover_loss_releases_retained_quota_after_restart(system, monkeypatch, tmp_path, terminal):
    factory, sessions, now, allowed, origin = system
    app = factory()
    app.tasks.retention_seconds = -1
    app.tasks.max_retained_per_role = 1
    executions = []
    class Executor(ProcessExecutor):
        def is_authorized(self):
            return True
    async def execute(plan):
        executions.append(plan['id'])
        return Executor([sys.executable, '-c', 'print("owned-cleanup-proof")'], tmp_path, {})
    monkeypatch.setattr(app, 'execute', execute)
    plan = app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'bounded fixture'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    submit = app.tasks.submit
    def lost_receipt(*args, **kwargs):
        task = submit(*args, **kwargs)
        assert app.tasks.query(task, origin, wait_seconds=3).ended_at is not None
        error = PermissionError if terminal == 'submit_permission' else OSError
        raise error('task exists but its handover receipt was lost')
    monkeypatch.setattr(app.tasks, 'submit', lost_receipt)
    now[0] += 1
    asyncio.run(app.tick())
    assert app.list(origin)[0]['latestOccurrence']['taskId'] is None
    monkeypatch.setattr(app.tasks, 'submit', submit)
    if terminal == 'expired':
        now[0] += 301
    elif terminal == 'revoked':
        allowed[0] = False
    elif terminal.startswith('cancel_'):
        method = '_update_task_status' if terminal == 'cancel_status_busy' else '_update'
        update = getattr(app, method)
        def busy_status(*args, **kwargs):
            with sqlite3.connect(sessions.database_path) as blocker:
                blocker.execute('BEGIN IMMEDIATE')
                return update(*args, **kwargs)
        monkeypatch.setattr(app, method, busy_status)
        with pytest.raises(sqlite3.OperationalError):
            app.cancel(origin, identifier=plan['id'])
        monkeypatch.setattr(app, method, update)
    elif terminal in {'source_exception', 'query_missing'}:
        def unavailable(*args):
            raise PermissionError('source removed') if terminal == 'source_exception' else KeyError('query receipt lost')
        with monkeypatch.context() as fault:
            if terminal == 'source_exception':
                fault.setattr(app, 'authorized', unavailable)
            else:
                fault.setattr(app.tasks, 'query', unavailable)
            asyncio.run(app.tick())
    if terminal in {'expired', 'revoked'}:
        asyncio.run(app.tick())
    reopened = factory()
    asyncio.run(reopened.tick())
    asyncio.run(reopened.tick())
    assert app.tasks.list(origin, kind='schedule') == ()
    assert sessions.list_messages(origin.session_key) == []
    assert len(executions) == 1
    expected = 'cancelled' if terminal.startswith('cancel_') else 'expired' if terminal == 'expired' else 'revoked'
    assert reopened.list(origin)[0]['latestOccurrence']['deliveryStatus'] == expected


@pytest.mark.parametrize('terminal', ['expired', 'cancelled'])
def test_terminal_gate_rejects_a_submission_that_has_not_entered_runtime(system, monkeypatch, tmp_path, terminal):
    from threading import Event
    factory, sessions, now, _, origin = system
    app = factory()
    output = tmp_path / 'must-not-run.txt'
    class Executor(ProcessExecutor):
        def is_authorized(self):
            return True
    async def execute(plan):
        return Executor([sys.executable, '-c', 'import pathlib; pathlib.Path("must-not-run.txt").touch()'], tmp_path, {})
    monkeypatch.setattr(app, 'execute', execute)
    plan = app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'bounded fixture'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    submit, entered, release = app.tasks.submit, Event(), Event()
    def delayed_submit(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return submit(*args, **kwargs)
    monkeypatch.setattr(app.tasks, 'submit', delayed_submit)
    now[0] += 1
    async def run():
        pending = asyncio.create_task(app.tick())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            if terminal == 'cancelled':
                await asyncio.to_thread(app.cancel, origin, identifier=plan['id'])
            else:
                now[0] += 301
                await factory().tick()
        finally:
            release.set()
            await pending
        await factory().tick()
    asyncio.run(run())
    assert not output.exists()
    assert app.tasks.list(origin, kind='schedule') == ()
    assert sessions.list_messages(origin.session_key) == []
    assert app.list(origin)[0]['latestOccurrence']['deliveryStatus'] == terminal
    assert app.list(origin)[0]['planStatus'] == ('completed' if terminal == 'expired' else 'cancelled')


@pytest.mark.parametrize('lost_receipt', [False, True])
def test_cancel_owns_an_accepted_submission_before_its_receipt_returns(system, monkeypatch, tmp_path, lost_receipt):
    from threading import Event
    factory, sessions, now, _, origin = system
    app = factory()
    app.tasks.retention_seconds = -1
    executions = []
    class Executor(ProcessExecutor):
        def is_authorized(self):
            return True
    async def execute(plan):
        executions.append(plan['id'])
        return Executor([sys.executable, '-c', 'print("accepted-before-cancel")'], tmp_path, {})
    monkeypatch.setattr(app, 'execute', execute)
    plan = app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'bounded fixture'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    submit, entered, release = app.tasks.submit, Event(), Event()
    def delayed_receipt(*args, **kwargs):
        task_id = submit(*args, **kwargs)
        assert app.tasks.query(task_id, origin, wait_seconds=3).ended_at is not None
        entered.set()
        assert release.wait(3)
        if lost_receipt:
            raise OSError('accepted receipt lost after cancel')
        return task_id
    monkeypatch.setattr(app.tasks, 'submit', delayed_receipt)
    now[0] += 1
    async def run():
        pending = asyncio.create_task(app.tick())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.to_thread(app.cancel, origin, identifier=plan['id'])
        finally:
            release.set()
            await pending
        await factory().tick()
        await factory().tick()
    asyncio.run(run())
    assert app.tasks.list(origin, kind='schedule') == ()
    assert sessions.list_messages(origin.session_key) == []
    assert len(executions) == 1
    assert app.list(origin)[0]['latestOccurrence']['deliveryStatus'] == 'cancelled'


@pytest.mark.parametrize('legacy_execution', ['claimed', 'failed'])
def test_legacy_sealed_handover_with_no_id_is_recovered_and_ack_failure_prevents_pruning(system, monkeypatch, tmp_path, legacy_execution):
    factory, sessions, now, _, origin = system
    app = factory()
    app.tasks.retention_seconds = -1
    class Executor(ProcessExecutor):
        def is_authorized(self):
            return True
    async def execute(plan):
        return Executor([sys.executable, '-c', 'print("legacy-sealed-proof")'], tmp_path, {})
    monkeypatch.setattr(app, 'execute', execute)
    plan = app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'bounded fixture'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    submit = app.tasks.submit
    def lost_receipt(*args, **kwargs):
        task_id = submit(*args, **kwargs)
        app.tasks.query(task_id, origin, wait_seconds=3)
        raise OSError('handover receipt lost')
    monkeypatch.setattr(app.tasks, 'submit', lost_receipt)
    now[0] += 1
    asyncio.run(app.tick())
    # Reproduce the already sealed state persisted by the previous local
    # implementation, including its missing cleanup flag.
    with sqlite3.connect(sessions.database_path) as db:
        db.execute("UPDATE schedule_occurrences SET execution=?,delivery='revoked',synced=1,result_ack_pending=0", (legacy_execution,))
    acknowledge = app.tasks.acknowledge_result
    def unavailable_ack(*args):
        raise sqlite3.OperationalError('ACK store temporarily busy')
    monkeypatch.setattr(app.tasks, 'acknowledge_result', unavailable_ack)
    now[0] += 31 * 86400
    reopened = factory()
    asyncio.run(reopened.tick())
    asyncio.run(reopened.tick())
    assert reopened.list(origin)[0]['id'] == plan['id'], 'unsettled ownership must survive history pruning'
    assert len(app.tasks.list(origin, kind='schedule')) == 1
    monkeypatch.setattr(app.tasks, 'acknowledge_result', acknowledge)
    asyncio.run(factory().tick())
    assert app.tasks.list(origin, kind='schedule') == ()
    assert sessions.list_messages(origin.session_key) == []
    asyncio.run(reopened.tick())
    assert reopened.list(origin) == [], 'settled terminal history can be reclaimed normally'


def test_temporary_role_disable_keeps_unknown_task_cleanup_until_delete_rolls_back(system, monkeypatch, tmp_path):
    factory, sessions, now, allowed, origin = system
    app = factory()
    app.tasks.retention_seconds = -1
    class Executor(ProcessExecutor):
        def is_authorized(self):
            return True
    async def execute(plan):
        return Executor([sys.executable, '-c', 'print("delete-rollback-proof")'], tmp_path, {})
    monkeypatch.setattr(app, 'execute', execute)
    app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'bounded fixture'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    submit = app.tasks.submit
    def lost_receipt(*args, **kwargs):
        task_id = submit(*args, **kwargs)
        app.tasks.query(task_id, origin, wait_seconds=3)
        raise OSError('handover receipt lost before role deletion')
    monkeypatch.setattr(app.tasks, 'submit', lost_receipt)
    now[0] += 1
    asyncio.run(app.tick())
    task = app.tasks.list(origin, kind='schedule')[0]
    # Real delete_role preflight disables management while awaiting owned
    # resources; a later deletion failure restores the same role.
    app.tasks.set_role_enabled(origin.role_id, False)
    allowed[0] = False
    with pytest.raises(PermissionError):
        app.tasks.list(origin, kind='schedule')
    with pytest.raises(KeyError):
        app.tasks.acknowledge_result(task.task_id, origin)
    asyncio.run(app.tick())
    app.tasks.set_role_enabled(origin.role_id, True)
    allowed[0] = True
    reopened = factory()
    asyncio.run(reopened.tick())
    asyncio.run(reopened.tick())
    assert app.tasks.list(origin, kind='schedule') == ()
    assert sessions.list_messages(origin.session_key) == []
    assert reopened.list(origin)[0]['latestOccurrence']['deliveryStatus'] == 'revoked'


def test_worker_retries_permission_error_persistence_busy_then_acknowledges_and_delivers_other_plan(system, monkeypatch, tmp_path):
    from threading import Event
    from backend.app.task_runtime import TaskOrigin
    factory, sessions, now, allowed, origin = system
    app = factory()
    app.tasks.retention_seconds = -1
    executions = []
    class Executor(ProcessExecutor):
        def is_authorized(self):
            return True
    async def execute(plan):
        executions.append(plan['id'])
        return Executor([sys.executable, '-c', 'print("secondary-handler-recovery")'], tmp_path, {})
    monkeypatch.setattr(app, 'execute', execute)
    app.create(origin, {'tier': 'soft', 'trigger': 'after', 'when': '1s', 'prompt': 'bounded fixture'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    sessions.open_role_session('other')
    other = TaskOrigin('other', 'role:other', 'other-parent', 'other-call')
    app.create(other, {'tier': 'instant', 'trigger': 'after', 'when': '2s', 'message': 'other-plan-proof'},
        received_at=now[0], received_source='hostReceivedAt', frozen={})
    submit = app.tasks.submit
    def lost_receipt(*args, **kwargs):
        task_id = submit(*args, **kwargs)
        assert app.tasks.query(task_id, origin, wait_seconds=3).ended_at is not None
        raise OSError('task handover receipt lost')
    monkeypatch.setattr(app.tasks, 'submit', lost_receipt)
    now[0] += 1
    asyncio.run(app.tick())
    assert len(app.tasks.list(origin, kind='schedule')) == 1
    app.authorized = lambda plan: plan['role_id'] != origin.role_id or allowed[0]
    allowed[0] = False
    app.tasks.set_role_enabled(origin.role_id, False)
    entered, blockers = Event(), []
    original_list = app.tasks.list
    def concurrent_writer(*args, **kwargs):
        if not entered.is_set():
            blocker = sqlite3.connect(sessions.database_path, check_same_thread=False)
            blocker.execute('BEGIN IMMEDIATE')
            blockers.append(blocker)
            entered.set()
        return original_list(*args, **kwargs)
    monkeypatch.setattr(app.tasks, 'list', concurrent_writer)
    async def run():
        app.start()
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # Keep the real writer lock beyond the schedule connection's busy
            # timeout, including the PermissionError handler's second write.
            await asyncio.sleep(.15)
            for blocker in blockers:
                blocker.rollback()
                blocker.close()
            blockers.clear()
            app.tasks.set_role_enabled(origin.role_id, True)
            allowed[0] = True
            now[0] += 1
            deadline = asyncio.get_running_loop().time() + 2
            while not sessions.list_messages(other.session_key) or original_list(origin, kind='schedule'):
                assert asyncio.get_running_loop().time() < deadline, 'shared worker must resume both delivery and retained-result cleanup'
                await asyncio.sleep(.02)
            assert [message.content for message in sessions.list_messages(other.session_key)] == ['other-plan-proof']
            assert sessions.list_messages(origin.session_key) == []
            assert len(executions) == 1
        finally:
            for blocker in blockers:
                blocker.rollback()
                blocker.close()
            await app.close()
    asyncio.run(run())
