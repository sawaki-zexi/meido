"""Foreground Shell owns cleanup even when result access has been revoked."""
import asyncio
import shlex
import sys
import threading
import time

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.agent_runtime import shell_tool
from backend.app.task_runtime import TaskOrigin, TaskOutcome, TaskRuntime


PRIVATE_OUTPUT = 'SYNTHETIC_PRIVATE_OUTPUT_333'


class HeldExecutor:
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.release = threading.Event()
        self.released = threading.Event()
        self.cancel_count = 0

    def run(self, execution):
        execution.write(PRIVATE_OUTPUT)
        self.started.set()
        try:
            assert self.release.wait(25), 'fixture resource was never released'
            return TaskOutcome('succeeded', 0, PRIVATE_OUTPUT)
        finally:
            self.released.set()

    def cancel(self):
        self.cancel_count += 1
        self.cancelled.set()


def make_shell(tmp_path, monkeypatch, *, timeout=60):
    executor = HeldExecutor()
    monkeypatch.setattr(shell_tool, 'ProcessExecutor', lambda *args: executor)
    tasks = TaskRuntime(tmp_path / 'tasks')
    allowed = threading.Event()
    allowed.set()
    context = ToolContext('r', 'role:r', 'parent', CancellationToken(), 'shell')
    origin = TaskOrigin('r', 'role:r')
    tool = shell_tool.ShellTool(tmp_path, tasks=tasks, allowed_commands=[sys.executable],
        timeout_seconds=timeout, authorized=lambda ctx: allowed.is_set())
    arguments = {'command': shlex.join([sys.executable, '-c', 'pass']), 'auto_promote': False}
    return tool, tasks, executor, allowed, context, origin, arguments


def assert_denied(result):
    assert result.is_error
    assert result.details == {'status': 'denied', 'exitCode': None}
    assert PRIVATE_OUTPUT not in result.content


@pytest.mark.parametrize('exit_reason', ['revoked', 'disabled', 'cancelled'])
@pytest.mark.parametrize('repeat_cancel', [False, True])
def test_foreground_exit_waits_for_owned_cleanup(tmp_path, monkeypatch, exit_reason, repeat_cancel):
    tool, tasks, executor, allowed, context, origin, arguments = make_shell(tmp_path, monkeypatch)

    async def run():
        foreground = asyncio.create_task(tool.execute(arguments, context))
        disabling = None
        try:
            assert await asyncio.to_thread(executor.started.wait, 3)
            task_id = tasks.list(origin, kind='shell')[0].task_id
            if exit_reason == 'revoked':
                allowed.clear()
            elif exit_reason == 'disabled':
                # Internal disable closes public access before its cleanup joins.
                disabling = asyncio.create_task(asyncio.to_thread(tasks.set_role_enabled, 'r', False))
            else:
                foreground.cancel()
            assert await asyncio.to_thread(executor.cancelled.wait, 3)
            if disabling is not None:
                with pytest.raises(KeyError):
                    tasks.query(task_id, origin)
                with pytest.raises(KeyError):
                    tasks.stop(task_id, origin)
            for _ in range(3):
                if repeat_cancel:
                    foreground.cancel()
                await asyncio.sleep(.02)
                assert not foreground.done(), 'foreground detached from live owned cleanup'
                assert not executor.released.is_set()
            executor.release.set()
            if repeat_cancel or exit_reason == 'cancelled':
                with pytest.raises(asyncio.CancelledError):
                    await foreground
            else:
                assert_denied(await foreground)
            assert executor.released.is_set()
            if disabling is not None:
                await disabling
                tasks.set_role_enabled('r', True)
            snapshot = tasks.query(task_id, origin)
            assert snapshot.status == 'cancelled'
            assert snapshot.ended_at is not None
            assert tasks.completion(task_id, origin) is None
        finally:
            executor.release.set()
            foreground.cancel()
            await asyncio.gather(foreground, *([disabling] if disabling else []), return_exceptions=True)
            tasks.close()

    asyncio.run(run())


