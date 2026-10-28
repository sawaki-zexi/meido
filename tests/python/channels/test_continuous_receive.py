"""QQ ingress remains durable while the shared ordinary runtime is busy."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.agent_runtime import AssistantDoneEvent, AssistantMessage, RuntimeManager
from backend.app.agent_runtime.capabilities import CapabilityRegistry
from backend.app.channels import ChannelHost
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.multichannel import MultichannelStore
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore


class Credentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


def payload(message_id, *, group="100", sender="11", trigger="at"):
    segments = [{"type": "text", "data": {"text": message_id}}]
    if trigger == "at":
        segments.insert(0, {"type": "at", "data": {"qq": "99"}})
    elif trigger == "reply":
        segments.insert(0, {"type": "reply", "data": {"id": "bot-prior", "sender_id": "99"}})
    return {"post_type": "message", "message_type": "group", "self_id": 99,
            "group_id": group, "message_id": message_id,
            "sender": {"user_id": sender, "nickname": f"sender-{sender}"}, "message": segments}


@pytest.fixture
def qq(tmp_path, monkeypatch, local_platform_only):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="接收角色", profile=RoleProfile(profile="设定")))
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(tmp_path / "sessions.db", sessions, Credentials())
    account = channels.register_channel_account(role.id, "qq", account_id="qq-continuous", enabled=True, allow_group=True)
    for group in ("100", "200"):
        channels.set_group_listening(role.id, account.account_id, group, True)
    env = SimpleNamespace(role=role, sessions=sessions, channels=channels, account=account,
                          started=asyncio.Event(), release=asyncio.Event(), send_started=asyncio.Event(),
                          send_release=asyncio.Event(), block="model", fail=False, calls=[], sent=[],
                          owners=[], active=0, max_active=0, published=asyncio.Queue())

    async def transport(operation, params):
        if operation == "get_msg":
            return {"data": {"group_id": "100", "sender": {"user_id": 99},
                             "message": [{"type": "text", "data": {"text": "prior reply"}}]}}
        if operation in {"get_friend_list", "get_group_list", "get_group_member_list"}:
            return {"data": []}
        assert operation == "send_group_msg", operation
        env.sent.append(params)
        if env.block == "send" and len(env.sent) == 1:
            env.send_started.set()
            await env.send_release.wait()
        return {"message_id": 1000 + len(env.sent)}

    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
    host = ChannelHost(channels)
    host.register(adapter)
    env.adapter, env.host = adapter, host
    manager = RuntimeManager(sessions)
    env.manager = manager
    channels._subscribers[role.id] = {env.published}

    class Provider:
        def __init__(self, model_adapter, current_role, configuration, *_args, **_kwargs):
            self.adapter, self.role, self.configuration = model_adapter, current_role, configuration

        async def stream_response(self, **kwargs):
            current = next(item.content for item in reversed(kwargs["messages"]) if item.role == "user")
            env.calls.append(current)
            index = len(env.calls)
            env.active += 1
            env.max_active = max(env.max_active, env.active)
            try:
                if index == 1:
                    env.started.set()
                    if env.block == "model":
                        await env.release.wait()
                    if env.fail:
                        raise RuntimeError("controlled model failure")
                yield AssistantDoneEvent(AssistantMessage(f"reply-{index}"))
            finally:
                env.active -= 1

    async def resolve(_role, *, session_key, run_id, external_only, **kwargs):
        env.owners.append(not external_only)
        return CapabilityRegistry(()).resolve(role_id=role.id, session_key=session_key, run_id=run_id)

    async def no_claim(*args, **kwargs):
        return None

    async def commit(*args):
        return None

    lock = asyncio.Lock()
    for name, value in {
        "store": roles, "session_store": sessions, "multichannel_store": channels,
        "channel_host": host, "channel_adapters": {account.account_id: adapter},
        "runtime_manager": manager, "role_locks": {}, "channel_event_locks": {},
        "_channel_roles_deleting": set(), "group_repository": MultichannelGroupRepository(channels),
        "model_configuration_store": ModelConfigurationStore(tmp_path / "models.json"),
        "MeidoProvider": Provider, "_runtime_capabilities": resolve,
        "_ordinary_context_provider": lambda provider, *_args, **_kwargs: provider,
        "_runtime_tool_candidates": lambda *args, **kwargs: (),
        "_accept_owner_run": lambda *args: None,
        "_refresh_channel_avatars": lambda *args: None,
        "_novelai_application": lambda: SimpleNamespace(claim_channel_reply=no_claim, tool_enabled=lambda *_args: False),
        "memory_worker": SimpleNamespace(role_deletion_blocked=lambda *_args: False, role_lock_for=lambda *_args: lock),
        "channel_memory_maintenance": SimpleNamespace(commit_turn=commit, heard=lambda *_args: None),
    }.items():
        monkeypatch.setattr(main, name, value)

    async def dispatch(data):
        await main._dispatch_channel_payload(account.account_id, data, source_adapter=adapter)

    async def received(message_id):
        async def read():
            while True:
                event = await env.published.get()
                if event.get("message", {}).get("metadata", {}).get("platformMessageId") == message_id:
                    return event
        return await asyncio.wait_for(read(), 1)

    def thread(group="100"):
        return channels.ensure_channel_thread(role.id, "qq", account.account_id, group, "group")

    env.dispatch, env.received, env.thread = dispatch, received, thread
    return env


def test_group_reply_disable_invalidates_running_and_queued_turns_only_in_that_group(qq):
    async def scenario():
        first = asyncio.create_task(qq.dispatch(payload("first")))
        pending = []
        try:
            await asyncio.wait_for(qq.started.wait(), 2)
            pending.append(asyncio.create_task(qq.dispatch(payload("queued"))))
            await qq.received("queued")
            pending.append(asyncio.create_task(qq.dispatch(payload("other-group", group="200"))))
            await qq.received("other-group")
            qq.channels.set_group_replies(qq.role.id, qq.account.account_id, "100", False)
            qq.channels.set_group_replies(qq.role.id, qq.account.account_id, "100", True)
            qq.release.set()
            await asyncio.gather(first, *pending)
            assert [item["group_id"] for item in qq.sent] == [200]
            assert len(qq.calls) == 2
            await qq.dispatch(payload("fresh"))
            assert [item["group_id"] for item in qq.sent] == [200, 100]
        finally:
            qq.release.set()
            await asyncio.gather(first, *pending, return_exceptions=True)
    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize("block", ["model", "send"])
def test_model_or_send_does_not_block_same_account_inbound_records_or_live_events(qq, block):
    async def scenario():
        qq.block = block
        first = asyncio.create_task(qq.dispatch(payload("first")))
        later = []
        try:
            await asyncio.wait_for(qq.started.wait() if block == "model" else qq.send_started.wait(), 1)
            later = [asyncio.create_task(qq.dispatch(payload("heard-a", trigger=None))),
                     asyncio.create_task(qq.dispatch(payload("heard-b", group="200", trigger=None))),
                     asyncio.create_task(qq.dispatch(payload("explicit-b", group="200")))]
            for mid in ("heard-a", "heard-b", "explicit-b"):
                event = await qq.received(mid)
                assert event["message"]["metadata"]["senderId"] == "11"
            assert qq.channels.listening.page(qq.role.id, qq.thread().thread_id)["messages"][0]["content"] == "heard-a"
            assert qq.channels.listening.page(qq.role.id, qq.thread("200").thread_id)["messages"][0]["content"] == "heard-b"
            users = [m for m in qq.sessions.list_messages(qq.thread("200").session_key) if m.role == "user"]
            assert [m.content for m in users] == ["explicit-b"]
            assert not first.done()
        finally:
            qq.release.set()
            qq.send_release.set()
            await asyncio.gather(first, *later, return_exceptions=True)
        assert len(qq.calls) == 2
        assert qq.max_active == 1
        assert len(qq.sent) == 2
        assert {str(item["group_id"]) for item in qq.sent} == {"100", "200"}
    asyncio.run(scenario())


def test_queued_at_and_reply_only_are_each_executed_once_without_future_history(qq):
    async def scenario():
        first = asyncio.create_task(qq.dispatch(payload("first")))
        pending = []
        try:
            await asyncio.wait_for(qq.started.wait(), 1)
            pending = [asyncio.create_task(qq.dispatch(payload("second"))),
                       asyncio.create_task(qq.dispatch(payload("third", trigger="reply"))),
                       asyncio.create_task(qq.dispatch(payload("second")))]
            await qq.received("second")
            await qq.received("third")
            assert len(qq.calls) == 1
            users = [m for m in qq.sessions.list_messages(qq.thread().session_key) if m.role == "user"]
            assert [m.content for m in users] == ["first", "second", "third"]
            assert users[-1].metadata["triggered"] and not users[-1].metadata["mentioned"]
        finally:
            qq.release.set()
            await asyncio.gather(first, *pending, return_exceptions=True)
        assert len(qq.calls) == 3
        assert qq.calls[0].endswith("first") and qq.calls[1].endswith("second") and qq.calls[2].endswith("third")
        assert qq.max_active == 1
        assert len(qq.sent) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["cancel", "fail"])
def test_one_failed_or_cancelled_run_releases_the_next_durable_turn(qq, ending):
    async def scenario():
        qq.fail = ending == "fail"
        first = asyncio.create_task(qq.dispatch(payload("first")))
        second = None
        try:
            await asyncio.wait_for(qq.started.wait(), 1)
            first_run = qq.sessions.active_run(qq.role.id)
            second = asyncio.create_task(qq.dispatch(payload("second", group="200")))
            await qq.received("second")
            if ending == "cancel":
                assert qq.manager.cancel_run(first_run.runId)
            else:
                qq.release.set()
            await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 2)
            assert qq.sessions.get_run(first_run.runId).status == ("cancelled" if ending == "cancel" else "failed")
            assert len(qq.calls) == 2
            assert len(qq.sent) == 1 and str(qq.sent[0]["group_id"]) == "200"
            assert qq.sessions.active_run(qq.role.id) is None
            assert not qq.manager.lock_for(qq.role.id).locked()
        finally:
            qq.release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    asyncio.run(scenario())


def test_connection_pause_invalidates_queued_turn_without_waiting_for_it_to_generate(qq):
    async def scenario():
        first = asyncio.create_task(qq.dispatch(payload("first")))
        second = drain = None
        try:
            await asyncio.wait_for(qq.started.wait(), 1)
            second = asyncio.create_task(qq.dispatch(payload("second", group="200")))
            await qq.received("second")
            drain = asyncio.create_task(qq.host.pause_and_drain(qq.account.account_id))
            await asyncio.sleep(0)
            qq.release.set()
            await asyncio.wait_for(asyncio.gather(first, second, drain, return_exceptions=True), 2)
            assert len(qq.calls) == 1, "a queued turn must not become a new admitted generation during handover"
            assert len(qq.sent) == 1 and str(qq.sent[0]["group_id"]) == "100"
        finally:
            qq.release.set()
            await asyncio.gather(first, *([second] if second else []), *([drain] if drain else []), return_exceptions=True)
    asyncio.run(scenario())


def bind_owner(qq, sender="11"):
    code = qq.channels.create_pairing_code(qq.role.id, "qq")
    assert qq.channels.consume_pairing_code(code, role_id=qq.role.id, channel="qq", scope="platform",
                                           platform_user_id=sender, account_id=qq.account.account_id, chat_id=sender)


def test_guest_accepted_before_pairing_never_gains_owner_tools_while_queued(qq):
    async def scenario():
        first = asyncio.create_task(qq.dispatch(payload("first", sender="12")))
        second = None
        try:
            await asyncio.wait_for(qq.started.wait(), 1)
            second = asyncio.create_task(qq.dispatch(payload("second")))
            await qq.received("second")
            bind_owner(qq)
            qq.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            assert qq.owners == [False, False]
            assert len(qq.sent) == 2
        finally:
            qq.release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("rebind", [False, True])
def test_owner_revoke_or_rebind_invalidates_old_queued_authority(qq, rebind):
    async def scenario():
        bind_owner(qq)
        first = asyncio.create_task(qq.dispatch(payload("first", sender="12")))
        second = None
        try:
            await asyncio.wait_for(qq.started.wait(), 1)
            second = asyncio.create_task(qq.dispatch(payload("second")))
            await qq.received("second")
            identity = next(item for item in qq.channels.list_identities() if item["platformUserId"] == "11")
            qq.channels.unbind_identity(identity["identityId"])
            if rebind:
                bind_owner(qq)
            qq.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            assert qq.owners == [False], "a new binding cannot resurrect queued owner authority"
            assert len(qq.sent) == 1
            user = next(m for m in qq.sessions.list_messages(qq.thread().session_key) if m.content == "second")
            assert user.metadata["senderIsOwner"] is True, "the original sender identity remains historical evidence"
        finally:
            qq.release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    asyncio.run(scenario())


def test_cancel_during_dispatched_send_keeps_real_receipt_without_resending(qq):
    async def scenario():
        qq.block = "send"
        first = asyncio.create_task(qq.dispatch(payload("first")))
        try:
            await asyncio.wait_for(qq.send_started.wait(), 1)
            first.cancel()
            await asyncio.sleep(0)
            qq.send_release.set()
            await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), 2)
            deliveries = qq.channels.list_deliveries(qq.role.id)
            assert len(deliveries) == 1
            assert deliveries[0]["status"] == "sent"
            assert deliveries[0]["platformMessageId"] == "1001"
            reply = next(m for m in qq.sessions.list_messages(qq.thread().session_key) if m.role == "assistant")
            assert reply.metadata["deliveryStatus"] == "sent"
            assert reply.metadata["platformMessageId"] == "1001"
            assert len(qq.sent) == 1
        finally:
            qq.send_release.set()
            await asyncio.gather(first, return_exceptions=True)
    asyncio.run(scenario())


def test_connection_generation_replacement_cannot_retarget_running_or_queued_turn(qq, monkeypatch):
    async def scenario():
        first = asyncio.create_task(qq.dispatch(payload("first")))
        second = None
        replacement_sends = []

        async def replacement_transport(operation, params):
            replacement_sends.append((operation, params))
            return {"data": {"message_id": "wrong-generation"}}

        try:
            await asyncio.wait_for(qq.started.wait(), 1)
            second = asyncio.create_task(qq.dispatch(payload("second", group="200")))
            await qq.received("second")
            replacement = NapCatAdapter(NapCatConfig(qq.account.account_id), transport=replacement_transport)
            qq.host.unregister(qq.account.account_id)
            qq.host.register(replacement)
            monkeypatch.setitem(main.channel_adapters, qq.account.account_id, replacement)
            qq.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            assert len(qq.calls) == 1
            assert qq.sent == []
            assert replacement_sends == [], "accepted work must not migrate to the replacement connection"
        finally:
            qq.release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    asyncio.run(scenario())


def test_cancel_before_send_gate_never_dispatches(qq, monkeypatch):
    async def scenario():
        qq.block = "none"
        reached, release = asyncio.Event(), asyncio.Event()
        original = qq.host._begin_send
        async def gate(account_id):
            reached.set()
            await release.wait()
            return await original(account_id)
        monkeypatch.setattr(qq.host, '_begin_send', gate)
        first = asyncio.create_task(qq.dispatch(payload('first')))
        try:
            await asyncio.wait_for(reached.wait(), 1)
            assert not qq.sent
            first.cancel()
            release.set()
            await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), 2)
            assert not qq.sent
            assert not qq.channels.list_deliveries(qq.role.id)
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
    asyncio.run(scenario())


def test_running_state_write_failure_releases_reserved_role(qq, monkeypatch):
    async def scenario():
        qq.block = 'none'
        original = qq.channels.set_channel_turn_state
        failed = False
        def write(thread, message_id, state):
            nonlocal failed
            if state == 'running' and not failed:
                failed = True
                raise OSError('controlled state write failure')
            return original(thread, message_id, state)
        monkeypatch.setattr(qq.channels, 'set_channel_turn_state', write)
        await qq.dispatch(payload('first'))
        assert not qq.manager.lock_for(qq.role.id).locked()
        assert qq.sessions.active_run(qq.role.id) is None
        await asyncio.wait_for(qq.dispatch(payload('second')), 2)
        assert len(qq.calls) == 1 and 'second' in qq.calls[0]
    asyncio.run(scenario())


@pytest.mark.parametrize('uncertain', [False, True])
def test_failed_or_uncertain_send_does_not_retry_or_block_next_turn(qq, monkeypatch, uncertain):
    from backend.app.channels.base import PartialDeliveryError, PlatformRejectedError
    async def scenario():
        qq.block = 'send'
        original = qq.adapter._transport
        async def transport(action, params):
            result = await original(action, params)
            segments = params.get('message', [])
            if action == 'send_group_msg' and any(segment.get('type') == 'reply'
                    and segment.get('data', {}).get('id') == 'first' for segment in segments):
                raise (PartialDeliveryError('unknown receipt') if uncertain else PlatformRejectedError('rejected'))
            return result
        monkeypatch.setattr(qq.adapter, '_transport', transport)
        first = asyncio.create_task(qq.dispatch(payload('first')))
        second = None
        try:
            await asyncio.wait_for(qq.send_started.wait(), 1)
            second = asyncio.create_task(qq.dispatch(payload('second')))
            await qq.received('second')
            qq.send_release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            assert len(qq.sent) == len(qq.calls) == 2
            deliveries = qq.channels.list_deliveries(qq.role.id)
            assert sorted(item['status'] for item in deliveries) == sorted(['sent', 'uncertain' if uncertain else 'failed'])
            history = qq.sessions.runtime_messages(qq.thread().session_key)
            assert all(item.content != 'reply-1' for item in history)
            assert any(item.content == 'reply-2' for item in history)
        finally:
            qq.send_release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    asyncio.run(scenario())


def test_socket_reconnect_under_same_adapter_invalidates_old_queue(qq, monkeypatch):
    async def scenario():
        original = qq.adapter._transport
        class Transport:
            connection_generation = 'socket-one'
            async def __call__(self, action, params):
                return await original(action, params)
        transport = Transport()
        monkeypatch.setattr(qq.adapter, '_transport', transport)
        first = asyncio.create_task(qq.dispatch(payload('first')))
        second = None
        try:
            await asyncio.wait_for(qq.started.wait(), 1)
            second = asyncio.create_task(qq.dispatch(payload('second')))
            await qq.received('second')
            transport.connection_generation = 'socket-two'
            qq.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            assert len(qq.calls) == 1 and qq.sent == []
            await qq.dispatch(payload('new-socket'))
            assert len(qq.calls) == 2 and len(qq.sent) == 1
        finally:
            qq.release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    asyncio.run(scenario())


def test_old_socket_event_waiting_before_acceptance_cannot_inherit_new_epoch(qq, monkeypatch):
    from backend.app.channels.transports import _invoke_event
    async def scenario():
        original = qq.adapter._transport
        class Transport:
            connection_generation = 'socket-one'
            async def __call__(self, action, params):
                return await original(action, params)
        transport = Transport()
        monkeypatch.setattr(qq.adapter, '_transport', transport)
        reached, release = asyncio.Event(), asyncio.Event()
        async def delayed_handler(data):
            reached.set()
            await release.wait()
            await qq.dispatch(data)
        task = asyncio.create_task(_invoke_event(delayed_handler, payload('old'), napcat_generation='socket-one'))
        try:
            await reached.wait()
            transport.connection_generation = 'socket-two'
            release.set()
            await asyncio.wait_for(task, 1)
            assert not qq.calls and not qq.sent
            messages = qq.sessions.list_messages(qq.thread().session_key)
            assert len(messages) == 1 and messages[0].metadata['channelTurnState'] == 'invalidated'
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_paused_intake_replay_keeps_original_socket_epoch(qq, monkeypatch):
    from backend.app.channels.transports import _invoke_event
    async def scenario():
        qq.block = 'none'
        original = qq.adapter._transport
        class Transport:
            connection_generation = 'socket-one'
            async def __call__(self, action, params):
                return await original(action, params)
        transport = Transport()
        monkeypatch.setattr(qq.adapter, '_transport', transport)
        await qq.host.pause_and_drain(qq.account.account_id)
        await _invoke_event(qq.dispatch, payload('old-buffered'), napcat_generation='socket-one')
        assert not qq.calls
        transport.connection_generation = 'socket-two'
        await qq.host.resume(qq.account.account_id)
        await qq.host.intake.drain(qq.account.account_id)
        assert not qq.calls and not qq.sent
        messages = qq.sessions.list_messages(qq.thread().session_key)
        assert len(messages) == 1 and messages[0].metadata['channelTurnState'] == 'invalidated'
        await qq.dispatch(payload('new-event'))
        assert len(qq.calls) == len(qq.sent) == 1
    asyncio.run(scenario())
