import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from backend.app.mcp_config import McpConfigStore, McpServerInput
from backend.app.mcp_stdio import McpStdioClient, McpProtocolError
from test_mcp_http import process_alive


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_server.py"


class Credentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


def configure(tmp_path, mode="normal", **changes):
    credentials = Credentials()
    store = McpConfigStore(tmp_path / "mcp.db", credentials)
    values = {"serverId": "notes", "name": "本地笔记", "command": [sys.executable, str(FIXTURE), str(tmp_path / "peer"), mode], "cwd": str(tmp_path), "env": {"MCP_TEST_SECRET": "vault-sensitive-token"}, "roleIds": ["r"], "risk": "external", "timeoutSeconds": 3}
    values.update(changes)
    config = store.save(McpServerInput(**values))
    return store, config


def test_configuration_is_durable_but_secrets_and_processes_are_not_in_catalog(tmp_path):
    store, config = configure(tmp_path)
    assert not (tmp_path / "peer/pid").exists()
    public = store.list_public()
    assert public[0]["envKeys"] == ["MCP_TEST_SECRET"]
    assert "vault-sensitive-token" not in json.dumps(public)
    assert b"vault-sensitive-token" not in (tmp_path / "mcp.db").read_bytes()
    reopened = McpConfigStore(tmp_path / "mcp.db", store.credentials)
    assert reopened.get("notes") == config
    assert reopened.environment(config)["MCP_TEST_SECRET"] == "vault-sensitive-token"


def test_real_stdio_handshake_pagination_call_and_minimal_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MEIDO_UNRELATED_SECRET", "must-not-inherit")
    store, config = configure(tmp_path, "environment")

    async def run():
        client = McpStdioClient(config, store.environment(config))
        try:
            tools = await client.connect()
            assert [item["name"] for item in tools] == ["echo", "slow"]
            result = await client.call("echo", {"text": "evidence actual"})
            assert "MEIDO_UNRELATED_SECRET" not in result["structuredContent"]["keys"]
            assert "MCP_TEST_SECRET" in result["structuredContent"]["keys"]
            assert Path(result["structuredContent"]["cwd"]) == tmp_path
            assert "vault-sensitive-token" in result["content"][0]["text"]
        finally:
            await client.close()
        assert client.closed

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["normal", "child_without_pipes"])
def test_cancelling_real_stdio_repeatedly_finishes_entire_process_tree_disposal(tmp_path, mode):
    store, config = configure(tmp_path, mode, timeoutSeconds=60)

    async def run():
        client = McpStdioClient(config, store.environment(config))
        task = None
        try:
            await client.connect()
            task = asyncio.create_task(client.call("slow", {}))
            async with asyncio.timeout(5):
                while not (tmp_path / "peer/child_pid").exists():
                    await asyncio.sleep(.01)
            parent = int((tmp_path / "peer/pid").read_text())
            child = int((tmp_path / "peer/child_pid").read_text())
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            assert not process_alive(parent) and not process_alive(child)
            assert not (tmp_path / "peer/effect.txt").exists()
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("interrupt", ["timeout", "cancel", "cancel_repeated"])
def test_unread_stdin_write_is_bounded_and_cancellation_cannot_block_tree_cleanup(tmp_path, interrupt):
    store, config = configure(tmp_path, "blocked_stdin", timeoutSeconds=1 if interrupt == "timeout" else 60)

    async def run():
        client = McpStdioClient(config, store.environment(config))
        task = None
        try:
            await client.connect()
            async with asyncio.timeout(5):
                while not (tmp_path / "peer/not_reading").exists():
                    await asyncio.sleep(.01)
            parent = int((tmp_path / "peer/pid").read_text())
            child = int((tmp_path / "peer/child_pid").read_text())
            task = asyncio.create_task(client.call("echo", {"text": "evidence " + "笔" * 60000}))
            await asyncio.sleep(.1)
            if interrupt != "timeout":
                assert task.cancel()
                if interrupt == "cancel_repeated":
                    await asyncio.sleep(.01)
                    assert task.cancel()
                    await asyncio.sleep(.01)
                    assert task.cancel()
            done, _ = await asyncio.wait({task}, timeout=2)
            assert task in done, "request writes and best-effort cancellation must have a strict budget"
            assert client.closed
            assert not process_alive(parent) and not process_alive(child)
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            assert isinstance(result, asyncio.TimeoutError if interrupt == "timeout" else asyncio.CancelledError)
            assert not (tmp_path / "peer/business.jsonl").exists()
        finally:
            # Explicit close also keeps the pre-fix red probe bounded.
            await client.close()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    events = [json.loads(line) for line in (tmp_path / "peer/events.jsonl").read_text().splitlines()]
    assert [item["method"] for item in events][:4] == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    assert events[3]["params"] == {"cursor": "page-2"}


@pytest.mark.parametrize("mode", ["malformed", "oversize", "duplicate", "cursor_loop", "remote_ref", "hang_init", "wrong_version", "bad_schema", "deep_schema", "many_tools", "large_catalog", "many_pages", "long_name"])
def test_real_stdio_rejects_invalid_or_unbounded_peer_and_cleans_owned_process(tmp_path, mode):
    store, config = configure(tmp_path, mode, timeoutSeconds=1, serverId="a" * 24 if mode == "long_name" else "notes")

    async def run():
        client = McpStdioClient(config, store.environment(config))
        with pytest.raises(McpProtocolError):
            await client.connect()
        assert client.closed
        pid_file = tmp_path / "peer/pid"
        if pid_file.exists():
            assert not process_alive(int(pid_file.read_text()))

    asyncio.run(run())
