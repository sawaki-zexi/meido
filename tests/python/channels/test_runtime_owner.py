"""The original channel loop owns each submitted coroutine through real Task done."""
import asyncio
import threading

import pytest

from backend.app.channels.runtime_owner import ChannelRuntimeOwner


class _LoopThread:
    """A bounded, naturally joined stdlib loop; no application or platform I/O."""
    def __init__(self):
        self.ready, self.closed = threading.Event(), threading.Event()
        self.releases, self.errors = [], []
        self.loop = None
        self.thread = threading.Thread(target=self._serve, name='test-channel-runtime-owner')

    def _serve(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

        def error_handler(owner_loop, context):
            self.errors.append(context.get('exception') or RuntimeError(context.get('message', 'loop callback failed')))
            owner_loop.default_exception_handler(context)
        loop.set_exception_handler(error_handler)
        self.ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            finally:
                loop.close()
                self.closed.set()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5) and self.loop is not None
        return self

    def invoke(self, operation):
        return asyncio.run_coroutine_threadsafe(operation, self.loop).result(timeout=5)

    def __exit__(self, *_):
        for release in self.releases:
            release.set()

        async def join_remaining():
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        try:
            self.invoke(join_remaining())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
        assert not self.thread.is_alive() and self.closed.is_set()
        assert self.loop.is_closed() and self.errors == []


async def _bind(owner):
    assert owner.bind() is True
    return asyncio.get_running_loop()


async def _event(event):
    assert await asyncio.to_thread(event.wait, 5), 'fixture event did not reach its bounded checkpoint'


async def _cancelled(task):
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=5)


def test_unbound_run_uses_the_callers_original_task_without_binding():
    owner, result = ChannelRuntimeOwner(), object()
    calls = []

    async def scenario():
        caller = asyncio.current_task()
        loop = asyncio.get_running_loop()

        async def factory():
            calls.append((asyncio.current_task(), asyncio.get_running_loop()))
            return result
        assert await owner.run(factory) is result
        assert calls == [(caller, loop)]
        assert owner.loop is None
    asyncio.run(scenario())


def test_bind_is_explicit_readonly_and_same_loop_run_preserves_the_original_result():
    owner, result = ChannelRuntimeOwner(), object()
    assert owner.loop is None

    async def scenario():
        loop = asyncio.get_running_loop()
        assert owner.bind() is True and owner.loop is loop
        assert owner.bind() is False and owner.loop is loop
        with pytest.raises(AttributeError):
            owner.loop = None
        calls = []

        async def factory():
            calls.append(asyncio.get_running_loop())
            return result
        assert await owner.run(factory) is result
        assert calls == [loop]
    asyncio.run(scenario())


def test_closed_bound_loop_rejects_before_factory_then_allows_an_explicit_new_binding():
    owner, calls = ChannelRuntimeOwner(), []
    with _LoopThread() as host:
        original_loop = host.invoke(_bind(owner))
    assert original_loop.is_closed() and owner.loop is original_loop

    async def factory():
        calls.append(asyncio.get_running_loop())
        return 'new explicit owner'

    async def scenario():
        with pytest.raises(RuntimeError):
            await owner.run(factory)
        assert calls == [] and owner.loop is original_loop
        assert owner.bind() is True
        assert owner.loop is asyncio.get_running_loop()
        assert await owner.run(factory) == 'new explicit owner'
        assert calls == [asyncio.get_running_loop()]
    asyncio.run(scenario())


def test_live_original_loop_cannot_be_rebound_from_another_running_loop():
    owner = ChannelRuntimeOwner()
    with _LoopThread() as host:
        original_loop = host.invoke(_bind(owner))

        async def scenario():
            with pytest.raises(RuntimeError):
                owner.bind()
            assert owner.loop is original_loop

            async def factory():
                return asyncio.get_running_loop(), threading.get_ident()
            loop, thread = await owner.run(factory)
            assert loop is original_loop and thread == host.thread.ident
        asyncio.run(scenario())


def test_cross_loop_cancel_waits_for_original_finally_and_repeated_cancel_does_not_interrupt_it():
    owner = ChannelRuntimeOwner()
    started, cleaning, release, finished, done = (threading.Event() for _ in range(5))
    original = {}
    with _LoopThread() as host:
        host.releases.append(release)
        host.invoke(_bind(owner))

        async def factory():
            original['calls'] = original.get('calls', 0) + 1
            original['task'] = asyncio.current_task()
            original['loop'] = asyncio.get_running_loop()
            original['task'].add_done_callback(lambda completed: done.set())
            started.set()
            try:
                await asyncio.Event().wait()  # Ordinary cancellable await; no irrevocable Future.
            finally:
                cleaning.set()
                while not release.is_set():
                    await asyncio.sleep(.005)
                finished.set()

        async def scenario():
            caller = asyncio.create_task(owner.run(factory))
            try:
                await _event(started)
                caller.cancel()
                await _event(cleaning)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not caller.done() and not original['task'].done()
                assert not finished.is_set() and not done.is_set()
                assert original['loop'] is host.loop and original['calls'] == 1
                with pytest.raises(RuntimeError):
                    owner.bind()
                caller.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not caller.done() and not original['task'].done()
                assert not finished.is_set() and not done.is_set()
                release.set()
                await _cancelled(caller)
                await _event(done)
                assert finished.is_set() and original['task'].done() and original['calls'] == 1
            finally:
                release.set()
                if not caller.done():
                    caller.cancel()
                await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), timeout=5)
        asyncio.run(scenario())


def test_cancel_before_host_accepts_submission_calls_no_factory_and_waits_for_the_host():
    owner = ChannelRuntimeOwner()
    entered, release, host_processed = (threading.Event() for _ in range(3))
    calls = []
    with _LoopThread() as host:
        host.releases.append(release)
        host.invoke(_bind(owner))

        def hold_host_dispatch():
            entered.set()
            try:
                if not release.wait(5):
                    raise TimeoutError('fixture host-dispatch gate exceeded its bounded lifetime')
            finally:
                host_processed.set()
        host.loop.call_soon_threadsafe(hold_host_dispatch)
        assert entered.wait(5)

        async def factory():
            calls.append((asyncio.get_running_loop(), threading.get_ident()))
            return 'a cancelled-before-dispatch factory must never run'

        async def scenario():
            caller = asyncio.create_task(owner.run(factory))
            try:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                caller.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert calls == [] and not caller.done()
                assert not host_processed.is_set()
                release.set()
                await _cancelled(caller)
                await _event(host_processed)

                async def checkpoint():
                    await asyncio.sleep(0)
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(checkpoint(), host.loop))
                assert calls == []
            finally:
                release.set()
                if not caller.done():
                    caller.cancel()
                await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), timeout=5)
        asyncio.run(scenario())
