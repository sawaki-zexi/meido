import asyncio
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from fastapi.testclient import TestClient

import pytest
import httpx

from backend.app import main
from backend.app.agent_runtime import CapabilityRegistry, ImageContent, ToolContext
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.browser_use.config import BrowserUseConfig
from backend.app.browser_use.plugin import BROWSER_USE_MANIFEST, browser_use_plugin_factory
from backend.app.browser_use.runtime import BrowserRuntime
from backend.app.browser_use.safety import sanitize_browser_arguments, sanitize_browser_text
from backend.app.browser_use.tool import BrowserSessions, load_tools, load_tool_specs
from backend.app.agent_runtime.plugins import PluginContext, PluginRegistry
from backend.app.models import AgentToolConfig, Role, RoleProfile
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from datetime import datetime, timezone


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs["role_id"]))
        self.started.set()
        if name == "agent_browser_wait_ms":
            await self.release.wait()
        if name == "agent_browser_screenshot":
            return {"text": "screenshot captured", "image": "aW1hZ2U=", "mimeType": "image/png"}
        return {"text": "ok"}

    async def close(self):
        self.closed = True


def test_browser_runtime_is_fixed_and_reports_missing_resources(tmp_path):
    with pytest.raises((RuntimeError, FileNotFoundError), match="Windows|缺失"):
        BrowserRuntime.resolve(tmp_path)


