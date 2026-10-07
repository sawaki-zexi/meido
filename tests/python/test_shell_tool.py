import asyncio
import os
import sys
from pathlib import Path

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.agent_runtime.shell_tool import ShellTool


def _tool(tmp_path, **kwargs):
    executable = str(Path(sys.executable)).replace("\\", "/")
    return ShellTool(tmp_path, allowed_commands=[executable], **kwargs), executable


def _context(token=None):
    return ToolContext("role-1", "session-1", "run-1", token or CancellationToken())


def test_shell_tool_runs_allowlisted_command_in_fixed_workspace(tmp_path):
    tool, executable = _tool(tmp_path)
    result = asyncio.run(tool.execute({"command": f'{executable} -c "import os; print(os.getcwd())"'}, _context()))

    assert result.is_error is False
    assert str(tmp_path) in result.content
    assert result.details["exitCode"] == 0


@pytest.mark.skipif(
    os.name == "nt" or str(Path(sys.executable)) == str(Path(sys.executable)).lower(),
    reason="requires a case-sensitive executable path",
)
def test_absolute_allowlist_preserves_case_sensitive_path(tmp_path):
    executable = str(Path(sys.executable)).replace("\\", "/")
    tool = ShellTool(tmp_path, allowed_commands=[executable])

    result = asyncio.run(tool.execute({"command": f'{executable} -c "print(1)"'}, _context()))

    assert result.is_error is False
    assert result.content.strip() == "1"


def test_shell_tool_denies_unallowlisted_executable(tmp_path):
    tool = ShellTool(tmp_path, allowed_commands=[])
    result = asyncio.run(tool.execute({"command": "python -c \"print(1)\""}, _context()))

    assert result.is_error is True
    assert result.details["status"] == "denied"


def test_bare_allowlist_does_not_allow_absolute_executable_path(tmp_path):
    _, executable = _tool(tmp_path)
    tool = ShellTool(tmp_path, allowed_commands=[Path(executable).name])
    result = asyncio.run(tool.execute({"command": f'{executable} -c "print(1)"'}, _context()))

    assert result.is_error is True
    assert result.details["status"] == "denied"


def test_shell_tool_rejects_model_supplied_cwd(tmp_path):
    tool = ShellTool(tmp_path, allowed_commands=["python"])
    result = asyncio.run(tool.execute({"command": "python", "cwd": "/tmp"}, _context()))

    assert result.is_error is True
    assert "unknown arguments" in result.content


def test_shell_tool_bounds_output_and_preserves_metadata(tmp_path):
    tool, executable = _tool(tmp_path, max_output_chars=20)
    code = "print('x' * 200)"
    result = asyncio.run(tool.execute({"command": f'{executable} -c "{code}"'}, _context()))

    assert result.is_error is False
    assert result.details["truncated"] is True
    assert result.details["outputChars"] > 20
    assert len(result.content) < 100


def test_shell_tool_timeout_returns_error_result(tmp_path):
    tool, executable = _tool(tmp_path, timeout_seconds=0.05)
    result = asyncio.run(tool.execute({"command": f'{executable} -c "import time; time.sleep(2)"'}, _context()))

    assert result.is_error is True
    assert result.details["status"] == "timed_out"


def test_shell_tool_does_not_forward_model_api_key_to_child(tmp_path, monkeypatch):
    monkeypatch.setenv("MEIDO_API_KEY", "must-not-leak")
    tool, executable = _tool(tmp_path)
    result = asyncio.run(tool.execute({
        "command": f'{executable} -c "import os; print(os.getenv(\'MEIDO_API_KEY\', \'missing\'))"',
    }, _context()))

    assert result.is_error is False
    assert "must-not-leak" not in result.content
    assert "missing" in result.content


def test_shell_tool_cancellation_terminates_process(tmp_path):
    tool, executable = _tool(tmp_path, timeout_seconds=10)
    token = CancellationToken()

    async def run():
        task = asyncio.create_task(tool.execute({"command": f'{executable} -c "import time; time.sleep(10)"'}, _context(token)))
        await asyncio.sleep(0.05)
        token.cancel()
        return await task

    result = asyncio.run(run())
    assert result.is_error is True
    assert result.details["status"] == "cancelled"
