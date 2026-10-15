"""Owned independent agent executions; Shiori 4f266ae profile semantics."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import threading
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

from ..models import SpawnToolConfig
from ..task_runtime import TaskBudget, TaskOrigin, TaskOutcome, TaskRuntime, TaskSnapshot
from ..task_runtime.process import ProcessExecutor
from ..task_runtime.runtime import TaskExecution
from .capabilities import CapabilityResolution
from .events import AgentEndEvent, MessageEndEvent
from .file_tools import build_file_tools
from .loop import run_agent_loop
from .provider import AssistantDoneEvent, CancellationToken, ModelProvider, TextDeltaEvent, ToolCallEndEvent
from .shell_tool import ShellTool
from .tools import AgentTool, AgentToolResult, ToolContext, ToolDefinition, ToolRegistry, resolve_tool_result
from .types import AgentMessage, AssistantMessage, ToolResultMessage, UserMessage

SPAWN_TOOL_NAMES = frozenset({"spawn", "spawn_manage"})
_READ = frozenset({"read_file", "list_dir"})
_WEB = frozenset({"web_search", "web_fetch"})
_WRITE = frozenset({"write_file", "edit_file", "shell"})


def _error() -> AgentToolResult:
    return AgentToolResult("Task unavailable in this role and original source, or delegation is disabled.",
                           {"status": "denied"}, True)


def _result(snapshot: TaskSnapshot, tasks: TaskRuntime, *, background: bool = False) -> AgentToolResult:
    details = {**snapshot.details(), "job_id": snapshot.task_id, "background": background,
               "completion": tasks.completion(snapshot.task_id, snapshot.origin)}
    return AgentToolResult(snapshot.output or f"[{snapshot.status}] job_id={snapshot.task_id}", details,
                           snapshot.status not in {"queued", "running", "succeeded"})


class BoundedModelProvider:
    def __init__(self, provider: ModelProvider, config: SpawnToolConfig):
        self.provider, self.config, self.requests, self.output = provider, config, 0, 0
        self.calls = 0

    async def stream_response(self, **kwargs):
        self.requests += 1
        if self.requests > self.config.maxTurns:
            raise ValueError("child model request budget exhausted")
        streamed = 0
        call_ids: set[str] = set()
        async for event in self.provider.stream_response(**kwargs):
            if isinstance(event, TextDeltaEvent):
                streamed += len(event.delta)
                self.output += len(event.delta)
            elif isinstance(event, ToolCallEndEvent):
                call_ids.add(event.tool_call.id)
                self.calls += 1
                self.output += len(json.dumps(asdict(event.tool_call)))
            elif isinstance(event, AssistantDoneEvent):
                self.output += max(0, len(event.message.content) - streamed)
                self.output += len(json.dumps(event.message.metadata, default=str))
                for call in event.message.tool_calls:
                    if call.id not in call_ids:
                        self.calls += 1
                        self.output += len(json.dumps(asdict(call)))
            if self.calls > self.config.maxTurns * 4:
                raise ValueError("child tool call budget exhausted")
            if self.output > self.config.maxModelOutputChars:
                raise ValueError("child model output budget exhausted")
            yield event


class _ChildTool:
    def __init__(self, tool: AgentTool, definition: ToolDefinition, allowed: Callable[[Mapping[str, object]], bool]):
        self.tool, self.definition, self.allowed = tool, definition, allowed

    async def execute(self, arguments, context, on_update=None):
        if not self.allowed(arguments):
            return _error()
        return await resolve_tool_result(self.tool.execute(arguments, context, on_update))


class _ChildRead:
    def __init__(self, parent: AgentTool, task_tool: AgentTool, directory: Path):
        self.parent, self.task_tool, self.directory = parent, task_tool, directory
        self.definition = parent.definition

    async def execute(self, arguments, context, on_update=None):
        path = Path(str(arguments.get("path", "")))
        candidate = path if path.is_absolute() else self.directory / path
        own = candidate.resolve().is_relative_to(self.directory.resolve()) and candidate.exists()
        return await resolve_tool_result((self.task_tool if own else self.parent).execute(arguments, context, on_update))


class _ChildShell:
    """A foreground process owned by the child, with no auto promotion."""
    def __init__(self, parent: ShellTool, directory: Path, execution: TaskExecution, owner: SpawnExecutor):
        self.parent, self.directory, self.execution, self.owner = parent, directory, execution, owner
        self.definition = replace(parent.definition, input_schema={
            "type": "object", "properties": {"command": {"type": "string", "minLength": 1},
                "description": {"type": "string", "maxLength": 500}}, "required": ["command"], "additionalProperties": False})

    async def execute(self, arguments, context, on_update=None):
        del on_update
        command = str(arguments["command"])
        if context.signal.is_cancelled() or not ShellTool.command_is_allowed(command, self.parent.allowed_commands):
            return _error()
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TMP", "TEMP"}}
        process = ProcessExecutor(shlex.split(command), self.directory, env)
        self.owner.process = process
        self.owner.shell_arguments = {"command": command}
        try:
            if context.signal.is_cancelled():
                process.cancel()
            running = asyncio.create_task(asyncio.to_thread(process.run, self.execution))
            try:
                outcome = await asyncio.wait_for(asyncio.shield(running), timeout=self.parent.timeout_seconds)
            except TimeoutError:
                process.cancel()
                await asyncio.shield(running)
                return AgentToolResult("Child Shell exceeded the frozen parent command timeout.", {"status": "timed_out"}, True)
            except asyncio.CancelledError:
                process.cancel()
                await asyncio.shield(running)
                raise
            return AgentToolResult(outcome.output_tail or "", {"status": outcome.status, "exitCode": outcome.exit_code},
                                   outcome.status != "succeeded")
        finally:
            process.cancel()
            self.owner.process = None
            self.owner.shell_arguments = None


class SpawnExecutor:
    def __init__(self, *, task: str, profile: str, config: SpawnToolConfig, provider: ModelProvider,
                 frozen: CapabilityResolution, context: ToolContext, authorized: Callable[[], bool]):
        self.task, self.profile, self.config, self.provider = task, profile, config, provider
        self.frozen, self.context, self.authorized = frozen, context, authorized
        self.signal = CancellationToken()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.running: asyncio.Task | None = None
        self.process: ProcessExecutor | None = None
        self.shell_arguments: Mapping[str, object] | None = None
        self.guard = threading.Lock()

    def is_authorized(self) -> bool:
        if not self.authorized():
            return False
        permitted = _READ | (_WEB if self.profile != "scripting" else frozenset()) | (_WRITE if self.profile != "research" else frozenset())
        return all(self.frozen.authorization_denial(definition, self.context,
            self.shell_arguments if definition.name == "shell" else None) is None
            for declaration in self.frozen.snapshot.tools
            if declaration.name in permitted and declaration.source in {"builtin", "role"}
            if (definition := self.frozen.definition(declaration.name)) is not None)

    def cancel(self) -> None:
        self.signal.cancel()
        if self.process is not None:
            self.process.cancel()
        with self.guard:
            if self.loop is not None and self.running is not None and not self.loop.is_closed():
                try:
                    self.loop.call_soon_threadsafe(self.running.cancel)
                except RuntimeError:
                    # asyncio.run may close its loop between the check and
                    # this cross-thread handoff after execution has finished.
                    pass

    def run(self, execution: TaskExecution) -> TaskOutcome:
        try:
            return asyncio.run(self._run(execution))
        except asyncio.CancelledError:
            return TaskOutcome("cancelled")
        finally:
            with self.guard:
                self.loop, self.running = None, None

    async def _run(self, execution: TaskExecution) -> TaskOutcome:
        with self.guard:
            self.loop, self.running = asyncio.get_running_loop(), asyncio.current_task()
        if self.signal.is_cancelled():
            return TaskOutcome("cancelled")
        directory = execution.directory()
        permitted = _READ | (_WEB if self.profile != "scripting" else frozenset()) | (_WRITE if self.profile != "research" else frozenset())
        task_files = {tool.definition.name: tool for tool in build_file_tools(self.context.role_id, directory)}
        tools: list[AgentTool] = []
        for declaration in self.frozen.snapshot.tools:
            name = declaration.name
            parent = self.frozen.get(name)
            if name not in permitted or parent is None or declaration.source not in {"builtin", "role"}:
                continue
            definition = self.frozen.definition(name)
            if definition is None:
                continue
            child = parent
            if name in _READ:
                child = _ChildRead(parent, task_files[name], directory)
            elif name in {"write_file", "edit_file"}:
                child = task_files[name]
            elif name == "shell" and isinstance(parent, ShellTool):
                child = _ChildShell(parent, directory, execution, self)
                definition = child.definition

            def allowed(arguments, declaration=definition):
                return self.authorized() and self.frozen.authorization_denial(declaration, self.context, arguments) is None

            tools.append(_ChildTool(child, definition, allowed))
        transcript: list[AgentMessage] = [UserMessage(self.task)]
        system = (f"[独立子任务 / independent delegated task]\nTask ID: {execution.task_id}\nProfile: {self.profile}\n"
                  f"Task directory: {directory}\nOnly write outputs in this task directory. "
                  "You have no parent conversation or owner identity. Return a bounded result. "
                  "No recursive delegation, memory/todo updates, channel sends or MCP tools. "
                  "Shell uses an argv allowlist; it is not an operating system or network sandbox.")
        outcome = TaskOutcome("failed")
        async for event in run_agent_loop(provider=BoundedModelProvider(self.provider, self.config), model="frozen-parent-model",
                system=system, messages=transcript, tools=tools, max_turns=self.config.maxTurns, signal=self.signal,
                session_id=execution.task_id, role_id=self.context.role_id, session_key=self.context.session_key,
                run_id=execution.task_id, provider_timeout=min(60, self.config.timeoutSeconds), tool_timeout=min(60, self.config.timeoutSeconds)):
            if isinstance(event, MessageEndEvent):
                message = event.message
                if isinstance(message, (AssistantMessage, ToolResultMessage)):
                    execution.write(json.dumps({"role": message.role, "content": message.content[:self.config.maxOutputChars],
                        "toolCalls": [asdict(call) for call in message.tool_calls] if isinstance(message, AssistantMessage) else [],
                        "toolName": message.tool_name if isinstance(message, ToolResultMessage) else None,
                        "toolCallId": message.tool_call_id if isinstance(message, ToolResultMessage) else None}, ensure_ascii=False) + "\n")
            if isinstance(event, AgentEndEvent):
                final = next((item.content for item in reversed(transcript) if isinstance(item, AssistantMessage)), "")
                outcome = TaskOutcome("succeeded" if event.reason == "completed" else "cancelled" if event.reason == "cancelled" else "failed",
                                      output_tail=final or event.error or event.reason)
        return outcome


class SpawnTool:
    definition = ToolDefinition(name="spawn", description="Delegate a bounded independent task. Profiles: research (read file/list/web), scripting (read plus task-directory writes and allowlisted Shell), general (union). Child receives only tools currently authorized to this parent. Synchronous by default; explicit background completion returns to this source. retry_count records a deliberate retry, never automatic replay.",
        input_schema={"type": "object", "properties": {
            "task": {"type": "string", "minLength": 1, "maxLength": 20000},
            "label": {"type": "string", "maxLength": 100},
            "profile": {"type": "string", "enum": ["research", "scripting", "general"]},
            "run_in_background": {"type": "boolean"},
            "retry_count": {"type": "integer", "minimum": 0, "maximum": 1}},
            "required": ["task"], "additionalProperties": False}, risk="external", source="role")

    def __init__(self, tasks: TaskRuntime, config: SpawnToolConfig, *, origin_resolver: Callable[[ToolContext], TaskOrigin],
                 authorized: Callable[[ToolContext], bool]):
        self.tasks, self.config, self.origin_resolver, self.authorized = tasks, deepcopy(config), origin_resolver, authorized
        self.frozen: CapabilityResolution | None = None
        self.provider_factory: Callable[[], ModelProvider] | None = None
        self.definition = replace(self.definition, timeout_seconds=config.timeoutSeconds + 5, output_limit=config.maxOutputChars)

    def bind(self, capabilities: CapabilityResolution, provider_factory: Callable[[], ModelProvider]) -> None:
        self.frozen, self.provider_factory = capabilities, provider_factory

    async def execute(self, arguments, context, on_update=None):
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if not self.authorized(context) or context.signal.is_cancelled() or self.frozen is None or self.provider_factory is None:
                return _error()
            frozen = self.frozen.current()
            if (context.role_id, context.session_key, context.run_id) != (frozen.snapshot.role_id, frozen.snapshot.session_key, frozen.snapshot.run_id):
                return _error()
            if context.capability_snapshot_id and frozen.snapshot.snapshot_id != context.capability_snapshot_id:
                return _error()
            origin = self.origin_resolver(context)
            background = arguments.get("run_in_background", False) is True
            executor = SpawnExecutor(task=arguments["task"], profile=arguments.get("profile", "research"), config=self.config,
                provider=self.provider_factory(), frozen=frozen, context=context, authorized=lambda: self.authorized(context))
            task_id = self.tasks.submit(origin, executor, budget=TaskBudget(self.config.timeoutSeconds, self.config.maxOutputChars),
                kind="spawn", description=json.dumps({"label": arguments.get("label", ""), "profile": arguments.get("profile", "research"),
                    "retry_count": arguments.get("retry_count", 0)}, ensure_ascii=False),
                authorized=executor.is_authorized, completion=background)
            if background:
                return _result(self.tasks.query(task_id, origin), self.tasks, background=True)
            try:
                while True:
                    if not self.authorized(context):
                        return _error()
                    if context.signal.is_cancelled():
                        await asyncio.to_thread(self.tasks.stop, task_id, origin)
                    snapshot = self.tasks.query(task_id, origin)
                    if snapshot.ended_at is not None:
                        return _result(snapshot, self.tasks)
                    await asyncio.sleep(.01)
            except asyncio.CancelledError:
                await asyncio.shield(asyncio.to_thread(self.tasks.stop, task_id, origin))
                raise
        except (KeyError, ValueError, PermissionError):
            return _error()


class SpawnManageTool:
    definition = ToolDefinition(name="spawn_manage", description="List, get or cancel independent child tasks owned by this exact role and original source. Unknown sources never disclose task content or logs.",
        input_schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "get", "cancel"]},
            "job_id": {"type": "string", "minLength": 1}}, "required": ["action"], "additionalProperties": False},
        risk="mutating", source="role", timeout_seconds=15)

    def __init__(self, tasks: TaskRuntime, *, origin_resolver: Callable[[ToolContext], TaskOrigin], authorized: Callable[[ToolContext], bool]):
        self.tasks, self.origin_resolver, self.authorized = tasks, origin_resolver, authorized

    async def execute(self, arguments, context, on_update=None):
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if not self.authorized(context):
                return _error()
            origin = self.origin_resolver(context)
            if arguments["action"] == "list":
                items = [item.details() for item in self.tasks.list(origin, kind="spawn")]
                return AgentToolResult(json.dumps(items, ensure_ascii=False), {"tasks": items})
            task_id = str(arguments.get("job_id", ""))
            snapshot = self.tasks.query(task_id, origin)
            if snapshot.kind != "spawn":
                return _error()
            if arguments["action"] == "cancel":
                snapshot = await asyncio.to_thread(self.tasks.stop, task_id, origin)
            return _result(snapshot, self.tasks)
        except (KeyError, ValueError, PermissionError):
            return _error()
