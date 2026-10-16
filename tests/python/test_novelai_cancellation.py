import asyncio
from pathlib import Path
import socket
import threading
import time

from fastapi import FastAPI
import httpx
import pytest
import uvicorn

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401


@pytest.mark.parametrize('marker_write_failure', [False, True])
def test_real_http_disconnect_during_local_image_import_seals_late_ui_publication(novelai_service, monkeypatch, marker_write_failure):
    from backend.app.novelai_generation import media
    from backend.app.task_runtime import TaskOrigin
    service = novelai_service
    api = FastAPI()
    api.include_router(service.application.router())
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, log_level='critical', lifespan='off', ws='none'))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])))
    importing, release = threading.Event(), threading.Event()
    original = media.os.replace
    if marker_write_failure:
        original_open = Path.open
        def fail_cancel_marker(path, mode='r', *args, **kwargs):
            if path.suffix == '.cancelled' and 'x' in mode:
                raise OSError('controlled cancel marker write failure')
            return original_open(path, mode, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', fail_cancel_marker)
    def block_metadata(source, destination):
        if Path(destination).parent.name == 'attachments' and Path(destination).suffix == '.json':
            importing.set()
            assert release.wait(5), 'controlled import release timed out'
        return original(source, destination)
    monkeypatch.setattr(media.os, 'replace', block_metadata)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        async def scenario():
            async with httpx.AsyncClient(trust_env=False) as client:
                operation = asyncio.create_task(client.post(f'http://127.0.0.1:{port}/api/roles/{service.role.id}/generations',
                    json={'requestId': '17117117117117117117117117117117', 'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request}))
                assert await asyncio.to_thread(importing.wait, 3)
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await operation
                await asyncio.sleep(.15)
                release.set()
                origin = TaskOrigin(service.role.id, f'role:{service.role.id}', source_thread_id=f'desktop:{service.role.id}')
                for _ in range(200):
                    tasks = service.tasks.list(origin, kind='novelai_image')
                    if tasks and tasks[0].ended_at is not None:
                        break
                    await asyncio.sleep(.01)
                assert tasks and tasks[0].ended_at is not None
                await service.application.recover()
                assert len(service.peer.requests) == 1
                assert not service.sessions.list_messages(f'role:{service.role.id}')
                history = service.application.history(service.role.id)['generations']
                assert history[0]['deliveryStatus'] == 'suppressed'
                # Reconstruct the business application: no in-memory cancelled
                # set may be required to prevent late publication after restart.
                await service.application.close()
                from backend.app.novelai_generation.application import NovelaiApplication
                reopened = NovelaiApplication(root=service.application.root, tasks=service.tasks, sessions=service.sessions,
                    roles=service.roles, channels=None, credentials=service.application.settings.credentials, http=service.application.http)
                try:
                    await reopened.recover()
                    assert not service.sessions.list_messages(f'role:{service.role.id}')
                    assert reopened.history(service.role.id)['generations'][0]['deliveryStatus'] == 'suppressed'
                    assert len(service.peer.requests) == 1
                finally:
                    await reopened.close()
        asyncio.run(scenario())
    finally:
        release.set()
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()
