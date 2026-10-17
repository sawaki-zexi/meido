"""Hostile schema probes run inside an owned subprocess, never hang pytest itself."""
import asyncio
import os
from pathlib import Path
import signal
import sys

import pytest

from backend.app.browser_use.process import spawn_process_tree
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.mcp_stdio import schema_validator, validate_instance
from test_mcp_http import main, save_server, setup_host


@pytest.mark.parametrize('result', [
    'plain ASCII',
    '{"source":"本机RSS唯一源168","title":"中文RSS-168-唯一甲"}',
    '混合文字、emoji 🐈 与换行\nsecond line',
])
def test_schema_worker_validates_utf8_structured_results(result):
    # This is the feed_query output shape returned by the real RSS server.
    # Validation must not depend on the subprocess locale or code page.
    schema = {'type': 'object', 'properties': {'result': {'type': 'string', 'title': 'Result'}},
              'required': ['result'], 'additionalProperties': False, 'title': 'feed_queryOutput'}
    value = {'result': result}
    schema_validator(schema).validate(value)

    async def check():
        assert await validate_instance(schema, value)
        assert not await validate_instance(schema, {'result': 168})
        assert not await validate_instance(schema, {'other': result})
        assert not await validate_instance(schema, {'result': result, 'extra': True})

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["regex_input", "regex_output", "fanout_input"])
def test_hostile_schema_has_a_killable_budget_and_does_not_block_host(tmp_path, monkeypatch, mode):
    if os.environ.get("MEIDO_MCP_SCHEMA_PROBE") != mode:
        async def probe():
            env = dict(os.environ, MEIDO_MCP_SCHEMA_PROBE=mode)
            args = (sys.executable, "-m", "pytest", f"{__file__}::test_hostile_schema_has_a_killable_budget_and_does_not_block_host[{mode}]", "-q")
            owner = None
            if os.name == "nt":
                process, owner = await spawn_process_tree(*args, cwd=str(Path.cwd()), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            else:
                process = await asyncio.create_subprocess_exec(*args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), 15)
                assert process.returncode == 0, output.decode(errors="replace")
            finally:
                if owner is not None:
                    await asyncio.to_thread(owner.close)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await process.wait()
        asyncio.run(probe())
        return

    client, role, sessions, configs = setup_host(tmp_path, monkeypatch)
    fixture = Path(__file__).resolve().parents[1] / "fixtures/mcp_stdio_server.py"
    assert save_server(client, role, tmp_path, command=[sys.executable, str(fixture), str(tmp_path / "peer"), mode], timeoutSeconds=10).status_code == 201

    class Adapter:
        count = 0
        ticks = 0
        ticker = None

        async def tick(self):
            while True:
                await asyncio.sleep(.02)
                self.ticks += 1

        async def stream_structured_messages(self, messages, configuration, tools):
            self.count += 1
            if self.count == 1:
                self.ticker = asyncio.create_task(self.tick())
                yield ModelToolCall("load", "tool_search", {"query": "select:mcp_add"})
            elif self.count == 2:
                yield ModelToolCall("connect", "mcp_add", {"server_id": "notes"})
            elif self.count == 3:
                yield ModelToolCall("find", "tool_search", {"query": "select:mcp_notes__echo"})
            elif self.count == 4:
                self.ticks = 0
                yield ModelToolCall("hostile", "mcp_notes__echo", {"text": "a" * 32 + "!" if mode == "regex_input" else "evidence call"})
            else:
                assert "schema validation exceeded time budget" in messages[-1]["content"]
                assert self.ticks >= 5, "host loop and revocation watchdog must remain responsive"
                self.ticker.cancel()
                await asyncio.gather(self.ticker, return_exceptions=True)
                yield ModelTextDelta("恶意 schema 已有界停止")

    monkeypatch.setattr(main, "model_adapter", Adapter())
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "验证 schema 预算"})
    assert "恶意 schema 已有界停止" in response.text, response.text
    if mode != "regex_output":
        assert not (tmp_path / "peer/business.jsonl").exists()
