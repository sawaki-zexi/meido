"""A disconnected foreground action must not replace an old image late."""
import asyncio
import socket
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401


def test_real_disconnect_after_runtime_finishes_prevents_foreground_replacement(novelai_service, monkeypatch):
    service = novelai_service
    api = FastAPI()
    api.include_router(service.application.router())
    path = f'/api/roles/{service.role.id}/generations'
    revision = service.application.settings.public()['revision']
    with TestClient(api) as client:
        response = client.post(path, json={'requestId': '17217217217217217217217217217217',
            'expectedRevision': revision, 'parameters': service.request})
        assert response.status_code == 200, response.text
        original = response.json()['generation']
    message = service.sessions.list_messages(f'role:{service.role.id}')[-1]
    endpoint = path + '/' + original['generationId'] + '/regenerate'
    arguments = {'requestId': '27227227227227227227227227227227', 'expectedRevision': revision,
        'sourceAttemptId': original['attemptId'], 'expectedAttachmentId': original['attachments'][0]['id'],
        'parameters': {key: value for key, value in service.request.items() if key != 'seed'}}
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, log_level='critical', lifespan='off', ws='none'))
    server_thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])))
    observed, release = threading.Event(), threading.Event()
    real_query = service.tasks.query

    def observe_terminal(task_id, origin, **kwargs):
        snapshot = real_query(task_id, origin, **kwargs)
        if snapshot.ended_at is not None and task_id != original['taskId'] and not observed.is_set():
            observed.set()
            assert release.wait(5), 'controlled terminal-observation release timed out'
        return snapshot

    monkeypatch.setattr(service.tasks, 'query', observe_terminal)
    server_thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started

        async def scenario():
            async with httpx.AsyncClient(trust_env=False) as client:
                request = asyncio.create_task(client.post(f'http://127.0.0.1:{port}' + endpoint, json=arguments))
                assert await asyncio.to_thread(observed.wait, 3)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                release.set()
                for _ in range(300):
                    history = service.application.history(service.role.id)['generations']
                    latest = next((item for item in history if item['attemptId'] != original['attemptId']), None)
                    if latest and latest['deliveryStatus'] in {'sent', 'suppressed'}:
                        break
                    await asyncio.sleep(.01)
                assert latest and len(service.peer.requests) == 2
                assert service.sessions.list_messages(f'role:{service.role.id}')[-1].metadata == message.metadata
                await service.application.recover()
                assert service.sessions.list_messages(f'role:{service.role.id}')[-1].metadata == message.metadata
                assert latest['deliveryStatus'] == 'suppressed'
        asyncio.run(scenario())
    finally:
        release.set()
        server.should_exit = True
        server_thread.join(10)
        listener.close()
        assert not server_thread.is_alive()
