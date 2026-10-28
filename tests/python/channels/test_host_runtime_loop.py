from __future__ import annotations

import asyncio
from contextlib import contextmanager
import contextvars
import threading

import pytest

from backend.app.channels import ChannelHost, DeliveryResult, DeliveryStatus, OutboundMessage
from tests.python.channels.test_host import FakeAdapter, make_store, make_target


_request_marker = contextvars.ContextVar("host_runtime_test_marker", default="missing")


def _configured_host(tmp_path, adapter_type=FakeAdapter):
    store = make_store(tmp_path)
    account = store.register_channel_account("role", "qq", account_id="qq", enabled=True)
    target = make_target(account.account_id)
    adapter = adapter_type(account.account_id, target)
    host = ChannelHost(store)
    host.register(adapter)
    return host, adapter, target


def test_bound_same_loop_stream_preserves_original_task_context_and_admitted_event(tmp_path):
    observations = []

    def observe(stage):
        observations.append((stage, asyncio.get_running_loop(), asyncio.current_task(), _request_marker.get()))

    class StreamingAdapter(FakeAdapter):
        async def stream_start(self, target, **_kwargs):
            observe("stream-start")
            return target

        async def stream_update(self, _target, content):
            observe(content)

        async def stream_finalize(self, target, content):
            observe("stream-finalize")
            assert content == "complete reply"
            return DeliveryResult(DeliveryStatus.SENT, target, "stream-platform-message")

        async def send(self, _message):
            raise AssertionError("an admitted streaming reply must retain its stream")

    host, adapter, target = _configured_host(tmp_path, StreamingAdapter)

    async def exercise():
        host.bind_runtime_loop()
        host.bind_runtime_loop()
        await host.account_targets("role", "qq")
        thread = host.store.ensure_channel_thread("role", "qq", "qq", target.chat_id, "private")
        caller = asyncio.current_task()
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        marker = _request_marker.set("original-chat")
        drain = None
        try:
            async with host.event_scope("qq") as admitted:
                assert admitted
                drain = asyncio.create_task(host.pause_and_drain("qq"))
                async with asyncio.timeout(5):
                    while host.accepts_events("qq"):
                        await asyncio.sleep(0)
                assert not drain.done()

                async def updates():
                    observe("stream-iterator")
                    while (item := await queue.get()) is not None:
                        yield item

                def authorize():
                    observe("authorization")
                    return True

                queue.put_nowait("partial reply")
                queue.put_nowait("complete reply")
                queue.put_nowait(None)
                result = await host.account_send(
                    "role", "qq", target.id, "", actor="reply",
                    source_thread_id=thread.thread_id,
                    stream_updates=updates(), authorize_source=authorize,
                )
                assert result.status is DeliveryStatus.SENT
                assert result.platform_message_id == "stream-platform-message"
                assert not drain.done()
            await asyncio.wait_for(drain, 5)
            assert observations
            assert {item[0] for item in observations} == {
                "authorization", "stream-start", "stream-iterator",
                "partial reply", "complete reply", "stream-finalize",
            }
            assert all(item[1] is loop and item[2] is caller and item[3] == "original-chat" for item in observations)
            deliveries = host.store.list_deliveries("role")
            assert len(deliveries) == 1 and deliveries[0]["status"] == "sent"
            messages = host.store.sessions.list_messages(thread.session_key)
            assert [message.content for message in messages] == ["complete reply"]
            assert not adapter.sent
        finally:
            _request_marker.reset(marker)
            if drain is not None and not drain.done():
                await asyncio.wait_for(drain, 5)
            await host.stop("qq")

    asyncio.run(exercise())


@pytest.mark.parametrize("closed", [False, True], ids=["stopped", "closed"])
def test_bound_unavailable_loop_rejects_send_before_attempt_without_worker_fallback(tmp_path, closed):
    host, adapter, target = _configured_host(tmp_path)
    owner = asyncio.new_event_loop()

    async def bind():
        host.bind_runtime_loop()
        await host.account_targets("role", "qq")

    try:
        owner.run_until_complete(bind())
        if closed:
            owner.close()
        with pytest.raises(RuntimeError, match="original channel runtime loop is unavailable"):
            asyncio.run(host.account_send("role", "qq", target.id, "must not escape", actor="owner"))
        assert adapter.sent == []
        assert host.store.list_deliveries("role") == []
        assert host.store.get_thread_by_chat("role", "qq", target.chat_id) is None
    finally:
        if not owner.is_closed():
            owner.close()


