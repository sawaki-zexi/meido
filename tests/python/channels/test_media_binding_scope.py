"""Only the original sender's binding can revoke an admitted media source."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.channels import ChannelHost, ChatType, InboundEvent, media
from backend.app.model_adapter import ModelTextDelta
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from test_channel_identity_parity import MemoryCredentials
from test_meme_turn import meme_turn, payload


def pair(channels, *, channel="qq", sender="11", scope="platform", account="receiver", role="role"):
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id=role, channel=channel, scope=scope,
        platform_user_id=sender, account_id=account, chat_id=sender)


@pytest.mark.parametrize("quoted", [False, True], ids=["current", "quoted"])
@pytest.mark.parametrize("sender", ["11", "22"], ids=["owner", "guest"])
def test_unrelated_pairing_during_file_download_preserves_original_turn(meme_turn, monkeypatch, quoted, sender):
    state, requests, downloads, exposed = meme_turn, [], [], []
    original = state.adapter._transport

    def file_segment():
        return {"type": "file", "data": {"name": "note.txt", "file_id": "file-id"}}

    async def transport(operation, params):
        if operation == "get_msg":
            return {"data": {"group_id": 100, "sender": {"user_id": 99}, "message": [file_segment()]}}
        if operation == "get_group_file_url":
            return {"data": {"url": "https://qq.test/file"}}
        return await original(operation, params)

    async def download(url, *, max_bytes, resolved):
        downloads.append(url)
        pair(state.channels, sender="33", account=state.adapter.account_id, role=state.role.id)
        return b"AUTHORIZED_FILE_BODY", "text/plain"

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            exposed.append({tool["function"]["name"] for tool in tools})
            yield ModelTextDelta("文件已收到")

    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_file", download)
    monkeypatch.setattr(main, "model_adapter", Model())

    async def scenario():
        event = payload("unrelated-pair", quoted=quoted, image=False, sender=sender)
        if not quoted:
            event["message"].append(file_segment())
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert downloads == ["https://qq.test/file"]
    assert len(requests) == 1 and "AUTHORIZED_FILE_BODY" in str(requests[0])
    if sender == "22":
        assert not {"chat_meme_save", "chat_meme_send", "memorize", "shell", "read_file"} & exposed[0]
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, "100", chat_type="group")
    messages = state.sessions.list_messages(thread.session_key)
    user = next(item for item in messages if item.role == "user")
    assert user.metadata["channelTurnState"] == "completed"
    assert len(user.metadata["quotedAttachments" if quoted else "attachments"]) == 1
    reply = next(item for item in messages if item.role == "assistant")
    assert state.sessions.get_run(reply.runId).status == "completed"
    assert reply.metadata["deliveryStatus"] == "sent"


@pytest.mark.parametrize("channel,scope", [("qq", "platform"), ("telegram", "platform"), ("feishu", "account")])
def test_media_binding_uses_canonical_scope_and_account(tmp_path, channel, scope):
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
    for role, account in (("role", "receiver"), ("other-role", "other")):
        if channel == "telegram":
            channels.save_account(role, account_id=account, token="synthetic-token", enabled=True)
        else:
            channels.register_channel_account(role, channel, account_id=account, enabled=True)
    host = ChannelHost(channels)
    host.register(SimpleNamespace(account_id="receiver", channel=channel))
    event = InboundEvent(channel, "receiver", "event", "message", "11", ChatType.PRIVATE, "11",
        metadata={"hostMediaSource": host.capture_media_source("receiver")})
    authorized = host.media_authorization("role", event)
    assert authorized()

    # Wrong scope never changes this sender's applicable identity.
    pair(channels, channel=channel, scope="account" if scope == "platform" else "platform")
    assert authorized()
    if scope == "account":
        pair(channels, channel=channel, scope=scope, account="other", role="other-role")
        assert authorized(), "an application-scoped identity must not cross accounts"
    pair(channels, channel=channel, scope=scope,
        account="other" if scope == "platform" else "receiver",
        role="other-role" if scope == "platform" else "role")
    assert not authorized(), "a platform binding applies across accounts, an account binding only locally"


@pytest.mark.parametrize("change", ["unbind", "repair", "repeat-pair", "promote"])
def test_original_sender_binding_change_never_renews_old_media(tmp_path, change):
    sessions = SessionStore(tmp_path / "sessions.db")
    channels = MultichannelStore(sessions.database_path, sessions)
    channels.register_channel_account("role", "qq", account_id="receiver", enabled=True)
    host = ChannelHost(channels)
    host.register(SimpleNamespace(account_id="receiver", channel="qq"))
    if change != "promote":
        pair(channels)
    event = InboundEvent("qq", "receiver", "event", "message", "11", ChatType.PRIVATE, "11",
        metadata={"hostMediaSource": host.capture_media_source("receiver")})
    authorized = host.media_authorization("role", event)
    assert authorized()
    if change in {"unbind", "repair"}:
        channels.unbind_identity(channels.list_identities()[0]["identityId"])
    if change != "unbind":
        pair(channels)
    assert not authorized()
