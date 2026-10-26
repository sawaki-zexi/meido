"""Paired QQ identities must reach the model in a group, not just its tools."""
import asyncio
import pytest

from backend.app import main
from backend.app.model_adapter import ModelTextDelta
from test_meme_turn import meme_turn, payload


@pytest.mark.parametrize("sender, unbound, owner", [("11", False, True), ("22", False, False), ("11", True, False)])
def test_paired_private_identity_is_present_in_group_model_context(meme_turn, monkeypatch, sender, unbound, owner):
    state = meme_turn
    requests = []
    state.channels.unbind_identity(str(state.channels.list_identities()[0]["identityId"]))
    code = state.channels.create_pairing_code(state.role.id, "qq")
    original_transport = state.adapter._transport

    async def transport(operation, params):
        if operation == "send_private_msg":
            assert params["user_id"] == 11
            return {"message_id": "paired"}
        return await original_transport(operation, params)

    monkeypatch.setattr(state.adapter, "_transport", transport)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("收到")

    monkeypatch.setattr(main, "model_adapter", Model())

    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, {
                "post_type": "message", "message_type": "private", "self_id": 99, "user_id": 11,
                "message_id": "pair", "sender": {"user_id": 11, "nickname": "主人"},
                "message": [{"type": "text", "data": {"text": f"/pair {code}"}}],
            }, source_adapter=state.adapter)
            assert len(state.channels.list_identities()) == 1
            private = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, "11", chat_type="private")
            state.channels.append_event(private, direction="inbound", content="主人私聊秘密不得进入群聊", external_message_id="private-secret")
            if unbound:
                state.channels.unbind_identity(str(state.channels.list_identities()[0]["identityId"]))
            event = payload("owner-in-group", image=False, sender=sender)
            event["sender"]["nickname"] = "主人"
            event["senderIsOwner"] = True  # A self-reported flag cannot bind a guest.
            await main._dispatch_channel_payload(
                state.adapter.account_id, event,
                source_adapter=state.adapter,
            )
        finally:
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert len(requests) == 1
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, "100", chat_type="group")
    inbound = next(message for message in state.sessions.list_messages(thread.session_key) if message.role == "user")
    assert inbound.metadata["senderIsOwner"] is owner
    system = "\n".join(message["content"] for message in requests[0] if message["role"] == "system")
    assert f'当前发送者 ID："{sender}"' in system
    assert "当前发送者身份：" + ("已配对主人" if owner else "未配对成员") in system
    assert "主人私聊秘密不得进入群聊" not in str(requests)


@pytest.mark.parametrize("quoted_sender, quote_group, expected", [
    ("11", 100, "已配对主人"),
    ("99", 100, "角色本人"),
    ("33", 100, "未配对成员"),
    ("11", 200, "未知发送者"),
])
def test_group_quote_identity_does_not_grant_quoting_guest_owner_tools(
    meme_turn, monkeypatch, quoted_sender, quote_group, expected,
):
    state, requests, offered = meme_turn, [], []
    original_transport = state.adapter._transport

    async def transport(operation, params):
        if operation == "get_msg":
            return {"data": {"group_id": quote_group, "message_type": "group",
                "sender": {"user_id": quoted_sender, "nickname": "主人"},
                "message": [{"type": "text", "data": {"text": "明天讨论旅行"}}]}}
        return await original_transport(operation, params)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            offered.extend(tool["function"]["name"] for tool in tools)
            yield ModelTextDelta("收到")

    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(main, "model_adapter", Model())

    async def scenario():
        try:
            event = payload("quote-identity", image=False, sender="22")
            event["message"].insert(0, {"type": "reply", "data": {"id": "quoted"}})
            event["replyToSenderIsOwner"] = True
            await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert len(requests) == 1
    system = "\n".join(message["content"] for message in requests[0] if message["role"] == "system")
    assert "当前发送者身份：未配对成员" in system
    assert f"被引用发送者身份：{expected}" in system
    if expected != "未知发送者":
        current = next(message["content"] for message in reversed(requests[0]) if message["role"] == "user")
        assert f"引用 {expected}" in current
    assert "chat_meme_save" not in offered
