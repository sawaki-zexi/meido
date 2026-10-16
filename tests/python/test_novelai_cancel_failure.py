"""A failed cancel marker and SQLite seal still cannot abandon owned HTTP work."""
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


def test_foreground_disconnect_joins_runtime_even_when_both_seal_stores_fail(novelai_service, monkeypatch):
    from backend.app.task_runtime import TaskOrigin
    service = novelai_service
    service.peer.block = True
    api = FastAPI()
    api.include_router(service.application.router())
    marker_failed = threading.Event()
    original_open = Path.open
    def fail_marker(path, mode='r', *args, **kwargs):
        if path.suffix == '.cancelled' and 'x' in mode:
            marker_failed.set()
            raise OSError('controlled cancel marker failure')
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail_marker)
    with service.application.ledger.connect() as connection:
        connection.execute("CREATE TRIGGER fail_cancel_seal BEFORE INSERT ON novelai_cancelled_requests BEGIN SELECT RAISE(ABORT,'controlled cancel seal failure'); END")
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, log_level='critical', lifespan='off', ws='none'))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])))
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        async def scenario():
            async with httpx.AsyncClient(trust_env=False) as client:
                operation = asyncio.create_task(client.post(f'http://127.0.0.1:{port}/api/roles/{service.role.id}/generations',
                    json={'requestId': '17217217217217217217217217217217',
                        'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request}))
                assert await asyncio.to_thread(service.peer.accepted.wait, 3)
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await operation
                assert await asyncio.to_thread(marker_failed.wait, 2)
                origin = TaskOrigin(service.role.id, f'role:{service.role.id}', source_thread_id=f'desktop:{service.role.id}')
                for _ in range(75):
                    tasks = service.tasks.list(origin, kind='novelai_image')
                    if tasks and tasks[0].ended_at is not None:
                        break
                    await asyncio.sleep(.01)
                assert tasks and tasks[0].ended_at is not None, 'failed seal must still cancel and join Runtime before provider release'
                assert not service.peer.release.is_set()
                assert not service.sessions.list_messages(origin.session_key)
                assert len(service.peer.requests) == 1
        asyncio.run(scenario())
    finally:
        monkeypatch.setattr(Path, 'open', original_open)
        with service.application.ledger.connect() as connection:
            connection.execute('DROP TRIGGER fail_cancel_seal')
        service.peer.release.set()
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()
