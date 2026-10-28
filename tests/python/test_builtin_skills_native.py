"""Opt-in integration checks using installed CLIs and public weather services.

Run with MEIDO_SKILL_NATIVE_TESTS=1 and MEIDO_SUMMARIZE_CLI pointing to the
installed @steipete/summarize/dist/cli.js. Node 24+ and curl must be on PATH.
Enabling these checks makes missing dependencies or network failures failures.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    os.environ.get("MEIDO_SKILL_NATIVE_TESTS") != "1",
    reason="real CLI/network tests require MEIDO_SKILL_NATIVE_TESTS=1",
)


@pytest.fixture
def native_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    for key, value in {
        "HOME": home, "USERPROFILE": home, "TMP": temporary, "TEMP": temporary,
        "MEIDO_DATA_DIR": tmp_path / "data", "MEIDO_ROLES_DIR": tmp_path / "roles",
        "MEIDO_PROJECT_ROOT": tmp_path / "project",
    }.items():
        monkeypatch.setenv(key, str(value))
    return home


def require_program(name):
    path = shutil.which(name)
    if path is None:
        pytest.fail(f"native checks are enabled, but {name} is missing from PATH")
    return Path(path).resolve().as_posix()


@pytest.fixture
def summarize_installation(monkeypatch, native_environment):
    node = require_program("node")
    configured = os.environ.get("MEIDO_SUMMARIZE_CLI")
    if not configured:
        pytest.fail("native checks are enabled, but MEIDO_SUMMARIZE_CLI must name the real installed cli.js")
    cli = Path(configured).expanduser().resolve()
    if not cli.is_file() or cli.suffix != ".js":
        pytest.fail(f"MEIDO_SUMMARIZE_CLI is not an installed JavaScript CLI: {cli}")
    node_modules = next((parent for parent in cli.parents if parent.name == "node_modules"), None)
    if node_modules is None:
        pytest.fail("native checks require the real npm-installed summarize package and its node_modules/.bin")
    bins = node_modules / ".bin"
    monkeypatch.setenv("PATH", os.pathsep.join((str(Path(node).parent), str(bins), os.environ.get("PATH", ""))))
    if shutil.which("summarize", path=str(bins)) is None:
        pytest.fail(f"the actual npm summarize entry is missing from {bins}")
    return node, cli.as_posix()


def native_client(tmp_path, monkeypatch, adapter, executable):
    from backend.app import main
    from backend.app.model_config import ModelConfigurationStore
    from backend.app.role_store import RoleStore
    from backend.app.session_manager import SessionManager
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases

    roles = RoleStore(tmp_path / "roles")
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    for name, value in {
        "store": roles, "roles_root": tmp_path / "roles", "session_store": sessions,
        "session_manager": SessionManager(roles, sessions), "model_adapter": adapter,
        "model_configuration_store": ModelConfigurationStore(tmp_path / "data" / "models.json"),
        "role_locks": {}, "project_root": tmp_path / "project",
    }.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    response = client.post("/api/roles", json={
        "name": "真实 CLI 验收角色", "profile": {"profile": "依据实际工具来源回答。"},
        "agentConfig": {"shell": {"enabled": True, "allowedCommands": [executable],
                                    "timeoutSeconds": 45, "maxOutputChars": 30000}},
    })
    assert response.status_code == 201, response.text
    return client, response.json()["role"]["id"], roles, sessions


def latest_tool_output(messages):
    output = next(item["content"] for item in reversed(messages) if item["role"] == "tool")
    assert isinstance(output, str)
    assert not output.startswith(("[failed]", "[timed_out]", "[denied]", "[output truncated")), output
    return output


def assert_builtin_was_selected(sessions, role_id, skill_id):
    run_id = sessions.list_messages(f"role:{role_id}")[-1].runId
    skills = sessions.get_run(run_id).modelSnapshot["capabilities"]["skills"]
    assert [(item["id"], item["source"]) for item in skills] == [(skill_id, "builtin")]


@contextmanager
def local_summary_provider(home, source_marker):
    received = []
    errors = []
    summary = "LOCAL_PROVIDER_VERIFIED: The compass measured 23 degrees northeast at dawn."

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.connection.settimeout(3)
            try:
                length = int(self.headers.get("content-length", "0"))
                assert 0 < length < 200000, "unbounded provider request"
                body = json.loads(self.rfile.read(length))
                assert self.path == "/v1/chat/completions"
                assert self.headers.get("Authorization") == "Bearer meido-native-local-only"
                assert source_marker in json.dumps(body), "CLI did not submit actual file content"
                received.append(body)  # Never retain headers or credential values.
                completion = {"id": "native-local-result", "object": "chat.completion", "created": int(time.time()),
                    "model": "gpt-4o-mini", "choices": [{"index": 0, "message": {"role": "assistant", "content": summary}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
                if body.get("stream"):
                    chunk = {"id": completion["id"], "object": "chat.completion.chunk", "created": completion["created"],
                             "model": completion["model"], "choices": [{"index": 0, "delta": {"role": "assistant", "content": summary}, "finish_reason": None}]}
                    final = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": completion["usage"]}
                    payload = ("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(final) + "\n\ndata: [DONE]\n\n").encode()
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(completion).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as error:
                errors.append(str(error))
                self.send_error(400, "invalid local test provider request")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    try:
        config = home / ".summarize" / "config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"env": {
            "OPENAI_API_KEY": "meido-native-local-only",  # Deliberately invalid outside this local fixture.
            "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "OPENAI_USE_CHAT_COMPLETIONS": "1",
        }, "cache": {"enabled": False}}), encoding="utf-8")
        yield received, summary
        assert not errors, errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "local test provider did not stop"


def test_typed_weather_skill_runs_real_curl_and_returns_both_public_sources(
    tmp_path, monkeypatch, native_environment,
):
    from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall

    curl = require_program("curl")
    common = [curl, "--fail", "--silent", "--show-error", "--connect-timeout", "8",
              "--max-time", "20", "--max-filesize", "8192"]
    wttr = shlex.join([*common, "https://wttr.in/Shanghai?format=3"])
    backup = shlex.join([*common, "https://api.open-meteo.com/v1/forecast?latitude=31.23&longitude=121.47&current=temperature_2m,weather_code&timezone=Asia%2FShanghai"])

    class Adapter:
        calls = 0
        primary = ""
        fallback = None

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            assert "shell" in {item["function"]["name"] for item in tools}
            if self.calls == 1:
                assert "天气查询" in json.dumps([item for item in messages if item["role"] == "system"], ensure_ascii=False)
                yield ModelToolCall("native-wttr", "shell", {"command": wttr})
                yield ModelResponseDone("tool_use")
            elif self.calls == 2:
                self.primary = latest_tool_output(messages)
                assert "Shanghai" in self.primary and re.search(r"[+-]?\d+(?:\.\d+)?°C", self.primary), self.primary
                yield ModelToolCall("native-open-meteo", "shell", {"command": backup})
                yield ModelResponseDone("tool_use")
            else:
                assert self.calls == 3
                self.fallback = json.loads(latest_tool_output(messages))
                current = self.fallback["current"]
                assert isinstance(current["temperature_2m"], (float, int))
                assert isinstance(current["weather_code"], int)
                assert current["time"] and self.fallback["timezone"] == "Asia/Shanghai"
                yield ModelTextDelta(f"NATIVE_WEATHER_VERIFIED wttr.in {self.primary.strip()}; Open-Meteo {current['time']} {current['temperature_2m']}°C")

    adapter = Adapter()
    client, role_id, _, sessions = native_client(tmp_path, monkeypatch, adapter, curl)
    try:
        response = client.post(f"/api/roles/{role_id}/messages", json={
            "content": "$weather 查询上海（已确认经纬度31.23,121.47），并核验备用天气来源。",
        })
    finally:
        client.close()
    assert response.status_code == 200, response.text
    assert "NATIVE_WEATHER_VERIFIED" in response.text, response.text
    assert adapter.calls == 3 and adapter.fallback is not None
    assert_builtin_was_selected(sessions, role_id, "weather")


def test_typed_summarize_skill_reads_real_workspace_file_and_returns_native_cli_summary(
    tmp_path, monkeypatch, native_environment, summarize_installation,
):
    from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall

    node, cli = summarize_installation
    source_marker = "MEIDO_NATIVE_SUMMARIZE_FILE_SOURCE_23_DEGREES"
    command = shlex.join([node, cli, "./notes.txt", "--force-summary", "--model", "openai/gpt-4o-mini",
                          "--json", "--timeout", "10s", "--retries", "0", "--firecrawl", "off", "--no-cache"])
    with local_summary_provider(native_environment, source_marker) as (received, expected_summary):
        class Adapter:
            calls = 0

            async def stream_structured_messages(self, messages, configuration, tools):
                self.calls += 1
                assert "shell" in {item["function"]["name"] for item in tools}
                if self.calls == 1:
                    assert "摘要与正文提取" in json.dumps([item for item in messages if item["role"] == "system"], ensure_ascii=False)
                    yield ModelToolCall("native-file-summary", "shell", {"command": command})
                    yield ModelResponseDone("tool_use")
                else:
                    assert self.calls == 2
                    result, _ = json.JSONDecoder().raw_decode(latest_tool_output(messages).lstrip())
                    assert source_marker in result["prompt"]
                    assert result["summary"] == expected_summary
                    assert result["llm"] is not None
                    assert len(received) == 1 and source_marker in json.dumps(received[0])
                    yield ModelTextDelta("NATIVE_SUMMARY_VERIFIED " + result["summary"])

        adapter = Adapter()
        client, role_id, roles, sessions = native_client(tmp_path, monkeypatch, adapter, node)
        (roles.workspace_path(role_id) / "notes.txt").write_text(
            source_marker + "\nThe observatory tested a brass compass at dawn. The reading was 23 degrees northeast.\n",
            encoding="utf-8",
        )
        try:
            response = client.post(f"/api/roles/{role_id}/messages", json={
                "content": "$summarize 摘要当前 workspace 中的 notes.txt。",
            })
        finally:
            client.close()
        assert response.status_code == 200, response.text
        assert "NATIVE_SUMMARY_VERIFIED " + expected_summary in response.text, response.text
        assert adapter.calls == 2 and len(received) == 1
        assert_builtin_was_selected(sessions, role_id, "summarize")


def test_real_summarize_cli_extracts_public_url_without_model_credentials(
    tmp_path, native_environment, summarize_installation,
):
    from backend.app.agent_runtime import CancellationToken, ToolContext
    from backend.app.agent_runtime.shell_tool import ShellTool

    node, cli = summarize_installation
    command = shlex.join([node, cli, "https://example.com", "--extract", "--json", "--timeout", "20s",
                          "--firecrawl", "off", "--max-extract-characters", "8192"])
    result = asyncio.run(ShellTool(tmp_path, allowed_commands=[node], timeout_seconds=45, max_output_chars=20000).execute(
        {"command": command}, ToolContext("native-url-role", "role:native-url-role", "native-url-run", CancellationToken()),
    ))
    assert not result.is_error, result.content
    assert result.details["status"] == "succeeded" and result.details["exitCode"] == 0
    assert not result.details["truncated"]
    extracted, _ = json.JSONDecoder().raw_decode(result.content.lstrip())
    assert extracted["input"]["kind"] == "url"
    assert extracted["extracted"]["title"] == "Example Domain"
    assert extracted["extracted"]["url"].rstrip("/") == "https://example.com"
    assert "documentation examples" in extracted["extracted"]["content"]
    assert extracted["llm"] is None
    assert not any(extracted["env"].values()), "isolated extraction should not discover provider credentials"
