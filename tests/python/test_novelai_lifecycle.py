import asyncio

import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def test_owned_maintenance_finishes_only_local_work_and_close_joins(novelai_service):
    from backend.app.novelai_generation.context import GenerationContext
    from backend.app.task_runtime import TaskOrigin
    service = novelai_service
    source = GenerationContext(TaskOrigin(service.role.id, f'role:{service.role.id}', source_thread_id=f'desktop:{service.role.id}'), 'ui:local-repair', 'ui:local-repair', frozenset(), ui_action=True)
    async def scenario():
        with service.application.ledger.connect() as connection:
            connection.execute("CREATE TRIGGER local_disk_failure BEFORE UPDATE OF status ON novelai_attempts WHEN NEW.status='succeeded' BEGIN SELECT RAISE(ABORT,'fixture disk failure'); END")
        result = await service.application.submit(source, service.request)
        assert result['status'] == 'local_pending'
        with service.application.ledger.connect() as connection:
            connection.execute('DROP TRIGGER local_disk_failure')
        service.application.start()
        for _ in range(150):
            messages = service.sessions.list_messages(source.origin.session_key)
            if messages:
                break
            await asyncio.sleep(.02)
        assert len(messages) == 1
        assert len(service.peer.requests) == 1
        await service.application.close()
        with pytest.raises(PermissionError):
            await service.application.submit(source, service.request)
    asyncio.run(scenario())


def test_real_role_policy_revocation_stops_inflight_http_without_late_image(novelai_main):
    client, service = novelai_main
    service.peer.block = True
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor() as executor:
        generation = executor.submit(client.post, f'/api/roles/{service.role.id}/generations', json={
            'requestId': '17117117117117117117117117117118', 'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request})
        assert service.peer.accepted.wait(3)
        response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
            'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'manualEnabled': False}}})
        assert response.status_code == 200, response.text
        service.peer.release.set()
        assert generation.result(5).status_code == 403
    asyncio.run(service.application.recover())
    assert len(service.peer.requests) == 1
    assert not service.sessions.list_messages(f'role:{service.role.id}')
    history = service.application.history(service.role.id)['generations']
    assert history[0]['status'] == 'unknown'
    assert history[0]['deliveryStatus'] == 'suppressed'


def test_role_cannot_enable_generation_without_current_explicit_consent(novelai_main):
    client, service = novelai_main
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'consentRevision': 0}}})
    assert response.status_code == 422
    assert service.roles.get(service.role.id).agentConfig.novelai.consentRevision != 0


def test_role_deletion_purges_only_owned_generation_data_and_keeps_account_consumption(novelai_service):
    from backend.app.novelai_generation.context import GenerationContext
    from backend.app.task_runtime import TaskOrigin
    service = novelai_service
    source = GenerationContext(TaskOrigin(service.role.id, f'role:{service.role.id}', source_thread_id=f'desktop:{service.role.id}'), 'ui:delete', 'ui:delete', frozenset(), ui_action=True)
    async def scenario():
        await service.application.submit(source, service.request)
        await service.application.invalidate(service.role.id)
        service.roles.delete(service.role.id)
        service.sessions.delete_role_session(service.role.id)
        service.application.purge_role(service.role.id)
        assert not (service.application.root / service.role.id).exists()
        assert service.application.ledger.list(service.role.id)['generations'] == []
        usage = service.application.ledger.usage(service.role.id)
        assert usage['accountAttempts'] == 1 and usage['manualAttempts'] == 0
        assert service.application.settings.public()['tokenConfigured']
    asyncio.run(scenario())


@pytest.mark.parametrize('scope,count', [('global', 8), ('role', 3)])
def test_generation_consumes_existing_runtime_global_and_role_concurrency(novelai_service, scope, count):
    import threading
    from backend.app.novelai_generation.context import GenerationContext
    from backend.app.task_runtime import TaskOrigin
    from backend.app.task_runtime.runtime import TaskBudget, TaskOutcome
    service = novelai_service
    release = threading.Event()
    class HeldWork:
        def run(self, execution):
            assert release.wait(5)
            return TaskOutcome('succeeded')
        def cancel(self):
            release.set()
    try:
        for index in range(count):
            identifier = service.role.id if scope == 'role' else f'other-role-{index}'
            service.tasks.submit(TaskOrigin(identifier, f'role:{identifier}'), HeldWork(),
                budget=TaskBudget(5, 100, 1024), kind='fixture')
        source = GenerationContext(TaskOrigin(service.role.id, f'role:{service.role.id}', source_thread_id=f'desktop:{service.role.id}'), 'ui:budget', 'ui:budget', frozenset(), ui_action=True)
        with pytest.raises(ValueError, match='concurrency'):
            asyncio.run(service.application.submit(source, service.request))
        assert not service.peer.requests
        assert service.application.history(service.role.id)['usage']['accountAttempts'] == 0
    finally:
        release.set()
        for index in range(count):
            service.tasks.cancel_role_tasks(service.role.id if scope == 'role' else f'other-role-{index}')