def test_browser_process_transport_uses_large_stdio_line_limit(monkeypatch):
    import backend.app.browser_use.process as process_module

    captured = {}

    class FakeProcess:
        pid = 42

    async def create_process(*command, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(process_module.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(process_module, "sys", type("Platform", (), {"platform": "linux"})())

    async def run():
        process, owner = await process_module.spawn_process_tree("agent-browser", cwd=".", env={})
        assert process.pid == 42
        assert owner is None

    asyncio.run(run())
    assert captured["limit"] == 20 * 1024 * 1024


def test_browser_core_schema_has_all_shiori_tools_without_starting_browser(tmp_path):
    tools = load_tools(BrowserSessions(tmp_path, BrowserUseConfig(), transport=FakeTransport()))
    assert len(tools) == 28
    assert {tool.definition.name for tool in tools} >= {
        "agent_browser_open", "agent_browser_snapshot", "agent_browser_click",
        "agent_browser_screenshot", "agent_browser_tab_list", "agent_browser_close",
    }
    # Independent contract digest from Shiori 4f266ae's complete tools.json,
    # including all 28 names, descriptions, titles and input schemas.
    canonical = json.dumps(load_tool_specs(), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == "f7241bc43017bd3ae5590a9f250fe4b982e9bcd91e6e983682bfa83e965f2e59"


def test_browser_tool_rejects_host_owned_arguments(tmp_path):
    tool = next(
        item
        for item in load_tools(BrowserSessions(tmp_path, BrowserUseConfig(), transport=FakeTransport()))
        if item.definition.name == "agent_browser_open"
    )
    result = asyncio.run(tool.execute(
        {"url": "https://example.com", "session": "model-controlled"},
        ToolContext("role-1", "role:role-1", "run-1", CancellationToken()),
    ))
    assert result.is_error is True
    assert result.details == {"status": "denied"}


def test_browser_plugin_is_hidden_until_role_enables_it(tmp_path):
    registry = PluginRegistry([(BROWSER_USE_MANIFEST, browser_use_plugin_factory)])
    service = BrowserSessions(tmp_path, BrowserUseConfig(), transport=FakeTransport())
    context = PluginContext("browser_use", str(tmp_path), frozenset({"browser.session"}), {"browser.session": service})
    contribution = browser_use_plugin_factory(context)
    resolution = CapabilityRegistry(plugins=registry)
    disabled = asyncio.run(resolution.resolve_async(
        role_id="role-1", session_key="role:role-1", run_id="run-1",
        enabled_tools=set(), enabled_plugin_ids=set(), granted_capabilities={"browser.session"}, host_services={"browser.session": service},
    ))
    enabled = asyncio.run(resolution.resolve_async(
        role_id="role-1", session_key="role:role-1", run_id="run-2",
        enabled_tools={"agent_browser_open"}, enabled_plugin_ids={"browser_use"}, granted_capabilities={"browser.session"}, host_services={"browser.session": service},
    ))
    assert disabled.provider_schemas() == []
    assert [schema["function"]["name"] for schema in enabled.provider_schemas()] == ["agent_browser_open"]
    asyncio.run(context.close())
    asyncio.run(disabled.close())
    asyncio.run(enabled.close())
    assert contribution.tools


def test_browser_sessions_are_role_scoped_serial_and_cleanup_queued_work(tmp_path):
    transport = FakeTransport()
    sessions = BrowserSessions(tmp_path, BrowserUseConfig(), transport=transport)
    signal = CancellationToken()

    async def run():
        first = asyncio.create_task(sessions.call("role-a", "agent_browser_wait_ms", {}, signal))
        await transport.started.wait()
        second = asyncio.create_task(sessions.call("role-a", "agent_browser_open", {"url": "https://example.com"}, signal))
        await asyncio.sleep(0)
        assert [call[0] for call in transport.calls] == ["agent_browser_wait_ms"]
        signal.cancel()
        with pytest.raises(RuntimeError, match="cancelled"):
            await first
        transport.release.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            await second
        await sessions.close()

    asyncio.run(run())
    assert transport.closed is True
    assert transport.calls[0][2] == "role-a"


def test_disabling_role_blocks_new_browser_calls_until_reenabled(tmp_path):
    transport = FakeTransport()
    sessions = BrowserSessions(tmp_path, BrowserUseConfig(), transport=transport)
    signal = CancellationToken()

    async def run():
        await sessions.set_role_enabled("role-a", False)
        denied = await sessions.call("role-a", "agent_browser_open", {}, signal)
        assert denied.is_error is True
        assert denied.details["errorType"] == "PluginDisabled"
        await sessions.set_role_enabled("role-a", True)
        allowed = await sessions.call("role-a", "agent_browser_open", {}, signal)
        assert allowed.is_error is False
        await sessions.close()

    asyncio.run(run())
    assert [call[0] for call in transport.calls] == ["agent_browser_open"]


def test_screenshot_result_is_bounded_ephemeral_image():
    from backend.app.browser_use.tool import result_from_remote
    result = result_from_remote({"text": "screen", "image": "aW1hZ2U=", "mimeType": "image/png"})
    assert result.images == (ImageContent("image/png", "aW1hZ2U="),)
    assert "aW1hZ2U=" not in str(result.details)
    image_only = result_from_remote({"image": "aW1hZ2U=", "mimeType": "image/png"})
    assert "aW1hZ2U=" not in image_only.content


def test_browser_profiles_are_isolated_by_role_and_deleted_with_role_data(tmp_path, monkeypatch):
    class FakeDaemon:
        def __init__(self, executable, profile, env):
            self.executable = executable
            self.profile = profile
            self.env = env

        async def start(self, timeout):
            self.timeout = timeout

        async def close(self):
            return None

    class FakeMcpClient:
        instances = []

        def __init__(self, command, *, cwd, env):
            self.command = command
            self.cwd = cwd
            self.env = env
            self.closed = False
            self.calls = []
            self.instances.append(self)

        async def connect(self):
            return None

        async def call(self, name, arguments, *, timeout):
            self.calls.append((name, arguments, timeout))
            return {"text": "ok"}

        async def close(self):
            self.closed = True

    from backend.app.browser_use import session as session_module
    from backend.app.browser_use.profile import ProfileLease

    monkeypatch.setattr(session_module, "BrowserDaemon", FakeDaemon, raising=False)
    monkeypatch.setattr(session_module, "McpStdioSession", FakeMcpClient)
    runtime = BrowserRuntime(tmp_path / "agent-browser.exe", tmp_path / "chrome.exe")
    sessions = BrowserSessions(tmp_path / "data", BrowserUseConfig(), runtime=runtime)
    signal = CancellationToken()

    async def run():
        await sessions.call("role-a", "agent_browser_open", {}, signal)
        await sessions.call("role-b", "agent_browser_open", {}, signal)
        return sessions

    asyncio.run(run())
    clients = FakeMcpClient.instances
    assert len(clients) == 2
    assert clients[0].cwd != clients[1].cwd
    assert clients[0].env["AGENT_BROWSER_PROFILE"] != clients[1].env["AGENT_BROWSER_PROFILE"]
    assert clients[0].env["AGENT_BROWSER_SESSION"] != clients[1].env["AGENT_BROWSER_SESSION"]
    assert clients[0].env["AGENT_BROWSER_NAMESPACE"] == clients[1].env["AGENT_BROWSER_NAMESPACE"]

    role_a_profile = Path(clients[0].env["AGENT_BROWSER_PROFILE"])
    role_a_screenshots = tmp_path / "data" / "screenshots" / hashlib.sha256(b"role-a").hexdigest()
    role_a_screenshots.mkdir(parents=True)
    (role_a_screenshots / "latest.png").write_bytes(b"image")
    asyncio.run(sessions.close_role("role-a"))
    assert clients[0].calls[-1] == (
        "agent_browser_close",
        {"session": clients[0].env["AGENT_BROWSER_SESSION"]},
        5,
    )
    sessions.delete_role_data("role-a")
    assert not role_a_profile.exists()
    assert not role_a_screenshots.exists()
    lease = ProfileLease(role_a_profile)
    lease.close()
    asyncio.run(sessions.close())
    assert all(client.closed for client in clients)


def test_browser_close_reaps_owned_generation_and_reopens_same_role_profile(tmp_path, monkeypatch):
    class FakeDaemon:
        instances = []

        def __init__(self, executable, profile, env):
            self.executable = executable
            self.profile = profile
            self.env = env
            self.started_with = None
            self.closed = False
            self.instances.append(self)

        async def start(self, timeout):
            self.started_with = timeout

        async def close(self):
            self.closed = True

    class FakeMcpClient:
        instances = []

        def __init__(self, command, *, cwd, env):
            self.command = command
            self.cwd = cwd
            self.env = env
            self.calls = []
            self.disconnected = False
            self.instances.append(self)

        async def connect(self):
            return None

        async def call(self, name, arguments, *, timeout):
            self.calls.append((name, arguments, timeout))
            return {"text": "ok"}

        async def disconnect(self):
            self.disconnected = True

    from backend.app.browser_use import session as session_module

    monkeypatch.setattr(session_module, "BrowserDaemon", FakeDaemon, raising=False)
    monkeypatch.setattr(session_module, "McpStdioSession", FakeMcpClient)
    runtime = BrowserRuntime(tmp_path / "agent-browser.exe", tmp_path / "chrome.exe")
    sessions = BrowserSessions(tmp_path / "data", BrowserUseConfig(), runtime=runtime)
    signal = CancellationToken()

    async def run():
        await sessions.call("role-a", "agent_browser_open", {"url": "https://example.com"}, signal)
        first_client = FakeMcpClient.instances[0]
        first_daemon = FakeDaemon.instances[0]
        session_name = first_client.env["AGENT_BROWSER_SESSION"]
        assert first_client.env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] == "0"
        assert Path(first_client.env["AGENT_BROWSER_CONFIG"]).read_text(encoding="utf-8") == "{}"
        assert first_daemon.started_with == BrowserUseConfig().timeout_seconds
        assert first_client.calls[0] == (
            "agent_browser_open",
            {"url": "https://example.com", "session": session_name, "timeoutMs": 45000},
            45,
        )

        closed = await sessions.call("role-a", "agent_browser_close", {}, signal)
        assert "已关闭" in closed.content
        assert first_client.calls[-1] == ("agent_browser_close", {"session": session_name}, 5)
        assert first_client.disconnected is True
        assert first_daemon.closed is True

        await asyncio.wait_for(sessions.close_role("role-a"), timeout=1)
        await sessions.call("role-a", "agent_browser_open", {"url": "https://example.com"}, signal)
        second_client = FakeMcpClient.instances[1]
        second_daemon = FakeDaemon.instances[1]
        assert second_client.cwd == first_client.cwd
        assert second_client.env["AGENT_BROWSER_SESSION"] != session_name
        assert second_daemon.closed is False
        await sessions.close()

    asyncio.run(run())


def test_browser_sessions_use_a_fresh_run_session_with_a_reused_role_profile(tmp_path, monkeypatch):
    class FakeDaemon:
        instances = []

        def __init__(self, executable, profile, env):
            self.profile = profile
            self.env = env
            self.closed = False
            self.instances.append(self)

        async def start(self, timeout):
            self.timeout = timeout

        async def close(self):
            self.closed = True

    class FakeMcpClient:
        instances = []

        def __init__(self, command, *, cwd, env):
            self.cwd = cwd
            self.env = env
            self.closed = False
            self.instances.append(self)

        async def connect(self):
            return None

        async def call(self, name, arguments, *, timeout):
            return {"text": "ok"}

        async def disconnect(self):
            self.closed = True

    from backend.app.browser_use import session as session_module

    monkeypatch.setattr(session_module, "BrowserDaemon", FakeDaemon)
    monkeypatch.setattr(session_module, "McpStdioSession", FakeMcpClient)
    runtime = BrowserRuntime(tmp_path / "agent-browser.exe", tmp_path / "chrome.exe")
    sessions = BrowserSessions(tmp_path / "data", BrowserUseConfig(), runtime=runtime)
    signal = CancellationToken()

    async def run():
        await sessions.call("role-a", "agent_browser_open", {}, signal, run_id="run-1")
        first = FakeMcpClient.instances[0]
        await sessions.close_run("role-a", "run-1")
        await sessions.call("role-a", "agent_browser_open", {}, signal, run_id="run-2")
        second = FakeMcpClient.instances[1]
        assert first.cwd == second.cwd
        assert first.env["AGENT_BROWSER_PROFILE"] == second.env["AGENT_BROWSER_PROFILE"]
        assert first.env["AGENT_BROWSER_SESSION"] != second.env["AGENT_BROWSER_SESSION"]
        assert first.closed is True
        await sessions.close()
        assert second.closed is True

    asyncio.run(run())


def test_browser_sensitive_values_are_redacted_from_provider_transcript_and_storage(tmp_path):
    secret = "super-secret-value"
    safe_args = sanitize_browser_arguments(
        "agent_browser_fill",
        {"selector": "#password", "text": secret, "url": "https://example.test/?token=" + secret},
    )
    assert secret not in str(safe_args)
    safe_text, redactions = sanitize_browser_text(
        f'apiKey={secret} Authorization: Bearer {secret} Cookie: sid={secret}'
    )
    assert secret not in safe_text
    assert redactions >= 3

    role = Role(
        id="role-1", name="角色", profile=RoleProfile(profile="设定"),
        createdAt=datetime.now(timezone.utc), updatedAt=datetime.now(timezone.utc),
    )

    class CapturingAdapter:
        def __init__(self):
            self.requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            del configuration, tools
            self.requests.append(messages)
            yield ModelTextDelta("完成")

    adapter = CapturingAdapter()
    provider = MeidoProvider(adapter, role, None)

    async def run_provider():
        return [event async for event in provider.stream_response(
            model="test-model",
            system="",
            messages=[
                AssistantMessage(tool_calls=(ToolCall(
                    "call-1",
                    "agent_browser_fill",
                    {"selector": "#password", "text": secret},
                ),)),
                ToolResultMessage(
                    "call-1",
                    "agent_browser_fill",
                    f"apiKey={secret} Cookie: sid={secret}",
                ),
            ],
            tools=[{"type": "function", "function": {"name": "agent_browser_fill"}}],
            signal=CancellationToken(),
        )]

    asyncio.run(run_provider())
    assert secret not in str(adapter.requests)

    initialize_databases(tmp_path / "db")
    store = SessionStore(tmp_path / "db" / "sessions.db")
    session = store.open_role_session("role-1")
    store.append_agent_message(
        session.sessionKey,
        AssistantMessage(tool_calls=(ToolCall(
            "call-1", "agent_browser_fill", {"text": secret},
        ),)),
        run_id="run-1",
    )
    store.append_agent_message(
        session.sessionKey,
        ToolResultMessage(
            "call-1", "agent_browser_fill", f"apiKey={secret} Cookie: sid={secret}"
        ),
        run_id="run-1",
    )
    stored = store.list_messages(session.sessionKey)
    assert secret not in str(stored)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job ownership is platform-specific")
def test_browser_owned_native_process_exits_with_its_owner(tmp_path):
    import sys

    from backend.app.browser_use.process import spawn_process_tree
    child_pid_file = tmp_path / "browser-child.pid"
    code = (
        "import os, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "open(os.environ['MEIDO_BROWSER_CHILD_PID_FILE'], 'w').write(str(child.pid)); "
        "time.sleep(60)"
    )

    async def run():
        process, owner = await spawn_process_tree(
            sys.executable,
            "-c",
            code,
            cwd=str(tmp_path),
            env={**os.environ, "MEIDO_BROWSER_CHILD_PID_FILE": str(child_pid_file)},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert owner is not None
        try:
            for _ in range(100):
                if child_pid_file.is_file():
                    break
                await asyncio.sleep(0.02)
            assert child_pid_file.is_file()
            await asyncio.to_thread(owner.close)
            await asyncio.wait_for(process.wait(), timeout=5)
            assert process.returncode is not None
        finally:
            if process.returncode is None:
                from backend.app.browser_use.process import terminate_process_tree
                await terminate_process_tree(process)
            await asyncio.to_thread(owner.close)

    asyncio.run(run())


def test_missing_native_runtime_is_a_structured_tool_error(tmp_path):
    tool = next(item for item in load_tools(BrowserSessions(tmp_path, BrowserUseConfig(), resources=tmp_path)) if item.definition.name == "agent_browser_open")
    result = asyncio.run(tool.execute(
        {"url": "https://example.com"},
        ToolContext("role-1", "role:role-1", "run-1", CancellationToken()),
    ))
    assert result.is_error is True
    assert result.details["status"] == "unavailable"


def test_browser_mcp_failure_is_a_structured_tool_error(tmp_path):
    class FailingTransport:
        async def call(self, name, arguments, **kwargs):
            del name, arguments, kwargs
            from backend.app.browser_use.mcp import McpToolError
            raise McpToolError("net::ERR_CONNECTION_REFUSED")

        async def close(self):
            return None

    tool = next(
        item
        for item in load_tools(BrowserSessions(tmp_path, BrowserUseConfig(), transport=FailingTransport()))
        if item.definition.name == "agent_browser_open"
    )
    result = asyncio.run(tool.execute(
        {"url": "http://127.0.0.1:1/"},
        ToolContext("role-1", "role:role-1", "run-1", CancellationToken()),
    ))
    assert result.is_error is True
    assert result.details["status"] == "failed"
    assert result.details["errorType"] == "McpToolError"
    assert "ERR_CONNECTION_REFUSED" in result.details["message"]


def test_screenshot_image_is_encoded_only_for_the_next_provider_request():
    role = Role(
        id="role-1", name="角色", profile=RoleProfile(profile="设定"),
        createdAt=datetime.now(timezone.utc), updatedAt=datetime.now(timezone.utc),
    )
    class CapturingAdapter:
        def __init__(self):
            self.requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            del configuration, tools
            self.requests.append(messages)
            yield ModelTextDelta("已看到截图")

    adapter = CapturingAdapter()
    provider = MeidoProvider(adapter, role, None)

    async def run():
        return [event async for event in provider.stream_response(
            model="test-model",
            system="",
            messages=[
                UserMessage("看一下"),
                AssistantMessage(tool_calls=(ToolCall("call-1", "agent_browser_screenshot", {}),), stop_reason="tool_use"),
                ToolResultMessage("call-1", "agent_browser_screenshot", "截图", images=(ImageContent("image/png", "aW1hZ2U="),)),
            ],
            tools=[{"type": "function", "function": {"name": "agent_browser_screenshot"}}],
            signal=CancellationToken(),
        )]

    events = asyncio.run(run())
    assert events[-1].type == "assistant_done"
    assert adapter.requests[0][-1]["content"][1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="


def test_browser_authorization_persists_through_role_api(tmp_path, monkeypatch):
    roles = main.RoleStore(tmp_path / "roles")
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "browser_use_sessions", BrowserSessions(tmp_path / "browser", BrowserUseConfig(), transport=FakeTransport()))
    client = TestClient(main.app)

    response = client.post("/api/roles", json={
        "name": "浏览角色",
        "profile": {"profile": "允许网页操作"},
        "agentConfig": {"browserUse": {"enabled": True}},
    })

    assert response.status_code == 201
    role_id = response.json()["role"]["id"]
    assert response.json()["role"]["agentConfig"]["browserUse"]["enabled"] is True
    reloaded = main.RoleStore(tmp_path / "roles").get(role_id)
    assert reloaded is not None
    assert reloaded.agentConfig.browserUse.enabled is True


def test_enabled_browser_role_completes_tool_turn_and_keeps_screenshot_ephemeral(tmp_path, monkeypatch):
    class StructuredAdapter:
        def __init__(self):
            self.requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            del configuration
            self.requests.append((messages, tools))
            if len(self.requests) == 1:
                assert {item["function"]["name"] for item in tools} >= {
                    "agent_browser_open", "agent_browser_screenshot", "agent_browser_tab_list",
                }
                yield ModelToolCall("call-browser", "agent_browser_screenshot", {})
                return
            if len(self.requests) == 2:
                result = next(item for item in messages if item.get("role") == "tool")
                assert result["tool_call_id"] == "call-browser"
                assert result["content"][1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="
                yield ModelToolCall("call-browser-next", "agent_browser_tab_list", {})
                return
            if len(self.requests) == 3:
                screenshot = next(item for item in messages if item.get("tool_call_id") == "call-browser")
                assert screenshot["content"] == "screenshot captured"
                yield ModelTextDelta("网页已打开")
                return
            yield ModelTextDelta("网页已打开")

    adapter = StructuredAdapter()
    role, sessions, client, transport = _configure_browser_api(tmp_path, monkeypatch, adapter, enabled=True)
    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "打开网页"})

    assert response.status_code == 200
    assert "event: assistant_completed" in response.text
    assert len(adapter.requests) == 3
    runtime_messages = sessions.runtime_messages(f"role:{role.id}")
    screenshot_result = next(item for item in runtime_messages if isinstance(item, ToolResultMessage))
    assert screenshot_result.tool_name == "agent_browser_screenshot"
    assert screenshot_result.content == "screenshot captured"
    assert screenshot_result.images == ()
    session_payload = client.get(f"/api/roles/{role.id}/session").json()
    assert "aW1hZ2U=" not in str(session_payload)
    assert all(message["messageType"] == "text" for message in session_payload["messages"])

    updated = client.put(f"/api/roles/{role.id}", json={
        "name": role.name,
        "description": role.description,
        "profile": role.profile.model_dump(),
        "agentConfig": {"browserUse": {"enabled": False}},
    })
    assert updated.status_code == 200
    assert transport.closed is True
    disabled_response = client.post(f"/api/roles/{role.id}/messages", json={"content": "再看网页"})
    assert disabled_response.status_code == 200
    assert "event: assistant_completed" in disabled_response.text
    assert len(adapter.requests) == 4


def test_disabled_browser_role_rejects_forged_tool_call(tmp_path, monkeypatch):
    class StructuredAdapter:
        def __init__(self):
            self.requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            del messages, configuration
            self.requests.append(tools)
            if len(self.requests) == 1:
                assert "agent_browser_open" not in {item["function"]["name"] for item in tools}
                yield ModelToolCall("call-browser", "agent_browser_open", {"url": "https://example.com"})
            else:
                yield ModelTextDelta("无法访问网页")

    adapter = StructuredAdapter()
    role, sessions, client, _ = _configure_browser_api(tmp_path, monkeypatch, adapter, enabled=False)
    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "打开网页"})

    assert response.status_code == 200
    assert len(adapter.requests) == 2
    results = [item for item in sessions.runtime_messages(f"role:{role.id}") if isinstance(item, ToolResultMessage)]
    assert results[0].is_error is True
    assert results[0].details["reason"]


