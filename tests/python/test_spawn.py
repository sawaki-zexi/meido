import asyncio
import shlex
import sys
import time

import pytest

from backend.app.agent_runtime import CapabilityRegistry, CancellationToken, ToolContext
from backend.app.agent_runtime.file_tools import build_file_tools
from backend.app.agent_runtime.provider import AssistantDoneEvent, TextDeltaEvent
from backend.app.agent_runtime.shell_tool import ShellTool
from backend.app.agent_runtime.spawn_tool import SpawnManageTool, SpawnTool
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage
from backend.app.models import SpawnToolConfig
from backend.app.task_runtime import TaskOrigin, TaskRuntime


async def delegate(tmp_path, provider, *, profile='research', config=None, authorize=lambda: True, names=None):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    tasks = TaskRuntime(tmp_path / 'tasks')
    context = ToolContext('role-a', 'role:role-a', 'parent-run', CancellationToken(), 'parent-call')
    tools = (*build_file_tools('role-a', workspace), ShellTool(workspace, allowed_commands=[sys.executable]))
    frozen = CapabilityRegistry(tools).resolve(role_id='role-a', session_key=context.session_key, run_id=context.run_id,
        enabled_tools=names or {'read_file', 'list_dir', 'write_file', 'edit_file', 'shell'}, allowed_risks={'read_only', 'mutating', 'external'})
    def origin(ctx):
        return TaskOrigin(ctx.role_id, ctx.session_key, ctx.run_id, ctx.tool_call_id)
    tool = SpawnTool(tasks, config or SpawnToolConfig(enabled=True), origin_resolver=origin, authorized=lambda ctx: authorize())
    tool.bind(frozen, lambda: provider)
    manager = SpawnManageTool(tasks, origin_resolver=origin, authorized=lambda ctx: authorize())
    result = await tool.execute({'task': 'complete a controlled task', 'profile': profile, 'run_in_background': True}, context)
    return tasks, context, tool, manager, result


async def completed(tasks, context, task_id):
    origin = TaskOrigin(context.role_id, context.session_key)
    deadline = time.monotonic() + 3
    while True:
        snapshot = tasks.query(task_id, origin)
        if snapshot.ended_at is not None:
            return snapshot
        assert time.monotonic() < deadline
        await asyncio.sleep(.01)


@pytest.mark.parametrize('profile,expected', [
    ('research', {'read_file', 'list_dir'}),
    ('scripting', {'read_file', 'list_dir', 'write_file', 'edit_file', 'shell'}),
    ('general', {'read_file', 'list_dir', 'write_file', 'edit_file', 'shell'}),
])
def test_profiles_execute_only_the_frozen_parent_intersection(tmp_path, profile, expected):
    class Provider:
        async def stream_response(self, *, tools, **kwargs):
            assert {item['function']['name'] for item in tools} == expected
            yield AssistantDoneEvent(AssistantMessage('bounded-answer'))
    async def run():
        tasks, context, _tool, _manage, result = await delegate(tmp_path, Provider(), profile=profile)
        try:
            snapshot = await completed(tasks, context, result.details['taskId'])
            assert snapshot.status == 'succeeded'
            assert snapshot.output == 'bounded-answer'
        finally:
            tasks.close()
    asyncio.run(run())


def test_scripting_writes_only_host_directory_and_shell_cannot_detach(tmp_path):
    class Provider:
        async def stream_response(self, *, messages, tools, **kwargs):
            results = [item for item in messages if isinstance(item, ToolResultMessage)]
            if not results:
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('write', 'write_file', {'path': 'artifact.txt', 'content': 'isolated-452'}),)))
            elif len(results) == 1:
                assert not results[-1].is_error
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('escape', 'write_file', {'path': '../escape.txt', 'content': 'forbidden'}),)))
            elif len(results) == 2:
                assert results[-1].is_error
                command = shlex.join([sys.executable.replace('\\', '/'), '-c', "from pathlib import Path; print(Path('artifact.txt').read_text())"])
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('shell', 'shell', {'command': command}),)))
            elif len(results) == 3:
                assert 'isolated-452' in results[-1].content
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('detach', 'shell', {'command': 'anything', 'run_in_background': True}),)))
            else:
                assert results[-1].is_error
                yield AssistantDoneEvent(AssistantMessage('scripting-complete'))
    async def run():
        tasks, context, _tool, _manage, result = await delegate(tmp_path, Provider(), profile='scripting')
        try:
            snapshot = await completed(tasks, context, result.details['taskId'])
            assert snapshot.status == 'succeeded'
            assert (tasks.root / 'workspaces' / snapshot.task_id / 'artifact.txt').read_text() == 'isolated-452'
            assert not (tmp_path / 'workspace' / 'artifact.txt').exists()
            assert not (tasks.root / 'workspaces' / 'escape.txt').exists()
            assert len(tasks.list(TaskOrigin(context.role_id, context.session_key), kind='shell')) == 0
        finally:
            tasks.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['cancel', 'revoked', 'timeout', 'shutdown'])
