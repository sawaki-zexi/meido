"""Owned background lifetimes and local recovery through real Runtime/HTTP/SQLite."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import sqlite3
from uuid import uuid4

import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


async def _eventually(read, ready, *, timeout=4):
    async with asyncio.timeout(timeout):
        while True:
            value = read()
            if ready(value):
                return value
            await asyncio.sleep(.01)


async def _finished_task(service, result, origin):
    task = await asyncio.to_thread(service.tasks.query, result['taskId'], origin, wait_seconds=3)
    assert task.ended_at is not None, task
    return task


def _request(service, **parameters):
    return {'requestId': uuid4().hex, 'expectedRevision': service.application.settings.public()['revision'],
        'parameters': {**service.request, 'background': True, **parameters}}


def _ui_origin(service, role_id=None):
    from backend.app.task_runtime import TaskOrigin
    role_id = role_id or service.role.id
    return TaskOrigin(role_id, f'role:{role_id}', source_thread_id=f'desktop:{role_id}')


@asynccontextmanager
async def _background_client(service, *, start_maintenance=True):
    import httpx
    from fastapi import FastAPI
    api = FastAPI()
    api.include_router(service.application.router())
    if start_maintenance:
        service.application.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://testserver') as client:
            yield client
    finally:
        service.peer.release.set()
        await service.application.close()


def test_accepted_background_survives_its_real_parent_signal_and_run_completion(novelai_service):
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun
    from backend.app.novelai_generation.context import capture_tool_context
    service = novelai_service
    app = service.application
    key = f'role:{service.role.id}'
    run = AgentRun(runId='background-parent', roleId=service.role.id, sessionKey=key,
        status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={'humanInputSource': 'desktop_owner'})
    service.sessions.create_run_with_messages(run, '请后台画一张图')
    parent = CancellationToken()
    context = ToolContext(service.role.id, key, run.runId, CancellationToken(parent=parent), 'background-call')
    source = capture_tool_context(service.sessions, None, context, role_id=service.role.id, is_owner=True)
    service.peer.block = True

    async def scenario():
        app.start()
        try:
            result = await app.tool_for(service.role.id, is_owner=True).execute({**service.request, 'background': True}, context)
            assert not result.is_error and result.details['status'] == 'accepted'
            assert await asyncio.to_thread(service.peer.accepted.wait, 3)
            service.sessions.update_run(run.runId, status='completed', ended_at=datetime.now(timezone.utc))
            parent.cancel()
            assert context.signal.is_cancelled()
            await app.recover()
            assert service.tasks.query(result.details['taskId'], source.origin).ended_at is None
            service.peer.release.set()
            assert (await _finished_task(service, result.details, source.origin)).status == 'succeeded'
            completed = await _eventually(lambda: app.owner_generation(service.role.id, result.details['generationId'],
                attempt_id=result.details['attemptId']), lambda row: row['deliveryStatus'] == 'sent')
            rows = service.sessions.list_messages(key)
            assert len([row for row in rows if row.messageType == 'internal_completion']) == 1
            assert len([row for row in rows if row.role == 'user']) == 1
            assert rows[-1].runId is None and rows[-1].metadata['attachments'] == completed['attachments']
            assert service.sessions.get_run(run.runId).status == 'completed'
            assert len(service.peer.requests) == 1
        finally:
            service.peer.release.set()
            await app.close()
    asyncio.run(scenario())


def test_exact_background_stop_joins_runtime_and_seals_late_provider_delivery(novelai_service):
    service = novelai_service
    service.peer.block = True
    path = f'/api/roles/{service.role.id}/generations'

    async def scenario():
        async with _background_client(service) as client:
            response = await client.post(path, json=_request(service))
            assert response.status_code == 202, response.text
            accepted = response.json()['generation']
            assert await asyncio.to_thread(service.peer.accepted.wait, 3)
            response = await client.post(path + '/' + accepted['generationId'] + '/cancel',
                json={'attemptId': accepted['attemptId']})
            assert response.status_code == 200, response.text
            stopped = response.json()['generation']
            task = service.tasks.query(accepted['taskId'], _ui_origin(service))
            assert task.ended_at is not None and task.status == 'cancelled'
            assert stopped['attemptId'] == accepted['attemptId']
            assert stopped['status'] == 'unknown' and stopped['consumption'] == 'possible'
            assert stopped['deliveryStatus'] == 'suppressed'
            service.peer.release.set()
            await service.application.recover()
            result = await client.get(path + '/' + accepted['generationId'], params={'attemptId': accepted['attemptId']})
            assert result.json()['generation']['deliveryStatus'] == 'suppressed'
            assert service.sessions.list_messages(f'role:{service.role.id}') == []
            assert len(service.peer.requests) == 1
    asyncio.run(scenario())


def test_role_permission_revocation_after_background_acceptance_joins_without_completion(novelai_main):
    client, service = novelai_main
    service.peer.block = True
    path = f'/api/roles/{service.role.id}/generations'
    with client:
        response = client.post(path, json=_request(service))
        assert response.status_code == 202, response.text
        accepted = response.json()['generation']
        assert service.peer.accepted.wait(3)
        changed = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
            'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'manualEnabled': False}}})
        assert changed.status_code == 200, changed.text
        task = service.tasks.query(accepted['taskId'], _ui_origin(service))
        assert task.ended_at is not None and task.status == 'cancelled'
        service.peer.release.set()
        client.portal.call(service.application.recover)
        result = client.get(path + '/' + accepted['generationId'], params={'attemptId': accepted['attemptId']}).json()['generation']
        assert result['status'] == 'unknown' and result['deliveryStatus'] == 'suppressed'
        assert result['consumption'] == 'possible'
        assert service.sessions.list_messages(f'role:{service.role.id}') == []
        assert len(service.peer.requests) == 1


def test_background_result_waits_for_real_role_lock_then_publishes_once(novelai_service):
    service = novelai_service
    app = service.application
    path = f'/api/roles/{service.role.id}/generations'
    service.peer.block = True

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        app.lock_for = {service.role.id: lock}.__getitem__
        try:
            async with _background_client(service) as client:
                response = await client.post(path, json=_request(service))
                assert response.status_code == 202, response.text
                accepted = response.json()['generation']
                assert await asyncio.to_thread(service.peer.accepted.wait, 3)
                service.peer.release.set()
                assert (await _finished_task(service, accepted, _ui_origin(service))).status == 'succeeded'
                await app.recover()
                assert service.sessions.list_messages(f'role:{service.role.id}') == []
                assert app.owner_generation(service.role.id, accepted['generationId'], attempt_id=accepted['attemptId'])['deliveryStatus'] == 'pending'
                lock.release()
                completed = await _eventually(lambda: app.owner_generation(service.role.id, accepted['generationId'],
                    attempt_id=accepted['attemptId']), lambda row: row['deliveryStatus'] == 'sent')
                for _ in range(2):
                    await app.recover()
                messages = service.sessions.list_messages(f'role:{service.role.id}')
                assert len(messages) == 1 and messages[0].messageType == 'internal_completion'
                assert messages[0].metadata['attachments'] == completed['attachments']
                assert messages[0].metadata['generationAttemptIds'] == [accepted['attemptId']]
                assert len(service.peer.requests) == 1
        finally:
            if lock.locked():
                lock.release()
    asyncio.run(scenario())


def test_busy_role_does_not_starve_a_later_roles_completed_background_image(novelai_service):
    from backend.app.models import RoleInput
    service = novelai_service
    app = service.application
    other = app.create_role(RoleInput(name='可发布角色', profile={'profile': 'second owned role'},
        agentConfig={'novelai': {'manualEnabled': True, 'consentRevision': app.settings.public()['revision']}}))

    async def scenario():
        locks = {identifier: asyncio.Lock() for identifier in (service.role.id, other.id)}
        for lock in locks.values():
            await lock.acquire()
        app.lock_for = locks.__getitem__
        try:
            async with _background_client(service) as client:
                accepted = []
                # One account permits one in-flight generation. Finish each real
                # provider POST before admitting the next role; only delivery waits.
                for identifier in locks:
                    response = await client.post(f'/api/roles/{identifier}/generations', json=_request(service))
                    assert response.status_code == 202, response.text
                    result = response.json()['generation']
                    assert (await _finished_task(service, result, _ui_origin(service, identifier))).status == 'succeeded'
                    accepted.append(result)
                assert all(service.sessions.list_messages(f'role:{identifier}') == [] for identifier in locks)
                locks[other.id].release()
                await _eventually(lambda: app.owner_generation(other.id, accepted[1]['generationId'],
                    attempt_id=accepted[1]['attemptId']), lambda row: row['deliveryStatus'] == 'sent')
                assert service.sessions.list_messages(f'role:{service.role.id}') == []
                assert app.owner_generation(service.role.id, accepted[0]['generationId'],
                    attempt_id=accepted[0]['attemptId'])['deliveryStatus'] == 'pending'
                locks[service.role.id].release()
                await _eventually(lambda: app.owner_generation(service.role.id, accepted[0]['generationId'],
                    attempt_id=accepted[0]['attemptId']), lambda row: row['deliveryStatus'] == 'sent')
                for _ in range(2):
                    await app.recover()
                for identifier, result in zip(locks, accepted):
                    messages = service.sessions.list_messages(f'role:{identifier}')
                    assert len(messages) == 1 and messages[0].metadata['generationAttemptIds'] == [result['attemptId']]
                assert len(service.peer.requests) == 2
        finally:
            for lock in locks.values():
                if lock.locked():
                    lock.release()
    asyncio.run(scenario())


def test_background_handover_copy_failure_returns_no_acceptance_and_joins_runtime(novelai_service):
    service = novelai_service
    app = service.application
    path = f'/api/roles/{service.role.id}/generations'
    request = _request(service)
    with app.ledger.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_background_handover BEFORE UPDATE OF task_id ON novelai_attempts
            WHEN NEW.task_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'controlled background handover failure'); END""")

    async def scenario():
        async with _background_client(service) as client:
            response = await client.post(path, json=request)
            assert response.status_code == 503, response.text
            tasks = service.tasks.list(_ui_origin(service), kind='novelai_image')
            assert len(tasks) == 1 and tasks[0].ended_at is not None
            assert tasks[0].status == 'cancelled'
            assert service.peer.requests == []
            with app.ledger.connect() as connection:
                connection.execute('DROP TRIGGER fail_background_handover')
            await app.recover()
            history = app.history(service.role.id)
            assert len(history['generations']) == 1
            repaired = history['generations'][0]
            assert repaired['taskId'] == tasks[0].task_id
            assert repaired['status'] == 'cancelled_before_dispatch' and repaired['deliveryStatus'] == 'suppressed'
            assert repaired['consumption'] == 'not_dispatched'
            assert history['usage']['accountAttempts'] == history['usage']['manualAttempts'] == 0
            replay = await client.post(path, json=request)
            assert replay.status_code == 403, replay.text
            assert len(app.history(service.role.id)['generations']) == 1
            assert service.peer.requests == []
            assert service.sessions.list_messages(f'role:{service.role.id}') == []
    asyncio.run(scenario())


