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
    private = state.channels.ensure_channel_thread(state.role.id, "qq", state.adapter.account_id, "11", "private")
    state.channels.append_event(private, direction="inbound", content="主人私聊秘密不得进入群聊", external_message_id="private-secret")
    if unbound:
        state.channels.unbind_identity(str(state.channels.list_identities()[0]["identityId"]))

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("收到")

    monkeypatch.setattr(main, "model_adapter", Model())

    async def scenario():
        try:
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
