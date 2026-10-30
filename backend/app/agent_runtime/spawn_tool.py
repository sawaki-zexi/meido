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
from .capabilities import CapabilityRegistry, CapabilityResolution
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


async def _finish_owned_cleanup(pending):
    """Repeated cancellation cannot detach a task's resource release."""
    cleanup = asyncio.ensure_future(pending)
    interrupted = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as error:
            interrupted = error
    result = cleanup.result()
    if interrupted is not None:
        raise interrupted
    return result


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
    def __init__(self, tool: AgentTool, definition: ToolDefinition, allowed: Callable[[Mapping[str, object]], bool], current_tool=None):
        self.tool, self._definition, self.allowed = tool, definition, allowed
        self.current_tool = current_tool

    @property
    def definition(self):
        tool = self.current_tool() if self.current_tool is not None else self.tool
        return tool.definition if tool is not None else self._definition

    @definition.setter
    def definition(self, definition: ToolDefinition):
        self._definition = definition

    async def execute(self, arguments, context, on_update=None):
        if not self.allowed(arguments):
            return _error()
        tool = self.current_tool() if self.current_tool is not None else self.tool
        if tool is None:
            return _error()
        ToolRegistry.validate_definition(tool.definition, arguments)
        return await resolve_tool_result(tool.execute(arguments, context, on_update))


class _ChildRead:
    def __init__(self, parent: AgentTool, task_tool: AgentTool, directory: Path, current_parent=None):
        self.parent, self.task_tool, self.directory = parent, task_tool, directory
        self.current_parent = current_parent
        self._definition = parent.definition

    @property
    def definition(self):
        parent = self.current_parent() if self.current_parent is not None else self.parent
        return parent.definition if parent is not None else self._definition

    @definition.setter
    def definition(self, definition: ToolDefinition):
        self._definition = definition

    async def execute(self, arguments, context, on_update=None):
        path = Path(str(arguments.get("path", "")))
        candidate = path if path.is_absolute() else self.directory / path
        own = candidate.resolve().is_relative_to(self.directory.resolve()) and candidate.exists()
        parent = self.current_parent() if self.current_parent is not None else self.parent
        tool = self.task_tool if own else parent
        if tool is None:
            return _error()
        ToolRegistry.validate_definition(tool.definition, arguments)
        return await resolve_tool_result(tool.execute(arguments, context, on_update))


class _ChildShell:
    """A foreground process owned by the child, with no auto promotion."""
    def __init__(self, parent: ShellTool, directory: Path, execution: TaskExecution, owner: SpawnExecutor):
        self.parent, self.directory, self.execution, self.owner = parent, directory, execution, owner
        self.definition = replace(parent.definition, description='Run an explicitly allowlisted argv command in the isolated task directory; shell expressions are not supported.', input_schema={
            "type": "object", "properties": {"command": {"type": "string", "minLength": 1},
                "description": {"type": "string", "maxLength": 500}}, "required": ["command"], "additionalProperties": False})

    async def execute(self, arguments, context, on_update=None):
        del on_update
        command = str(arguments["command"])
        current = self.owner.capabilities.get("shell")
        if not isinstance(current, ShellTool):
            return _error()
        self.parent = current
        if context.signal.is_cancelled() or not self.parent.allows_restricted(self.owner.context, command):
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
                await _finish_owned_cleanup(running)
                return AgentToolResult("Child Shell exceeded the current command timeout.", {"status": "timed_out"}, True)
            except asyncio.CancelledError:
                process.cancel()
                await _finish_owned_cleanup(running)
                raise
            return AgentToolResult(outcome.output_tail or "", {"status": outcome.status, "exitCode": outcome.exit_code},
                                   outcome.status != "succeeded")
        finally:
            process.cancel()
            self.owner.process = None
            self.owner.shell_arguments = None


