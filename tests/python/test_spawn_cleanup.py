import asyncio
import threading

import pytest

from backend.app.agent_runtime import CapabilityRegistry, CancellationToken, ToolContext
from backend.app.agent_runtime.provider import AssistantDoneEvent
from backend.app.agent_runtime.spawn_tool import SpawnTool
from backend.app.agent_runtime.types import AssistantMessage
from backend.app.models import SpawnToolConfig
from backend.app.task_runtime import TaskBudget, TaskOrigin, TaskOutcome, TaskRuntime


@pytest.mark.parametrize('exit_reason', ['revoked', 'revoked_cancel', 'disabled', 'disabled_cancel'])
def test_foreground_denial_waits_for_owned_provider_cleanup(tmp_path, exit_reason):
    started, cleaning, release, released = (threading.Event() for _ in range(4))
    allowed = threading.Event()
    allowed.set()

    class Provider:
        async def stream_response(self, **kwargs):
            started.set()
            try:
                await asyncio.sleep(30)
                yield AssistantDoneEvent(AssistantMessage('private-child-result'))
            finally:
                cleaning.set()
                while not release.is_set():
                    await asyncio.sleep(.01)
                released.set()

    async def run():
        tasks = TaskRuntime(tmp_path / 'tasks')
        context = ToolContext('r', 'role:r', 'parent', CancellationToken(), 'spawn')
        origin = TaskOrigin('r', 'role:r')
        frozen = CapabilityRegistry([]).resolve(role_id='r', session_key='role:r', run_id='parent')
        tool = SpawnTool(tasks, SpawnToolConfig(enabled=True), origin_resolver=lambda ctx: origin,
            authorized=lambda ctx: allowed.is_set())
        tool.bind(frozen, Provider)
        foreground = asyncio.create_task(tool.execute({'task': 'private-child-task'}, context))
        disabling = None
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task_id = tasks.list(origin, kind='spawn')[0].task_id
            if exit_reason.startswith('disabled'):
                # Internal role disable closes public result access before joining.
                # Public role deletion rejects an active role run separately.
                disabling = asyncio.create_task(asyncio.to_thread(tasks.set_role_enabled, 'r', False))
            else:
                allowed.clear()
            assert await asyncio.to_thread(cleaning.wait, 3)
            if disabling is not None:
                with pytest.raises(KeyError):
                    tasks.query(task_id, origin)
                with pytest.raises(KeyError):
                    tasks.stop(task_id, origin)
            for _ in range(3):
                if exit_reason.endswith('_cancel'):
                    foreground.cancel()
                await asyncio.sleep(.02)
                assert not foreground.done(), 'denied foreground detached from live owned cleanup'
                assert not released.is_set()
            release.set()
            if exit_reason.endswith('_cancel'):
                with pytest.raises(asyncio.CancelledError):
                    await foreground
            else:
                result = await foreground
                assert result.is_error
                assert result.details == {'status': 'denied'}
                assert 'private-child' not in result.content
                assert task_id not in result.content
            assert released.is_set()
            if disabling is not None:
                await disabling
                tasks.set_role_enabled('r', True)
            snapshot = tasks.query(task_id, origin)
            assert snapshot.status == 'cancelled'
            assert snapshot.ended_at is not None
            assert tasks.completion(task_id, origin) is None
        finally:
            release.set()
            foreground.cancel()
            await asyncio.gather(foreground, *([disabling] if disabling is not None else []), return_exceptions=True)
            tasks.close()

    asyncio.run(run())


class HeldExecutor:
    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.release = threading.Event()

    def run(self, execution):
        self.started.set()
        assert self.release.wait(5), 'fixture cleanup was never released'
        return TaskOutcome('succeeded', output_tail='private-child-result')

    def cancel(self):
        self.cancelled.set()


def test_owned_cleanup_rejects_another_executor_without_cancel_or_result_access(tmp_path):
    tasks = TaskRuntime(tmp_path / 'tasks')
    owner = TaskOrigin('r', 'role:r')
    first, second = HeldExecutor(), HeldExecutor()
    try:
        first_id = tasks.submit(owner, first, budget=TaskBudget(), kind='spawn')
        second_id = tasks.submit(owner, second, budget=TaskBudget(), kind='spawn')
        assert first.started.wait(3) and second.started.wait(3)
        with pytest.raises(KeyError):
            tasks.cancel_owned_execution(first_id, second)
        assert not first.cancelled.is_set() and not second.cancelled.is_set()
        with pytest.raises(KeyError):
            tasks.query(first_id, TaskOrigin('other', 'role:other'))
        first.release.set()
        assert tasks.cancel_owned_execution(first_id, first) is None
        assert tasks.query(first_id, owner).ended_at is not None
        assert tasks.query(second_id, owner).ended_at is None
        assert not second.cancelled.is_set()
    finally:
        first.release.set()
        second.release.set()
        tasks.close()
