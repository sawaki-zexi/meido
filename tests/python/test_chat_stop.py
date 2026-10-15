import json
import socket
import threading

import httpx
import pytest
import uvicorn

from backend.app import main
from backend.app.agent_runtime.runtime import RuntimeManager
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


@pytest.fixture
def chat_server(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="女仆", profile=RoleProfile(profile="设定")))
    other = roles.create(RoleInput(name="另一位", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data/sessions.db")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "runtime_manager", RuntimeManager(sessions))
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "models.json"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(main.app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if server.started:
                break
            threading.Event().wait(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=5) as client:
            yield client, role, other
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


@pytest.mark.parametrize("partial", [False, True])
def test_stop_desktop_reply_before_or_after_first_token(chat_server, monkeypatch, partial):
    import asyncio

    client, role, other = chat_server
    cancelled = threading.Event()

    class Adapter:
        async def stream_reply(self, role, history, configuration=None):
            if history[-1].content == "下一句":
                yield "后续回复"
                return
            try:
                if partial:
                    yield "部分回复"
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(main, "model_adapter", Adapter())
    with client.stream("POST", f"/api/roles/{role.id}/messages", json={"content": "第一句"}) as stream:
        assert stream.status_code == 200
        run_id = None
        for line in stream.iter_lines():
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                run_id = payload.get("runId", run_id)
                if (partial and payload.get("delta")) or (not partial and payload.get("messageId")):
                    break
        assert run_id
        assert client.post(f"/api/roles/{other.id}/runs/{run_id}/stop").status_code == 404
        stopped = client.post(f"/api/roles/{role.id}/runs/{run_id}/stop")
        assert stopped.status_code == 200
        messages = stopped.json()["messages"]
        assert messages[-1]["metadata"]["runtimeReason"] == "cancelled"
        assert messages[-1]["content"] == ("部分回复" if partial else "")
        assert messages[0]["content"] == "第一句"
        if partial:
            assert cancelled.wait(timeout=1)

    assert client.post(f"/api/roles/{role.id}/runs/{run_id}/stop").status_code == 200
    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "下一句"})
    assert response.status_code == 200
    assert "event: assistant_completed" in response.text
    assert "后续回复" in response.text
    history = client.get(f"/api/roles/{role.id}/session").json()["messages"]
    assert [item["content"] for item in history] == ["第一句", "部分回复" if partial else "", "下一句", "后续回复"]