def test_new_host_can_rebuild_without_transferring_old_registered_adapters(tmp_path):
    host, original_adapter, target = _configured_host(tmp_path)
    owner = asyncio.new_event_loop()

    async def prepare():
        host.bind_runtime_loop()
        await host.account_targets("role", "qq")

    try:
        owner.run_until_complete(prepare())
    finally:
        owner.close()

    async def rebuild():
        with pytest.raises(RuntimeError, match="original channel host still owns resources"):
            host.bind_runtime_loop()
        rebuilt = ChannelHost(host.store)
        adapter = FakeAdapter("qq", target)
        rebuilt.register(adapter)
        rebuilt.bind_runtime_loop()
        try:
            result = await rebuilt.account_send("role", "qq", target.id, "new host reply", actor="owner")
            assert result.status is DeliveryStatus.SENT
            assert len(adapter.sent) == 1
            assert original_adapter.sent == []
            assert len(host.store.list_deliveries("role")) == 1
        finally:
            await rebuilt.stop("qq")

    asyncio.run(rebuild())


@contextmanager
def _service_loop(host, adapter):
    ready = threading.Event()
    state = {}
    errors = []

    def serve():
        loop = asyncio.new_event_loop()
        state["loop"] = loop

        async def lifetime():
            stop = asyncio.Event()
            state["stop"] = stop
            try:
                host.bind_runtime_loop()
                await host.account_targets("role", "qq")
                await host.start("qq")
                ready.set()
                await stop.wait()
            finally:
                await host.pause_and_drain("qq")
                await host.stop("qq")

        try:
            loop.run_until_complete(lifetime())
        except BaseException as error:
            errors.append(error)
            ready.set()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()

    thread = threading.Thread(target=serve, name="test-owned-channel-loop")
    thread.start()
    try:
        assert ready.wait(5), "channel fixture did not start"
        if errors:
            raise errors[0]
        yield state["loop"]
    finally:
        loop = state.get("loop")
        if loop is not None and loop.is_running():
            # Always release the synthetic adapter, including assertion failures.
            loop.call_soon_threadsafe(adapter.release.set)
            loop.call_soon_threadsafe(state["stop"].set)
        thread.join(8)
        assert not thread.is_alive(), "the original channel owner did not join"
        if errors:
            raise errors[0]


def test_cross_loop_repeated_cancel_joins_original_task_finally_and_send_lease(tmp_path):
    class HoldingAdapter(FakeAdapter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.entered = threading.Event()
            self.cleanup_entered = threading.Event()
            self.cleanup_finished = threading.Event()
            self.original_finished = threading.Event()
            self.release = asyncio.Event()
            self.original_task = None
            self.original_loop = None
            self.interrupted_cleanup = False
            self.calls = 0

        async def send(self, message: OutboundMessage):
            self.calls += 1
            self.original_task = asyncio.current_task()
            self.original_loop = asyncio.get_running_loop()
            self.original_task.add_done_callback(lambda _task: self.original_finished.set())
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cleanup_entered.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.interrupted_cleanup = True
                    raise
                self.cleanup_finished.set()

    host, adapter, target = _configured_host(tmp_path, HoldingAdapter)
    with _service_loop(host, adapter) as owner:
        async def exercise():
            assert asyncio.get_running_loop() is not owner
            caller = asyncio.create_task(host.account_send("role", "qq", target.id, "one owned attempt", actor="owner"))
            drain = None
            try:
                assert await asyncio.to_thread(adapter.entered.wait, 5)
                caller.cancel()
                assert await asyncio.to_thread(adapter.cleanup_entered.wait, 5)

                drain_started = threading.Event()

                async def pause():
                    drain_started.set()
                    await host.pause_and_drain("qq")

                drain = asyncio.run_coroutine_threadsafe(pause(), owner)
                assert await asyncio.to_thread(drain_started.wait, 5)
                for _ in range(2):
                    caller.cancel()
                    await asyncio.sleep(0)
                assert not caller.done(), "caller returned before original cleanup"
                assert not adapter.original_task.done()
                assert not drain.done(), "send lease released while original adapter was still cleaning up"
                assert not adapter.cleanup_finished.is_set()
                assert not adapter.interrupted_cleanup

                owner.call_soon_threadsafe(adapter.release.set)
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(caller), 5)
                await asyncio.wait_for(asyncio.wrap_future(drain), 5)
                assert adapter.cleanup_finished.is_set()
                assert adapter.original_task.done()
                assert await asyncio.to_thread(adapter.original_finished.wait, 5)
                assert adapter.original_loop is owner
                assert not adapter.interrupted_cleanup
                assert adapter.calls == 1
                deliveries = host.store.list_deliveries("role")
                assert len(deliveries) == 1 and deliveries[0]["status"] == "uncertain"
            finally:
                owner.call_soon_threadsafe(adapter.release.set)
                if not caller.done():
                    caller.cancel()
                await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), 5)
                if drain is not None:
                    await asyncio.wait_for(asyncio.wrap_future(drain), 5)

        asyncio.run(exercise())
