"""Cancellation keeps the schedule's worker threads owned through completion."""
import asyncio
from threading import Event

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.schedules.tools import ScheduleManageTool
from backend.app.task_runtime import TaskOrigin


@pytest.mark.parametrize('failure', [None, ValueError, PermissionError])
def test_cancel_tool_joins_its_thread_and_preserves_cancellation_over_thread_error(failure):
    entered, release, finished = Event(), Event(), Event()
    class Application:
        def cancel(self, origin, **kwargs):
            entered.set()
            assert release.wait(3)
            try:
                if failure:
                    raise failure('controlled stop failure')
                return {'cancelled': 1}
            finally:
                finished.set()
    tool = ScheduleManageTool('cancel_schedule', lambda: Application(),
        origin_resolver=lambda context: TaskOrigin('role', 'role:role'), authorized=lambda context: True)
    async def run():
        pending = asyncio.create_task(tool.execute({'id': '12345678'},
            ToolContext('role', 'role:role', 'run', CancellationToken(), 'call')))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(3):
                pending.cancel()
                await asyncio.sleep(.01)
                assert not pending.done() and not finished.is_set()
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
        assert finished.is_set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    asyncio.run(run())