def test_running_child_cancellation_releases_model_and_has_real_terminal_state(tmp_path, mode):
    class Provider:
        cancelled = False
        async def stream_response(self, **kwargs):
            try:
                await asyncio.sleep(10)
                yield AssistantDoneEvent(AssistantMessage('too-late'))
            finally:
                self.cancelled = True
    async def run():
        allowed = True
        provider = Provider()
        config = SpawnToolConfig(enabled=True, timeoutSeconds=.15 if mode == 'timeout' else 5)
        tasks, context, _tool, manage, result = await delegate(tmp_path, provider, config=config, authorize=lambda: allowed)
        task_id = result.details['taskId']
        try:
            await asyncio.sleep(.05)
            if mode == 'revoked':
                allowed = False
            elif mode == 'shutdown':
                await asyncio.to_thread(tasks.close)
            elif mode == 'cancel':
                cancelled = await manage.execute({'action': 'cancel', 'job_id': task_id}, context)
                assert cancelled.details['status'] == 'cancelled'
                again = await manage.execute({'action': 'cancel', 'job_id': task_id}, context)
                assert again.details['status'] == 'cancelled'
            snapshot = await completed(tasks, context, task_id)
            assert snapshot.status == {'timeout': 'timed_out', 'shutdown': 'interrupted'}.get(mode, 'cancelled')
            assert provider.cancelled
            assert not snapshot.output.endswith('too-late')
        finally:
            tasks.close()
    asyncio.run(run())


@pytest.mark.parametrize('config,expected', [
    (SpawnToolConfig(enabled=True, maxTurns=1), 'max_turns'),
    (SpawnToolConfig(enabled=True, maxModelOutputChars=100), 'budget exhausted'),
])
def test_model_requests_and_cumulative_output_have_finite_budgets(tmp_path, config, expected):
    class Provider:
        async def stream_response(self, **kwargs):
            if config.maxTurns == 1:
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('list', 'list_dir', {'path': '.'}),)))
            else:
                yield TextDeltaEvent('x' * 101)
                yield AssistantDoneEvent(AssistantMessage('x' * 101))
    async def run():
        tasks, context, _tool, _manage, result = await delegate(tmp_path, Provider(), config=config)
        try:
            snapshot = await completed(tasks, context, result.details['taskId'])
            assert snapshot.status == 'failed'
            assert expected in snapshot.output
        finally:
            tasks.close()
    asyncio.run(run())


def test_management_seals_content_and_stop_from_other_roles_and_sessions(tmp_path):
    class Provider:
        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(AssistantMessage('private-child-evidence'))
    async def run():
        tasks, context, tool, manage, result = await delegate(tmp_path, Provider())
        task_id = result.details['taskId']
        try:
            await completed(tasks, context, task_id)
            own = await manage.execute({'action': 'list'}, context)
            assert own.details['tasks'][0]['taskId'] == task_id
            for role, session in [('other-role', context.session_key), (context.role_id, 'thread:other')]:
                alien = ToolContext(role, session, 'later', CancellationToken())
                for action in ['get', 'cancel']:
                    denied = await manage.execute({'action': action, 'job_id': task_id}, alien)
                    assert denied.is_error
                    assert 'private-child-evidence' not in denied.content
                    assert 'logPath' not in denied.details
                assert (await manage.execute({'action': 'list'}, alien)).details == {'tasks': []}
            for forged in [{'profile': 'admin'}, {'retry_count': 2}, {'role_id': 'other-role'}, {'task': ''}]:
                denied = await tool.execute({'task': 'test', **forged}, context)
                assert denied.is_error
        finally:
            tasks.close()
    asyncio.run(run())


def test_child_process_is_owned_by_cancel_and_never_becomes_a_shell_task(tmp_path):
    from test_background_shell import process_alive
    class Provider:
        async def stream_response(self, **kwargs):
            command = shlex.join([sys.executable.replace('\\', '/'), '-u', '-c',
                "import os,time; from pathlib import Path; Path('pid').write_text(str(os.getpid())); print('child-process-log'); time.sleep(20)"])
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('run', 'shell', {'command': command}),)))
    async def run():
        tasks, context, _tool, manage, result = await delegate(tmp_path, Provider(), profile='scripting')
        task_id = result.details['taskId']
        try:
            pid_file = tasks.root / 'workspaces' / task_id / 'pid'
            deadline = time.monotonic() + 3
            while not pid_file.exists() or not pid_file.read_text():
                assert time.monotonic() < deadline
                await asyncio.sleep(.01)
            pid = int(pid_file.read_text())
            assert process_alive(pid)
            result = await manage.execute({'action': 'cancel', 'job_id': task_id}, context)
            assert result.details['status'] == 'cancelled'
            assert not process_alive(pid)
            assert not tasks.list(TaskOrigin(context.role_id, context.session_key), kind='shell')
        finally:
            tasks.close()
    asyncio.run(run())


