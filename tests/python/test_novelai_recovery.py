import asyncio
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401


def test_received_image_import_recovers_only_local_steps_after_real_metadata_write_failure(novelai_service, monkeypatch):
    from backend.app.novelai_generation import media
    service = novelai_service
    api = FastAPI()
    api.include_router(service.application.router())
    path = f'/api/roles/{service.role.id}/generations'
    request = {'requestId': '17117117117117117117117117117117',
        'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request}
    original = media.os.replace
    failures = []
    def fail_metadata(source, destination):
        if Path(destination).parent.name == 'attachments' and Path(destination).suffix == '.json':
            failures.append(str(destination))
            raise OSError('controlled metadata write failure')
        return original(source, destination)
    with TestClient(api) as client:
        with monkeypatch.context() as patch:
            patch.setattr(media.os, 'replace', fail_metadata)
            result = client.post(path, json=request).json()['generation']
        assert result['status'] == 'local_pending' and len(failures) == 1
        assert len(service.peer.requests) == 1
        assert not service.sessions.list_messages(f'role:{service.role.id}')
        original_open = Path.open
        def no_binary_rewrite(path, mode='r', *args, **kwargs):
            if 'w' in mode and path.name.endswith('.png.partial'):
                raise AssertionError('recovery must reuse the already imported binary')
            return original_open(path, mode, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'open', no_binary_rewrite)
            asyncio.run(service.application.recover())
        recovered = client.get(path).json()['generations'][0]
        assert recovered['status'] == 'succeeded' and recovered['deliveryStatus'] == 'sent'
        assert len(recovered['attachments']) == 1 and len(service.peer.requests) == 1
        assert len(service.sessions.list_messages(f'role:{service.role.id}')) == 1
        asyncio.run(service.application.recover())
        assert len(service.sessions.list_messages(f'role:{service.role.id}')) == 1


def test_import_commit_failure_after_spool_removal_recovers_owned_images_without_new_post(novelai_service):
    service = novelai_service
    api = FastAPI()
    api.include_router(service.application.router())
    path = f'/api/roles/{service.role.id}/generations'
    request = {'requestId': '17117117117117117117117117117117',
        'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request}
    with sqlite3.connect(service.sessions.database_path) as connection:
        connection.execute("CREATE TRIGGER fail_generation_import BEFORE UPDATE OF status ON novelai_attempts WHEN NEW.status='succeeded' BEGIN SELECT RAISE(ABORT,'controlled imported state write failure'); END")
    with TestClient(api) as client:
        result = client.post(path, json=request).json()['generation']
        assert result['status'] == 'local_pending'
        assert len(service.peer.requests) == 1
        with sqlite3.connect(service.sessions.database_path) as connection:
            connection.execute('DROP TRIGGER fail_generation_import')
        asyncio.run(service.application.recover())
        recovered = client.get(path).json()['generations'][0]
        assert recovered['status'] == 'succeeded' and recovered['deliveryStatus'] == 'sent'
        assert len(recovered['attachments']) == 1 and len(service.peer.requests) == 1


def test_failed_runtime_handover_write_joins_foreground_and_releases_retained_result_locally(novelai_service):
    from backend.app.task_runtime import TaskOrigin
    service = novelai_service
    api = FastAPI()
    api.include_router(service.application.router())
    path = f'/api/roles/{service.role.id}/generations'
    with sqlite3.connect(service.sessions.database_path) as connection:
        connection.execute("CREATE TRIGGER fail_task_handover BEFORE UPDATE OF task_id ON novelai_attempts WHEN NEW.task_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'controlled handover write failure'); END")
    with TestClient(api) as client:
        result = client.post(path, json={'requestId': '17117117117117117117117117117119',
            'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request})
        assert result.status_code == 503
        origin = TaskOrigin(service.role.id, f'role:{service.role.id}', source_thread_id=f'desktop:{service.role.id}')
        tasks = service.tasks.list(origin, kind='novelai_image')
        assert len(tasks) == 1 and tasks[0].ended_at is not None
        assert not service.peer.requests
        with sqlite3.connect(service.sessions.database_path) as connection:
            connection.execute('DROP TRIGGER fail_task_handover')
        asyncio.run(service.application.recover())
        history = client.get(path).json()['generations'][0]
        assert history['taskId'] == tasks[0].task_id
        assert history['status'] == 'cancelled_before_dispatch'
        assert service.application.history(service.role.id)['usage']['manualAttempts'] == 0
        # Exercise the real Runtime retention seam: recovery must have ACKed,
        # so pruning can free its sole retained slot for another task.
        service.tasks.max_retained_per_role = 1
        service.tasks.retention_seconds = 0
        from backend.app.task_runtime.runtime import TaskBudget, TaskOutcome
        class LocalWork:
            def run(self, execution):
                return TaskOutcome('succeeded')
            def cancel(self):
                pass
        next_id = service.tasks.submit(origin, LocalWork(), budget=TaskBudget(1, 100, 1024), kind='fixture', retain_until_ack=True)
        assert service.tasks.query(next_id, origin, wait_seconds=1).ended_at is not None