def test_real_windows_browser_page_workflow_when_native_resources_are_configured(tmp_path):
    resource_root = os.getenv("MEIDO_BROWSER_USE_RESOURCES")
    if not resource_root:
        pytest.skip("set MEIDO_BROWSER_USE_RESOURCES to run the native Windows browser acceptance")
    runtime = BrowserRuntime.resolve(Path(resource_root))
    original_processes = _native_browser_process_ids(runtime)

    class PageHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'''<!doctype html>
<html><head><title>Meido browser acceptance</title></head>
<body><h1>Local browser acceptance</h1><label>Name<input id="name"></label>
<label>Subscribe<input id="subscribe" type="checkbox"></label>
<label>Choice<select id="choice"><option value="one">One</option><option value="two">Two</option></select></label>
<button id="save" onclick="document.querySelector('#status').textContent = 'saved:' + document.querySelector('#name').value; localStorage.setItem('browser-use', 'role-a'); document.cookie = 'browser-owner=role-a; Max-Age=3600; path=/'">Save</button>
<p id="status">Waiting</p><div style="height:1500px">Scroll area</div></body></html>'''
            if self.path == "/second":
                body = body.replace(b"Meido browser acceptance", b"Meido second tab").replace(
                    b"Local browser acceptance", b"Second browser acceptance"
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    sessions = BrowserSessions(tmp_path / "browser", BrowserUseConfig(), resources=Path(resource_root), runtime=runtime)
    signal = CancellationToken()
    tools = {tool.definition.name: tool for tool in load_tools(sessions)}
    invoked = set()
    observed_processes = set()

    async def invoke(role_id, name, arguments=None, token=None):
        if name == "agent_browser_close":
            observed_processes.update(_native_browser_process_ids(runtime))
        result = await tools[name].execute(arguments or {}, ToolContext(role_id, f"role:{role_id}", "native-acceptance", token or signal))
        if name == "agent_browser_open" and not result.is_error:
            observed_processes.update(_native_browser_process_ids(runtime))
        invoked.add(name)
        return result

    async def call(role_id, name, arguments=None, token=None):
        result = await invoke(role_id, name, arguments, token)
        assert result.is_error is False, (name, result.content, result.details)
        return result

    async def assert_current_page(expected_url, expected_title):
        current_url = await call("role-a", "agent_browser_get_url")
        current_title = await call("role-a", "agent_browser_get_title")
        assert current_url.content == expected_url
        assert current_title.content == expected_title

    async def run():
        nonlocal sessions, tools
        await call("role-a", "agent_browser_open", {"url": url})
        read = await call("role-a", "agent_browser_read", {"url": url})
        assert "Local browser acceptance" in read.content
        await call("role-a", "agent_browser_wait_for_selector", {"selector": "#name", "waitTimeoutMs": 2000})
        await call("role-a", "agent_browser_wait_for_text", {"text": "Local browser acceptance", "waitTimeoutMs": 2000})
        await call("role-a", "agent_browser_wait_for_load", {"state": "load", "waitTimeoutMs": 2000})
        await call("role-a", "agent_browser_wait_ms", {"ms": 10})
        snapshot = await call("role-a", "agent_browser_snapshot")
        assert "Local browser acceptance" in snapshot.content
        textbox = next(line for line in snapshot.content.splitlines() if "textbox" in line and "Name" in line)
        button = next(line for line in snapshot.content.splitlines() if "button" in line and "Save" in line)
        textbox_ref = re.search(r"\[ref=([^\]]+)\]", textbox)
        button_ref = re.search(r"\[ref=([^\]]+)\]", button)
        assert textbox_ref is not None and button_ref is not None
        await call("role-a", "agent_browser_fill", {"selector": f"@{textbox_ref.group(1)}", "text": "Meido"})
        await call("role-a", "agent_browser_type", {"selector": "#name", "text": "-native"})
        initial_choice = await call("role-a", "agent_browser_eval", {"script": "document.querySelector('#choice').value"})
        assert json.loads(initial_choice.content) == "one"
        await call("role-a", "agent_browser_select", {"selector": "#choice", "values": ["two"]})
        selected_choice = await call("role-a", "agent_browser_eval", {"script": "document.querySelector('#choice').value"})
        assert json.loads(selected_choice.content) == "two"
        await call("role-a", "agent_browser_check", {"selector": "#subscribe"})
        checked = await call("role-a", "agent_browser_eval", {"script": "document.querySelector('#subscribe').checked"})
        assert "true" in checked.content
        await call("role-a", "agent_browser_uncheck", {"selector": "#subscribe"})
        unchecked = await call("role-a", "agent_browser_eval", {"script": "document.querySelector('#subscribe').checked"})
        assert "false" in unchecked.content
        await call("role-a", "agent_browser_scroll", {"direction": "down", "amount": 200})
        scrolled = await call("role-a", "agent_browser_eval", {"script": "window.scrollY > 0"})
        assert "true" in scrolled.content
        await call("role-a", "agent_browser_scroll", {"direction": "up", "amount": 200})
        await call("role-a", "agent_browser_press", {"key": "Tab"})
        await call("role-a", "agent_browser_click", {"selector": f"@{button_ref.group(1)}"})
        text = await call("role-a", "agent_browser_get_text", {"selector": "#status"})
        assert "saved:Meido-native" in text.content
        screenshot = await call("role-a", "agent_browser_screenshot")
        assert screenshot.images and len(screenshot.images[0].data_base64) < 2_700_000
        initial_tabs = await call("role-a", "agent_browser_tab_list")
        original_tabs = json.loads(initial_tabs.content)["data"]["tabs"]
        assert len(original_tabs) == 1
        original_tab_id = original_tabs[0]["tabId"]
        assert original_tabs[0]["url"] == url
        second_url = url + "second"
        await call("role-a", "agent_browser_tab_new", {"label": "second", "url": second_url})
        await call("role-a", "agent_browser_wait_for_text", {"text": "Second browser acceptance", "waitTimeoutMs": 2000})
        await assert_current_page(second_url, "Meido second tab")
        tabs = await call("role-a", "agent_browser_tab_list")
        opened_tabs = json.loads(tabs.content)["data"]["tabs"]
        second_tab = next(tab for tab in opened_tabs if tab["label"] == "second")
        second_tab_id = second_tab["tabId"]
        assert second_tab["url"] == second_url
        assert {tab["tabId"] for tab in opened_tabs} == {original_tab_id, second_tab_id}
        await call("role-a", "agent_browser_tab_switch", {"tab": original_tab_id})
        await assert_current_page(url, "Meido browser acceptance")
        await call("role-a", "agent_browser_tab_switch", {"tab": "second"})
        await assert_current_page(second_url, "Meido second tab")
        await call("role-a", "agent_browser_tab_close", {"tab": "second"})
        remaining = await call("role-a", "agent_browser_tab_list")
        remaining_tabs = json.loads(remaining.content)["data"]["tabs"]
        assert {tab["tabId"] for tab in remaining_tabs} == {original_tab_id}
        assert remaining_tabs[0]["url"] == url
        await assert_current_page(url, "Meido browser acceptance")
        await call("role-a", "agent_browser_open", {"url": url})
        stale = await invoke("role-a", "agent_browser_click", {"selector": f"@{button_ref.group(1)}"})
        assert stale.is_error is True
        assert stale.details["status"] == "failed"
        await call("role-a", "agent_browser_snapshot")
        await call("role-a", "agent_browser_open", {"url": url + "next"})
        await call("role-a", "agent_browser_back")
        previous = await call("role-a", "agent_browser_get_url")
        assert url in previous.content and "/next" not in previous.content
        await call("role-a", "agent_browser_forward")
        following = await call("role-a", "agent_browser_get_url")
        assert url + "next" in following.content
        await call("role-a", "agent_browser_reload")
        await call("role-a", "agent_browser_close")
        await sessions.close_role("role-a")
        await sessions.close()

        # Restart the host service as well as Chrome, retaining only disk data.
        sessions = BrowserSessions(tmp_path / "browser", BrowserUseConfig(), resources=Path(resource_root), runtime=runtime)
        tools = {tool.definition.name: tool for tool in load_tools(sessions)}

        await call("role-a", "agent_browser_open", {"url": url})
        persisted = await call("role-a", "agent_browser_eval", {"script": "localStorage.getItem('browser-use')"})
        assert "role-a" in persisted.content
        cookie = await call("role-a", "agent_browser_eval", {"script": "document.cookie"})
        assert "browser-owner=role-a" in cookie.content
        await call("role-a", "agent_browser_close")
        await sessions.close_role("role-a")

        await call("role-b", "agent_browser_open", {"url": url})
        isolated = await call("role-b", "agent_browser_eval", {"script": "localStorage.getItem('browser-use')"})
        assert "role-a" not in isolated.content
        other_cookie = await call("role-b", "agent_browser_eval", {"script": "document.cookie"})
        assert "browser-owner" not in other_cookie.content

        wait_signal = CancellationToken()
        waiting = asyncio.create_task(invoke("role-b", "agent_browser_wait_ms", {"ms": 20000}, wait_signal))
        await asyncio.sleep(0.75)
        wait_signal.cancel()
        with pytest.raises(RuntimeError, match="cancelled"):
            await waiting
        await call("role-b", "agent_browser_open", {"url": url})
        title = await call("role-b", "agent_browser_get_title")
        assert "Meido browser acceptance" in title.content
        assert invoked == set(tools), "the native acceptance must exercise every declared tool"
        await sessions.close()
        from backend.app.browser_use.profile import ProfileLease
        for role_id in ("role-a", "role-b"):
            lease = ProfileLease(tmp_path / "browser" / "profiles" / hashlib.sha256(role_id.encode()).hexdigest())
            lease.close()
        assert not list((tmp_path / "browser" / "run").rglob("*.port"))
        assert not list((tmp_path / "browser" / "run").rglob("*.config"))
        assert _native_browser_process_ids(runtime, tracked=observed_processes) <= original_processes

    async def run_with_cleanup():
        try:
            await run()
        finally:
            await sessions.close()

    try:
        asyncio.run(run_with_cleanup())
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def _native_browser_process_ids(runtime, *, tracked=()):
    """Observe this test process's native descendants, including tracked orphans."""
    command = (
        "$paths = ConvertFrom-Json $env:MEIDO_TEST_NATIVE_PATHS; "
        "$tracked = ConvertFrom-Json $env:MEIDO_TEST_NATIVE_PIDS; "
        "$all = @(Get-CimInstance Win32_Process); "
        "$owned = [System.Collections.Generic.HashSet[int]]::new(); "
        "$null = $owned.Add([int]$env:MEIDO_TEST_OWNER_PID); "
        "do { $changed = $false; foreach ($process in $all) { "
        "if ($owned.Contains([int]$process.ParentProcessId)) { "
        "if ($owned.Add([int]$process.ProcessId)) { $changed = $true } } } } while ($changed); "
        "ConvertTo-Json -Compress -InputObject @($all | Where-Object { "
        r"$image = $_.ExecutablePath; if ($image -and $image.StartsWith('\\?\')) { $image = $image.Substring(4) }; "
        "($owned.Contains([int]$_.ProcessId) -or $tracked -contains $_.ProcessId) -and $paths -contains $image "
        "} | ForEach-Object { $_.ProcessId })"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ,
             "MEIDO_TEST_NATIVE_PATHS": json.dumps([str(runtime.agent_browser), str(runtime.chrome)]),
             "MEIDO_TEST_NATIVE_PIDS": json.dumps(list(tracked)), "MEIDO_TEST_OWNER_PID": str(os.getpid())},
        check=True, capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return set(json.loads(result.stdout.strip() or "[]"))


def test_native_browser_http_tool_round_trip_and_active_role_deletion(tmp_path, monkeypatch):
    resource_root = os.getenv("MEIDO_BROWSER_USE_RESOURCES")
    if not resource_root:
        pytest.skip("set MEIDO_BROWSER_USE_RESOURCES to run the native Windows browser acceptance")
    runtime = BrowserRuntime.resolve(Path(resource_root))
    original_processes = _native_browser_process_ids(runtime)

    class PageHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html><title>Native HTTP acceptance</title><h1>Real browser from role chat</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    browser = BrowserSessions(tmp_path / "browser", BrowserUseConfig(), runtime=runtime)

    class Adapter:
        requests = []
        native_pids = set()

        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests.append((messages, tools))
            if len(self.requests) > 1:
                self.native_pids.update(_native_browser_process_ids(runtime))
            if len(self.requests) == 1:
                yield ModelToolCall("native-open", "agent_browser_open", {"url": url})
            elif len(self.requests) == 2:
                yield ModelToolCall("native-screenshot", "agent_browser_screenshot", {})
            else:
                assert messages[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
                yield ModelTextDelta("真实浏览器完成")

    roles = main.RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    from backend.app.todo import TodoStore
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", main.SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", Adapter())
    monkeypatch.setattr(main, "model_configuration_store", main.ModelConfigurationStore(tmp_path / "data" / "models.json"))
    monkeypatch.setattr(main, "todo_store", TodoStore(tmp_path / "data" / "todos.db"))
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(main, "browser_use_sessions", browser)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://meido.test") as client:
                created = await client.post("/api/roles", json={
                    "name": "原生浏览器验收", "profile": {"profile": "使用本地测试网页"},
                    "agentConfig": {"browserUse": {"enabled": True}},
                })
                assert created.status_code == 201
                role_id = created.json()["role"]["id"]
                response = await client.post(f"/api/roles/{role_id}/messages", json={"content": "打开本地页面并截图"})
                assert "assistant_completed" in response.text
                assert "真实浏览器完成" in response.text
                assert {
                    item["function"]["name"] for item in main.model_adapter.requests[0][1]
                    if item["function"]["name"].startswith("agent_browser_")
                } == set(BROWSER_USE_MANIFEST.declared_tools)
                assert "iVBORw0KGgo" not in str(sessions.list_messages(f"role:{role_id}"))
                assert _native_browser_process_ids(runtime, tracked=main.model_adapter.native_pids) <= original_processes

                # Delete through the real role API while a new native run owns
                # an active Chrome/profile, using the same application loop.
                tool = next(item for item in load_tools(browser) if item.definition.name == "agent_browser_open")
                opened = await tool.execute({"url": url}, ToolContext(role_id, f"role:{role_id}", "active-delete", CancellationToken()))
                assert not opened.is_error, (opened.content, opened.details)
                active_processes = _native_browser_process_ids(runtime)
                assert active_processes - original_processes
                response = await client.delete(f"/api/roles/{role_id}")
                assert response.status_code == 204, response.text
                identity = hashlib.sha256(role_id.encode()).hexdigest()
                assert not (tmp_path / "browser" / "profiles" / identity).exists()
                assert not (tmp_path / "browser" / "screenshots" / identity).exists()
                assert _native_browser_process_ids(runtime, tracked=active_processes | main.model_adapter.native_pids) <= original_processes
        finally:
            await browser.close()

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def _configure_browser_api(tmp_path, monkeypatch, adapter, *, enabled):
    roles = main.RoleStore(tmp_path / "roles")
    role = roles.create(main.RoleInput(
        name="浏览角色",
        profile=RoleProfile(profile="网页设定"),
        agentConfig=AgentToolConfig(browserUse={"enabled": enabled}),
    ))
    main.initialize_databases(tmp_path / "data")
    sessions = main.SessionStore(tmp_path / "data" / "sessions.db")
    transport = FakeTransport()
    browser_sessions = BrowserSessions(tmp_path / "browser", BrowserUseConfig(), transport=transport)
    monkeypatch.setattr(main, "store", roles)
    monkeypatch.setattr(main, "session_store", sessions)
    monkeypatch.setattr(main, "session_manager", main.SessionManager(roles, sessions))
    monkeypatch.setattr(main, "model_adapter", adapter)
    monkeypatch.setattr(main, "model_configuration_store", main.ModelConfigurationStore(tmp_path / "data" / "model-config.json"))
    monkeypatch.setattr(main, "role_locks", {})
    monkeypatch.setattr(main, "browser_use_sessions", browser_sessions)
    return role, sessions, TestClient(main.app), transport
