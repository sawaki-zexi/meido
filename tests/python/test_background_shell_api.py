import asyncio
import shlex
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime import RuntimeManager
from backend.app.agent_runtime import ToolContext, CancellationToken
from backend.app.agent_runtime.shell_tool import TaskOutputTool
from backend.app.agent_runtime.types import ToolResultMessage
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from backend.app.todo import TodoStore


@pytest.fixture
def api(tmp_path, monkeypatch):
    initialize_databases(tmp_path / "data")
    roles, sessions = RoleStore(tmp_path / "roles"), SessionStore(tmp_path / "data" / "sessions.db")
    tasks = TaskRuntime(tmp_path / "tasks")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", SessionManager(roles, sessions))
    monkeypatch.setattr(main, "runtime_manager", RuntimeManager(sessions))
    monkeypatch.setattr(main, "model_configuration_store", ModelConfigurationStore(tmp_path / "data" / "models.json"))
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(main, "todo_store", TodoStore(tmp_path / "data" / "todos.db"))
    monkeypatch.setattr(main, "task_runtime", tasks, raising=False)
    yield TestClient(main.app), roles, sessions, tasks
    tasks.close()


def test_http_background_task_can_be_observed_and_stopped_in_later_turn(api, monkeypatch):
    client, roles, sessions, tasks = api

    class Adapter:
        requests = []
        task_id = None

        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests.append(tools)
            step = len(self.requests)
            if step == 1:
                yield ModelToolCall("start-owned-command", "shell", {
                    "command": shlex.join([sys.executable.replace("\\", "/"), "-u", "-c", "import time; print('http-ready'); time.sleep(5)"]),
                    "run_in_background": True,
                })
            elif step == 3:
                yield ModelToolCall("read-owned-command", "task_output", {"task_id": self.task_id, "block": True, "timeout_ms": 100})
            elif step == 5:
                yield ModelToolCall("stop-owned-command", "task_stop", {"task_id": self.task_id})
            else:
                yield ModelTextDelta("任务状态已返回")

    adapter = Adapter()
    monkeypatch.setattr(main, "model_adapter", adapter)
    role = client.post("/api/roles", json={"name": "后台执行", "profile": {"profile": "后台任务验收"}, "agentConfig": {
        "shell": {"enabled": True, "allowedCommands": [sys.executable], "timeoutSeconds": 10}
    }}).json()["role"]
    role_id = role["id"]
    response = client.post(f"/api/roles/{role_id}/messages", json={"content": "在后台执行"})
    assert "assistant_completed" in response.text
    assert {"shell", "task_output", "task_stop"} <= {item["function"]["name"] for item in adapter.requests[0]}
    results = [message for message in sessions.runtime_messages(f"role:{role_id}") if isinstance(message, ToolResultMessage)]
    submitted = results[-1]
    assert submitted.is_error is False
    adapter.task_id = submitted.details["taskId"]
    assert submitted.details["background"] is True
    parent = sessions.get_run(submitted.details["origin"]["parentRunId"])
    assert parent.status == "completed"
    assert submitted.details["origin"]["toolCallId"] == "start-owned-command"
    assert submitted.details["origin"]["capabilityVersion"] == parent.modelSnapshot["capabilities"]["snapshotId"]
    assert client.post(f"/api/roles/{role_id}/messages", json={"content": "查看进度"}).status_code == 200
    results = [message for message in sessions.runtime_messages(f"role:{role_id}") if isinstance(message, ToolResultMessage)]
    assert "http-ready" in results[-1].content
    assert results[-1].details["taskId"] == adapter.task_id
    assert client.post(f"/api/roles/{role_id}/messages", json={"content": "停止任务"}).status_code == 200
    results = [message for message in sessions.runtime_messages(f"role:{role_id}") if isinstance(message, ToolResultMessage)]
    assert results[-1].details["status"] == "cancelled"
    assert Path(results[-1].details["logPath"]).is_relative_to(tasks.root)
    assert not list(roles.workspace_path(role_id).rglob("*.log"))


@pytest.mark.parametrize("action", ["disable", "delete"])
def test_http_role_revocation_or_deletion_releases_real_background_process(api, monkeypatch, action):
    from test_background_shell import process_alive

    client, roles, sessions, tasks = api

    class Adapter:
        calls = 0
        schemas = []

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            self.schemas.append(tools)
            if self.calls == 1:
                yield ModelToolCall("start", "shell", {"command": shlex.join([
                    sys.executable.replace("\\", "/"), "-u", "-c",
                    "import os,time; from pathlib import Path; Path('pid').write_text(str(os.getpid())); time.sleep(10)",
                ]), "run_in_background": True})
            else:
                yield ModelTextDelta("已处理")

    adapter = Adapter()
    monkeypatch.setattr(main, "model_adapter", adapter)
    role = client.post("/api/roles", json={"name": "任务归属", "profile": {"profile": "设定"}, "agentConfig": {
        "shell": {"enabled": True, "allowedCommands": [sys.executable], "timeoutSeconds": 20}
    }}).json()["role"]
    role_id = role["id"]
    assert client.post(f"/api/roles/{role_id}/messages", json={"content": "后台执行"}).status_code == 200
    submitted = next(message for message in sessions.runtime_messages(f"role:{role_id}") if isinstance(message, ToolResultMessage))
    pid_file = roles.workspace_path(role_id) / "pid"
    deadline = time.monotonic() + 2
    while not pid_file.exists():
        assert time.monotonic() < deadline
        time.sleep(.01)
    pid = int(pid_file.read_text())
    assert process_alive(pid)
    if action == "disable":
        config = role["agentConfig"]
        config["shell"]["enabled"] = False
        config["enabledTools"] = ["shell", "task_output", "task_stop"]
        response = client.put(f"/api/roles/{role_id}", json={"name": role["name"], "profile": role["profile"], "agentConfig": config})
        assert response.status_code == 200
        assert client.post(f"/api/roles/{role_id}/messages", json={"content": "仍想查询"}).status_code == 200
        assert not {"shell", "task_output", "task_stop"} & {schema["function"]["name"] for schema in adapter.schemas[-1]}
    else:
        assert client.delete(f"/api/roles/{role_id}").status_code == 204
        assert not Path(submitted.details["logPath"]).exists()
        denied = asyncio.run(TaskOutputTool(tasks).execute({"task_id": submitted.details["taskId"]},
            ToolContext(role_id, f"role:{role_id}", "later", CancellationToken())))
        assert denied.is_error is True and "logPath" not in denied.details
    assert not process_alive(pid)
