"""A live poll survives observations and rejects actual connection replacement."""
import asyncio

import pytest

from backend.app.multichannel import MultichannelStore, TelegramChannelService
from backend.app.session_store import SessionStore

pytestmark = pytest.mark.usefixtures("local_platform_only")


class Credentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


@pytest.fixture
def polling_store(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions, Credentials())
    account = store.save_account("role", token="fixture", enabled=True)
    store.set_platform_identity(account.account_id, "123", display_name="before")
    return store, account.account_id


async def exercise_poll(store, account_id, change, *, continues, failed_poll=False):
    entered, release, continued, delivered = (asyncio.Event() for _ in range(4))
    received = []

    class Transport:
        calls = 0

        async def get_me(self, token):
            return {"id": 123, "username": "after"}

        async def get_updates(self, token, *, offset=None):
            self.calls += 1
            if self.calls == 1:
                return []
            if self.calls == 2:
                entered.set()
                await release.wait()
                if failed_poll:
                    raise ConnectionError("fixture poll failed")
                return [{"update_id": 1}]
            continued.set()
            await asyncio.Future()

    async def route(account_id, update):
        received.append(update)
        delivered.set()

    transport = Transport()
    service = TelegramChannelService(store, account_id, transport, on_update=route)
    stop = asyncio.Event()
    running = asyncio.create_task(service.run_polling(stop))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert service.connection_snapshot()["online"] is True
        await change(service)
        if continues:
            assert service.connection_snapshot()["online"] is True
        release.set()
        if continues:
            await asyncio.wait_for(continued.wait(), 1)
            await asyncio.wait_for(delivered.wait(), 1)
            assert not running.done() and transport.calls == 3
            assert received == [{"update_id": 1}]
            assert service.connection_snapshot()["online"] is True
        else:
            await asyncio.wait_for(running, 1)
            assert received == [] and transport.calls == 2
            assert service.connection_snapshot()["online"] is not True
            assert store.get_account(account_id).last_error is None
    finally:
        stop.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("change", ["control", "connection_test", "message_error", "clear_error", "display_name", "identity_rejected"])
def test_observation_updates_preserve_poll_and_next_message(polling_store, change):
    store, account_id = polling_store
    if change == "clear_error":
        store.set_account_state(account_id, last_error="previous_failure")

    async def mutate(service):
        if change == "connection_test":
            await service.test_account(account_id)
        elif change == "message_error":
            async def failed_message():
                raise ValueError("fixture message failure")
            await service._complete_message(failed_message())
        elif change == "clear_error":
            store.set_account_state(account_id, last_error=None)
        elif change == "display_name":
            store.save_account("role", account_id=account_id, enabled=True, display_name="after")
        elif change == "identity_rejected":
            with pytest.raises(ValueError):
                store.set_platform_identity(account_id, "456")

    asyncio.run(exercise_poll(store, account_id, mutate, continues=True))


@pytest.mark.parametrize("change", ["token", "token_restore", "permissions_restore", "account_restore",
    "config_restore", "platform_restore", "recreated"])
@pytest.mark.parametrize("failed_poll", [False, True])
def test_actual_connection_changes_reject_inflight_updates_even_after_restore(polling_store, change, failed_poll):
    store, account_id = polling_store

    async def mutate(service):
        if change in {"token", "token_restore"}:
            store.save_account("role", account_id=account_id, enabled=True, token="replacement")
            if change == "token_restore":
                store.save_account("role", account_id=account_id, enabled=True, token="fixture")
        elif change == "permissions_restore":
            store.save_account("role", account_id=account_id, enabled=True, allow_group=True)
            store.save_account("role", account_id=account_id, enabled=True, allow_group=False)
        elif change == "account_restore":
            store.set_account_state(account_id, enabled=False)
            store.set_account_state(account_id, enabled=True)
        elif change == "config_restore":
            store.set_channel_config(account_id, {"fixture": True})
            store.set_channel_config(account_id, {})
        elif change == "platform_restore":
            store.set_channel_enabled("telegram", False)
            store.set_channel_enabled("telegram", True)
        elif change == "recreated":
            store.delete_account(account_id)
            store.save_account("role", account_id=account_id, enabled=True, token="fixture")
            store.set_platform_identity(account_id, "123")

    asyncio.run(exercise_poll(store, account_id, mutate, continues=False, failed_poll=failed_poll))