def test_foreground_parent_cancel_stops_child_instead_of_detaching_it(tmp_path):
    class Provider:
        entered = False
        stopped = False
        async def stream_response(self, **kwargs):
            self.entered = True
            try:
                await asyncio.sleep(20)
                yield AssistantDoneEvent(AssistantMessage('late-result'))
            finally:
                self.stopped = True
    async def run():
        provider = Provider()
        tasks, context, tool, _manage, initial = await delegate(tmp_path, provider)
        # Finish the first explicit background invocation; observe a fresh synchronous call.
        await asyncio.to_thread(tasks.stop, initial.details['taskId'], TaskOrigin(context.role_id, context.session_key))
        provider.entered, provider.stopped = False, False
        foreground = asyncio.create_task(tool.execute({'task': 'foreground-only'}, context))
        while not provider.entered:
            await asyncio.sleep(.01)
        foreground.cancel()
        with pytest.raises(asyncio.CancelledError):
            await foreground
        assert provider.stopped
        snapshots = tasks.list(TaskOrigin(context.role_id, context.session_key), kind='spawn')
        assert len(snapshots) == 2
        assert all(item.status == 'cancelled' for item in snapshots)
        assert sum(tasks.completion(item.task_id, item.origin) is not None for item in snapshots) == 1
        tasks.close()
    asyncio.run(run())


def test_global_concurrency_budget_denies_extra_child_before_starting_model(tmp_path):
    class Provider:
        async def stream_response(self, **kwargs):
            await asyncio.sleep(20)
            yield AssistantDoneEvent(AssistantMessage('late'))
    async def run():
        tasks, context, tool, _manage, result = await delegate(tmp_path, Provider())
        try:
            tasks.max_per_role = 1
            denied = await tool.execute({'task': 'another child'}, context)
            assert denied.is_error
            assert len(tasks.list(TaskOrigin(context.role_id, context.session_key), kind='spawn')) == 1
            assert result.details['taskId']
        finally:
            tasks.close()
    asyncio.run(run())


def test_pending_completion_is_retained_and_new_work_is_denied_at_storage_limit(tmp_path):
    class Provider:
        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(AssistantMessage('must-deliver-once'))
    async def run():
        tasks, context, tool, _manage, result = await delegate(tmp_path, Provider())
        task_id = result.details['taskId']
        try:
            await completed(tasks, context, task_id)
            tasks.max_retained_per_role = 1
            denied = await tool.execute({'task': 'cannot evict an undelivered result', 'run_in_background': True}, context)
            assert denied.is_error
            assert tasks.pending_completions()[0].task_id == task_id
            tasks.retention_seconds = -1
            assert tasks.query(task_id, TaskOrigin(context.role_id, context.session_key)).output == 'must-deliver-once'
            assert tasks.completion(task_id, TaskOrigin(context.role_id, context.session_key))['status'] == 'pending'
        finally:
            tasks.close()
    asyncio.run(run())


def test_repeated_foreground_cancel_waits_for_owned_cleanup_after_real_process(tmp_path):
    import threading
    released, cleanup_entered, process_observed = threading.Event(), threading.Event(), threading.Event()
    class Provider:
        async def stream_response(self, *, messages, **kwargs):
            results = [item for item in messages if isinstance(item, ToolResultMessage)]
            if not results:
                command = shlex.join([sys.executable.replace('\\', '/'), '-c', "print('real-process-before-cleanup-441')"])
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('real-process', 'shell', {'command': command}),)))
            else:
                assert 'real-process-before-cleanup-441' in results[-1].content
                process_observed.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    cleanup_entered.set()
                    while not released.is_set():
                        await asyncio.sleep(.01)
    async def run():
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        tasks = TaskRuntime(tmp_path / 'tasks')
        context = ToolContext('r', 'role:r', 'parent', CancellationToken(), 'spawn')
        frozen = CapabilityRegistry([ShellTool(workspace, allowed_commands=[sys.executable])]).resolve(role_id='r',
            session_key='role:r', run_id='parent', enabled_tools={'shell'}, allowed_risks={'external'})
        tool = SpawnTool(tasks, SpawnToolConfig(enabled=True), origin_resolver=lambda ctx: TaskOrigin('r', 'role:r'), authorized=lambda ctx: True)
        tool.bind(frozen, lambda: Provider())
        foreground = asyncio.create_task(tool.execute({'task': 'run one controlled process', 'profile': 'scripting'}, context))
        try:
            deadline = asyncio.get_running_loop().time() + 3
            while not process_observed.is_set():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(.01)
            foreground.cancel()
            while not cleanup_entered.is_set():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(.01)
            for _ in range(3):
                foreground.cancel()
                await asyncio.sleep(.02)
                assert not foreground.done(), 'foreground detached from live owned cleanup'
            snapshot = tasks.list(TaskOrigin('r', 'role:r'), kind='spawn')[0]
            assert snapshot.ended_at is None
            released.set()
            with pytest.raises(asyncio.CancelledError):
                await foreground
            snapshot = tasks.query(snapshot.task_id, TaskOrigin('r', 'role:r'))
            assert snapshot.status == 'cancelled'
            assert snapshot.ended_at is not None
        finally:
            released.set()
            await asyncio.gather(foreground, return_exceptions=True)
            tasks.close()
    asyncio.run(run())