class SpawnExecutor:
    def __init__(self, *, task: str, profile: str, config: SpawnToolConfig, provider: ModelProvider,
                 capabilities: CapabilityResolution, context: ToolContext, authorized: Callable[[], bool]):
        self.task, self.profile, self.config, self.provider = task, profile, config, provider
        permitted = _READ | (_WEB if profile != "scripting" else frozenset()) | (_WRITE if profile != "research" else frozenset())
        self.capabilities = capabilities.selected_subset({item.name for item in capabilities.current().snapshot.tools
            if item.name in permitted and item.source in {"builtin", "role"}})
        self.context, self.authorized = replace(context, capability_binding=None), authorized
        self.signal = CancellationToken()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.running: asyncio.Task | None = None
        self.process: ProcessExecutor | None = None
        self.shell_arguments: Mapping[str, object] | None = None
        self.guard = threading.Lock()
        self._cancel_requested = False
        shell = self.capabilities.get('shell')
        self.restricted_shell_allowed = (profile != 'research' and isinstance(shell, ShellTool)
                                         and shell.allows_restricted(context))

    def is_authorized(self) -> bool:
        if not self.authorized():
            return False
        command = self.shell_arguments.get('command') if self.shell_arguments else None
        if self.restricted_shell_allowed:
            shell = self.capabilities.get('shell')
            if not isinstance(shell, ShellTool) or not shell.allows_restricted(
                    self.context, str(command) if command is not None else None):
                return False
        permitted = _READ | (_WEB if self.profile != "scripting" else frozenset()) | (_WRITE if self.profile != "research" else frozenset())
        return all(self.capabilities.authorization_denial(definition, self.context,
            self.shell_arguments if definition.name == "shell" else None) is None
            for declaration in self.capabilities.snapshot.tools
            if declaration.name in permitted and declaration.source in {"builtin", "role"}
            if (definition := self.capabilities.definition(declaration.name)) is not None)

    def cancel(self) -> None:
        with self.guard:
            if self._cancel_requested:
                return
            self._cancel_requested = True
            self.signal.cancel()
            process = self.process
            if self.loop is not None and self.running is not None and not self.loop.is_closed():
                try:
                    self.loop.call_soon_threadsafe(self.running.cancel)
                except RuntimeError:
                    # asyncio.run may close its loop between the check and
                    # this cross-thread handoff after execution has finished.
                    pass
        if process is not None:
            process.cancel()

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
        for declaration in self.capabilities.snapshot.tools:
            name = declaration.name
            parent = self.capabilities.get(name)
            if name not in permitted or parent is None or declaration.source not in {"builtin", "role"}:
                continue
            definition = self.capabilities.definition(name)
            if definition is None:
                continue
            child = parent
            if name in _READ:
                child = _ChildRead(parent, task_files[name], directory, lambda name=name: self.capabilities.get(name))
            elif name in {"write_file", "edit_file"}:
                child = task_files[name]
            elif name == "shell" and isinstance(parent, ShellTool):
                if not self.restricted_shell_allowed:
                    continue
                child = _ChildShell(parent, directory, execution, self)
                definition = child.definition

            def allowed(arguments, name=name):
                current = self.capabilities.definition(name)
                return (self.authorized() and current is not None and current.source in {"builtin", "role"}
                        and self.capabilities.authorization_denial(current, self.context, arguments) is None)

            tools.append(_ChildTool(child, definition, allowed,
                (lambda name=name: self.capabilities.get(name)) if child is parent else None))
        def current_tool_allowed(definition, context):
            current = self.capabilities.definition(definition.name)
            return (self.authorized() and current is not None and current.source in {'builtin', 'role'}
                    and self.capabilities.authorization_denial(current, self.context) is None)
        child_capabilities = CapabilityRegistry(tools).resolve(role_id=self.context.role_id,
            session_key=self.context.session_key, run_id=execution.task_id, authorize_tool=current_tool_allowed)
        transcript: list[AgentMessage] = [UserMessage(self.task)]
        system = (f"[独立子任务 / independent delegated task]\nTask ID: {execution.task_id}\nProfile: {self.profile}\n"
                  f"Task directory: {directory}\nOnly write outputs in this task directory. "
                  "You have no parent conversation or owner identity. Return a bounded result. "
                  "No recursive delegation, memory/todo updates, channel sends or MCP tools. "
                  "Shell uses an argv allowlist; it is not an operating system or network sandbox.")
        outcome = TaskOutcome("failed")
        async for event in run_agent_loop(provider=BoundedModelProvider(self.provider, self.config), model="frozen-parent-model",
                system=system, messages=transcript, capabilities=child_capabilities, max_turns=self.config.maxTurns, signal=self.signal,
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
                outcome = TaskOutcome("succeeded" if event.reason == "completed" else "cancelled" if event.reason == "cancelled"
                                      else "timed_out" if event.failure_kind == "provider_timeout" else "failed",
                                      output_tail=final or event.error or event.reason)
        return outcome


class SpawnTool:
    definition = ToolDefinition(name="spawn", description="Delegate a bounded independent task only when it needs at least 4 tool steps, needs no intermediate user confirmation, and produces a report, file or conclusion. Do not delegate 1–3 calls, direct answers, session-memory changes or immediate send/tell/execute actions. Profiles: research (read file/list/web), scripting (task-directory writes and allowlisted Shell), general (union), coding (owned Codex exec in a granted repository). Use synchronous mode by default; background is for independent long work expected over 60 seconds or 15 calls. Coding requires repository, explicit run_in_background=true and retry_count=0; no chat child provider or Shell. Results return only to the original source; unknown submissions are never automatically replayed.",
        input_schema={"type": "object", "properties": {
            "task": {"type": "string", "minLength": 1, "maxLength": 20000, "description": "完整交接：任务目标、关键约束、必要上下文（含已尝试内容）和期望输出格式；子任务没有看过当前会话"},
            "label": {"type": "string", "maxLength": 100},
            "profile": {"type": "string", "enum": ["research", "scripting", "general", "coding"], "description": "默认 research 只读；命令/写文件用 scripting；两者都需用 general；coding 须有单独仓库许可"},
            "repository": {"type": "string", "minLength": 1, "maxLength": 2000},
            "run_in_background": {"type": "boolean", "description": "默认 false 同步等结果；独立长任务可 true，确认提交后等系统带回结果，不重复提交"},
            "retry_count": {"type": "integer", "minimum": 0, "maximum": 1}},
            "required": ["task"], "additionalProperties": False}, risk="external", source="role", search_hint='后台执行 子任务 多步调研 独立任务')

    def __init__(self, tasks: TaskRuntime, config: SpawnToolConfig, *, origin_resolver: Callable[[ToolContext], TaskOrigin],
                 authorized: Callable[[ToolContext], bool], coding_application=None, coding_access=None, coding_authorized=None):
        self.tasks, self.config, self.origin_resolver, self.authorized = tasks, deepcopy(config), origin_resolver, authorized
        self.coding_application, self.coding_access = coding_application, coding_access
        self.coding_authorized = coding_authorized or authorized
        self.capabilities: CapabilityResolution | None = None
        self.provider_factory: Callable[[], ModelProvider] | None = None
        self.definition = replace(self.definition, timeout_seconds=config.timeoutSeconds + 5, output_limit=config.maxOutputChars)

    def bind(self, capabilities: CapabilityResolution, provider_factory: Callable[[], ModelProvider]) -> None:
        self.capabilities, self.provider_factory = capabilities, provider_factory

    async def execute(self, arguments, context, on_update=None):
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if not self.authorized(context) or context.signal.is_cancelled() or self.capabilities is None or self.provider_factory is None:
                return _error()
            current = self.capabilities.current()
            if (context.role_id, context.session_key, context.run_id) != (current.snapshot.role_id, current.snapshot.session_key, current.snapshot.run_id):
                return _error()
            origin = self.origin_resolver(context)
            background = arguments.get("run_in_background", False) is True
            if arguments.get('profile') == 'coding':
                if (not background or arguments.get('retry_count') != 0 or not arguments.get('repository')
                        or self.coding_application is None or self.coding_access is None or not self.coding_authorized(context)):
                    return _error()
                from ..coding_tasks.tools import _owned_thread
                result = await _owned_thread(self.coding_application().start_task, origin,
                    repository=arguments['repository'], requirement=arguments['task'], executor='codex_exec',
                    admitted=lambda: not context.signal.is_cancelled() and self.authorized(context) and self.coding_authorized(context),
                    frozen_access=self.coding_access or None)
                details = {**result, 'job_id': result['id'], 'background': True}
                return AgentToolResult(json.dumps(details, ensure_ascii=False), details)
            if 'repository' in arguments:
                return _error()
            owned_context, authorized = replace(context, capability_binding=None), self.authorized
            executor = SpawnExecutor(task=arguments["task"], profile=arguments.get("profile", "research"), config=self.config,
                provider=self.provider_factory(), capabilities=current, context=owned_context, authorized=lambda: authorized(owned_context))
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
                await _finish_owned_cleanup(asyncio.to_thread(self.tasks.stop, task_id, origin))
                raise
        except (KeyError, ValueError, PermissionError):
            return _error()


class SpawnManageTool:
    definition = ToolDefinition(name="spawn_manage", description="List, get or cancel independent tasks owned by this exact role and original source. continue+task only appends an explicit new requirement to a registered Codex coding job in its exact native session; job_id stays stable. Ordinary child tasks cannot continue. Unknown sources never disclose task content or logs.",
        input_schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "get", "cancel", "continue"]},
            "task": {"type": "string", "minLength": 1, "maxLength": 20000},
            "job_id": {"type": "string", "minLength": 1}}, "required": ["action"], "additionalProperties": False},
        risk="external", source="role", timeout_seconds=15, search_hint='查看 取消 后台任务 subagent job_id spawn_manage')

    def __init__(self, tasks: TaskRuntime, *, origin_resolver: Callable[[ToolContext], TaskOrigin], authorized: Callable[[ToolContext], bool], coding_application=None, coding_access=None, coding_authorized=None):
        self.tasks, self.origin_resolver, self.authorized = tasks, origin_resolver, authorized
        self.coding_application, self.coding_access = coding_application, coding_access
        self.coding_authorized = coding_authorized or authorized

    async def execute(self, arguments, context, on_update=None):
        if arguments.get('action') == 'continue' and not getattr(self, 'new_execution_allowed', lambda: True)():
            return _error()
        del on_update
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if not self.authorized(context):
                return _error()
            origin = self.origin_resolver(context)
            action = arguments['action']
            if context.signal.is_cancelled() or ('task' in arguments) != (action == 'continue'):
                return _error()
            if arguments["action"] == "list":
                items = [item.details() for item in self.tasks.list(origin, kind="spawn")]
                if self.coding_application is not None and self.coding_access is not None and self.coding_authorized(context):
                    from ..coding_tasks.tools import _owned_thread
                    records = await _owned_thread(self.coding_application().list_tasks, origin, executor='codex_exec')
                    items += [{**item, 'job_id': item['id']} for item in records]
                return AgentToolResult(json.dumps(items, ensure_ascii=False), {"tasks": items})
            task_id = str(arguments.get("job_id", ""))
            if self.coding_application is not None and self.coding_access is not None and self.coding_authorized(context):
                from ..coding_tasks.tools import _owned_thread
                application = self.coding_application()
                try:
                    result = await _owned_thread(application.inspect, task_id, origin, executor='codex_exec')
                except KeyError:
                    result = None
                if result is not None:
                    if action == 'continue':
                        result = await _owned_thread(application.continue_task, task_id, origin,
                            requirement=arguments['task'], executor='codex_exec', frozen_access=self.coding_access or None,
                            admitted=lambda: not context.signal.is_cancelled() and self.authorized(context) and self.coding_authorized(context)
                                and getattr(self, 'new_execution_allowed', lambda: True)())
                    elif action == 'cancel':
                        result = await _owned_thread(application.cancel_task, task_id, origin, executor='codex_exec')
                    details = {**result, 'job_id': result['id'], 'background': True}
                    return AgentToolResult(json.dumps(details, ensure_ascii=False), details)
            if action == 'continue':
                return _error()
            snapshot = self.tasks.query(task_id, origin)
            if snapshot.kind != "spawn":
                return _error()
            if arguments["action"] == "cancel":
                snapshot = await asyncio.to_thread(self.tasks.stop, task_id, origin)
            return _result(snapshot, self.tasks)
        except (KeyError, ValueError, PermissionError):
            return _error()
