"""Host fixed read-only argv presets, owned by the original autonomous executor."""
import asyncio
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import shutil
import sys
import time

from ..agent_runtime.shell_tool import ShellTool
from ..mcp_stdio import finish_cleanup
from ..task_runtime.process import ProcessExecutor
from ..task_runtime.runtime import TaskExecution
from .source_validation import _read


@dataclass(frozen=True)
class Preset:
    identifier: str
    description: str
    argv: tuple[str, ...]
    executable_hash: str


def presets():
    result = {}
    for identifier, description, executable, arguments in (
        ('python-version', '只读核验宿主 Python 版本', sys.executable, ('--version',)),
        ('git-version', '只读核验宿主 Git 版本', shutil.which('git'), ('--version',)),
    ):
        if not executable:
            continue
        try:
            path = Path(executable).resolve(strict=True)
            digest = hashlib.sha256(_read(path, 64 * 1024 * 1024)).hexdigest()
            result[identifier] = Preset(identifier, description, (str(path), *arguments), digest)
        except (ValueError, OSError):
            continue
    return result


def available(preset, role):
    shell = role.get('agentConfig', {}).get('shell', {})
    return bool(shell.get('enabled') and ShellTool._executable_is_allowed(preset.argv[0], shell.get('allowedCommands', [])))


class _ExecutionView(TaskExecution):
    def __init__(self, turn):
        self.turn = turn
        self.task_id = turn.execution.task_id
        self.budget = replace(turn.execution.budget, output_chars=min(6000, turn.execution.budget.output_chars))
        self.deadline = min(turn.execution.deadline, time.monotonic() + 60)

    def write(self, text):
        if self.turn.signal.is_cancelled() or not self.turn.app._authorized(self.turn.origin):
            raise PermissionError('Drift Shell origin was sealed')
        self.turn.execution.write(text)

    def progress(self, text):
        self.turn.execution.progress(text)

    def directory(self):
        return self.turn.root


async def execute(turn, rule_id):
    preset = turn.shell_presets.get(rule_id)
    if (turn.skill is None or 'shell' not in turn.skill.tools or rule_id not in turn.manifest.shellRuleIds
            or rule_id not in turn.config.shellRuleIds or preset is None
            or not available(preset, turn.app.frozen_bindings(turn.origin).get('role', {}))):
        raise PermissionError('Drift Shell preset is outside the actual frozen grants')
    if hashlib.sha256(_read(Path(preset.argv[0]), 64 * 1024 * 1024)).hexdigest() != preset.executable_hash:
        raise PermissionError('Drift Shell executable changed')
    # Do not inherit secrets, Python module hooks or startup configuration.
    env = {name: os.environ[name] for name in ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'PATH') if name in os.environ}
    env.update({'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull})
    process = ProcessExecutor(list(preset.argv), turn.root, env)
    turn.processes.add(process)  # Responsibility exists before start.
    view = _ExecutionView(turn)
    pending = asyncio.create_task(asyncio.to_thread(process.run, view))
    turn.process_tasks.add(pending)
    try:
        async with asyncio.timeout(max(0, view.deadline - time.monotonic())):
            outcome = await asyncio.shield(pending)
        if not turn.app._authorized(turn.origin):
            raise PermissionError('Drift Shell origin was revoked')
        return {'status': outcome.status, 'exitCode': outcome.exit_code, 'output': outcome.output_tail or ''}
    finally:
        process.cancel()
        await finish_cleanup(pending)
        turn.process_tasks.discard(pending)
        turn.processes.discard(process)