@pytest.mark.parametrize('access_after_cancel', ['valid', 'revoked', 'disabled'])
def test_token_cancel_rechecks_result_access_after_cleanup(tmp_path, monkeypatch, access_after_cancel):
    tool, tasks, executor, allowed, context, origin, arguments = make_shell(tmp_path, monkeypatch)

    async def run():
        foreground = asyncio.create_task(tool.execute(arguments, context))
        disabling = None
        try:
            assert await asyncio.to_thread(executor.started.wait, 3)
            task_id = tasks.list(origin, kind='shell')[0].task_id
            context.signal.cancel()
            assert await asyncio.to_thread(executor.cancelled.wait, 3)
            if access_after_cancel == 'revoked':
                allowed.clear()
            elif access_after_cancel == 'disabled':
                disabling = asyncio.create_task(asyncio.to_thread(tasks.set_role_enabled, 'r', False))
                await asyncio.sleep(.03)
                with pytest.raises(KeyError):
                    tasks.query(task_id, origin)
            await asyncio.sleep(.03)
            assert not foreground.done()
            assert not executor.released.is_set()
            executor.release.set()
            result = await foreground
            if access_after_cancel == 'valid':
                assert result.details['status'] == 'cancelled'
                assert PRIVATE_OUTPUT in result.content
            else:
                assert_denied(result)
            if disabling is not None:
                await disabling
                tasks.set_role_enabled('r', True)
            assert executor.released.is_set()
            assert tasks.query(task_id, origin).ended_at is not None
        finally:
            executor.release.set()
            foreground.cancel()
            await asyncio.gather(foreground, *([disabling] if disabling else []), return_exceptions=True)
            tasks.close()

    asyncio.run(run())


def test_foreground_cleanup_preserves_first_timeout_reason(tmp_path, monkeypatch):
    tool, tasks, executor, _, context, origin, arguments = make_shell(tmp_path, monkeypatch, timeout=.1)

    async def run():
        foreground = asyncio.create_task(tool.execute(arguments, context))
        try:
            assert await asyncio.to_thread(executor.started.wait, 3)
            task_id = tasks.list(origin, kind='shell')[0].task_id
            assert await asyncio.to_thread(executor.cancelled.wait, 3)
            context.signal.cancel()
            await asyncio.sleep(.03)
            assert not foreground.done()
            executor.release.set()
            result = await foreground
            assert result.details['status'] == 'timed_out'
            assert executor.released.is_set()
            assert tasks.query(task_id, origin).status == 'timed_out'
        finally:
            executor.release.set()
            foreground.cancel()
            await asyncio.gather(foreground, return_exceptions=True)
            tasks.close()

    asyncio.run(run())


def test_cleanup_timeout_is_one_bounded_wait_and_keeps_nonterminal_truth(tmp_path, monkeypatch):
    tool, tasks, executor, _, context, origin, arguments = make_shell(tmp_path, monkeypatch)

    async def run():
        foreground = asyncio.create_task(tool.execute(arguments, context))
        try:
            assert await asyncio.to_thread(executor.started.wait, 3)
            task_id = tasks.list(origin, kind='shell')[0].task_id
            started = time.monotonic()
            context.signal.cancel()
            # Exercise the real Runtime's 10-second bound; a second serial wait
            # would still be pending. Do not replace the cleanup implementation.
            done, _ = await asyncio.wait({foreground}, timeout=13)
            assert foreground in done, 'cleanup started a second resource-release wait'
            with pytest.raises(TimeoutError, match='did not release its resources'):
                await foreground
            assert 9 <= time.monotonic() - started < 13
            assert executor.cancel_count == 1
            snapshot = tasks.query(task_id, origin)
            assert snapshot.status == 'running' and snapshot.ended_at is None
            assert not executor.released.is_set()
        finally:
            executor.release.set()
            foreground.cancel()
            await asyncio.gather(foreground, return_exceptions=True)
            tasks.close()

    asyncio.run(run())


@pytest.mark.parametrize('handoff', ['explicit', 'promoted'])
def test_background_handoff_survives_parent_token_end(tmp_path, monkeypatch, handoff):
    tool, tasks, executor, _, context, origin, arguments = make_shell(tmp_path, monkeypatch)
    if handoff == 'explicit':
        arguments['run_in_background'] = True
    else:
        arguments['auto_promote'] = True
        tool.promote_after_seconds = .01

    async def run():
        try:
            result = await tool.execute(arguments, context)
            assert result.details['background'] is True
            assert await asyncio.to_thread(executor.started.wait, 3)
            task_id = result.details['taskId']
            context.signal.cancel()
            await asyncio.sleep(.1)
            assert not executor.cancelled.is_set()
            assert tasks.query(task_id, origin).ended_at is None
            executor.release.set()
            snapshot = await asyncio.to_thread(tasks.query, task_id, origin, wait_seconds=3)
            assert snapshot.status == 'succeeded'
            assert snapshot.ended_at is not None
            assert PRIVATE_OUTPUT in snapshot.output
        finally:
            executor.release.set()
            tasks.close()

    asyncio.run(run())