def test_background_ack_sqlite_failure_retains_owned_result_then_recovers_without_post(novelai_service):
    from backend.app.task_runtime.runtime import TaskBudget, TaskOutcome
    service = novelai_service
    app = service.application
    origin = _ui_origin(service)
    runtime_database = service.tasks.root / 'tasks.db'
    service.tasks.max_retained_per_role = 1
    service.tasks.retention_seconds = 0
    with sqlite3.connect(runtime_database) as connection:
        connection.execute("""CREATE TRIGGER fail_background_ack BEFORE UPDATE OF retain_until_ack ON tasks
            WHEN NEW.retain_until_ack=0 BEGIN SELECT RAISE(ABORT,'controlled background ACK failure'); END""")

    class LocalWork:
        def run(self, execution):
            return TaskOutcome('succeeded')
        def cancel(self):
            pass

    async def scenario():
        # Keep recovery explicit until the fault is observed, then let the real
        # owned maintenance repair it after the SQLite trigger is removed.
        async with _background_client(service, start_maintenance=False) as client:
            response = await client.post(f'/api/roles/{service.role.id}/generations', json=_request(service))
            assert response.status_code == 202, response.text
            accepted = response.json()['generation']
            assert (await _finished_task(service, accepted, origin)).status == 'succeeded'
            await app.recover()
            assert len(service.peer.requests) == 1
            assert app.owner_generation(service.role.id, accepted['generationId'], attempt_id=accepted['attemptId'])['status'] == 'succeeded'
            with pytest.raises(ValueError, match='retention.*unacknowledged'):
                service.tasks.submit(origin, LocalWork(), budget=TaskBudget(1, 100, 1024), kind='fixture', retain_until_ack=True)
            with sqlite3.connect(runtime_database) as connection:
                connection.execute('DROP TRIGGER fail_background_ack')
            app.start()
            await _eventually(lambda: app.owner_generation(service.role.id, accepted['generationId'],
                attempt_id=accepted['attemptId']), lambda row: row['deliveryStatus'] == 'sent')
            with pytest.raises(KeyError):
                service.tasks.query(accepted['taskId'], origin)
            next_id = service.tasks.submit(origin, LocalWork(), budget=TaskBudget(1, 100, 1024), kind='fixture', retain_until_ack=True)
            assert service.tasks.query(next_id, origin, wait_seconds=1).ended_at is not None
            await app.recover()
            assert len(service.sessions.list_messages(origin.session_key)) == 1
            assert len(service.peer.requests) == 1
    asyncio.run(scenario())


def test_explicit_history_only_background_consumes_once_without_creating_a_message(novelai_service):
    service = novelai_service
    app = service.application
    body = {**_request(service), 'deliveryMode': 'history_only'}
    async def scenario():
        async with _background_client(service) as client:
            path = f'/api/roles/{service.role.id}/generations'
            response = await client.post(path, json=body)
            assert response.status_code == 202, response.text
            accepted = response.json()['generation']
            assert accepted['deliveryMode'] == 'history_only'
            assert (await _finished_task(service, accepted, _ui_origin(service))).status == 'succeeded'
            await _eventually(lambda: app.owner_generation(service.role.id, accepted['generationId'],
                attempt_id=accepted['attemptId']), lambda row: row['deliveryStatus'] == 'history_only')
            replay = await client.post(path, json=body)
            assert replay.status_code == 202 and replay.json()['generation']['attemptId'] == accepted['attemptId']
            assert not service.sessions.list_messages(f'role:{service.role.id}')
            assert len(service.peer.requests) == 1
    asyncio.run(scenario())
