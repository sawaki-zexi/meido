"""One TaskRuntime executor owns the complete asynchronous tick and cleanup."""
import asyncio
import threading

from ..task_runtime import TaskOutcome
from ..mcp_stdio import finish_cleanup
from .meme_storage import MemeQuotaError
from ..ai.request_budget import ImageRequestBudgetError


class AutonomyExecutor:
    def __init__(self, application, origin):
        self.application, self.origin = application, origin
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self._gate = threading.Lock()
        self._loop = None
        self._task = None

    def cancel(self):
        with self._gate:
            if self.cancelled.is_set():
                return
            self.cancelled.set()
            # Stop, the authorization watcher and role cleanup share this
            # executor. A second Task.cancel can interrupt adapter finally
            # blocks before the owned cleanup has actually released.
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)

    def run(self, execution):
        async def owned():
            with self._gate:
                self._loop = asyncio.get_running_loop()
                self._task = asyncio.current_task()
            try:
                if self.cancelled.is_set():
                    raise asyncio.CancelledError()
                return await self.application._execute_operation(self.origin, execution, self.cancelled)
            finally:
                try:
                    # A transport failure must not discard the only loop able
                    # to join its asyncio pipes. Cleanup is separate from, and
                    # never replays, the already consumed business operation.
                    cleanup = asyncio.create_task(self.application._finish_operation(self.origin))
                    await finish_cleanup(cleanup)
                finally:
                    with self._gate:
                        self._loop = self._task = None
        try:
            return asyncio.run(owned())
        except asyncio.CancelledError:
            self.application._abort_tick(self.origin, "cancelled")
            return TaskOutcome("cancelled")
        except MemeQuotaError:
            self.application._abort_tick(self.origin, MemeQuotaError.code)
            return TaskOutcome('failed', output_tail=MemeQuotaError.code)
        except ImageRequestBudgetError as error:
            cause = 'drift_image_budget:' + error.code
            self.application._abort_tick(self.origin, cause)
            return TaskOutcome('failed', output_tail=cause)
        except Exception:
            self.application._abort_tick(self.origin, "failed")
            return TaskOutcome("failed", output_tail="autonomy tick failed")
        finally:
            self.finished.set()
