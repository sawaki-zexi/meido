import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime

import pytest

from backend.app import main
from backend.app.models import RoleInput, RoleProfile
from test_channel_api import channel_api as channel_api


@asynccontextmanager
async def channel_events(role_id):
    """Read the public SSE route without buffering an infinite response."""
    started = asyncio.Event()
    bodies = asyncio.Queue()
    disconnected = asyncio.Event()
    path = f"/api/roles/{role_id}/channels/events"

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(event):
        if event["type"] == "http.response.start":
            assert event["status"] == 200
            started.set()
        elif event["type"] == "http.response.body" and event.get("body"):
            await bodies.put(event["body"])

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": [],
             "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 80)}
    running = asyncio.create_task(main.app(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)

        async def next_event():
            body = await asyncio.wait_for(bodies.get(), timeout=5)
            return json.loads(next(line[6:] for line in body.decode().splitlines() if line.startswith("data: ")))

        yield next_event
    finally:
        disconnected.set()
        await asyncio.wait_for(running, timeout=5)


@pytest.mark.parametrize("channel", ["telegram", "qq", "qqbot", "feishu"])
def test_live_message_uses_current_owner_projection_and_complete_thread(channel_api, channel):
    role, channels, _, client = channel_api
    account_id = f"{channel}-events"
    if channel == "telegram":
        account = channels.save_account(role.id, account_id=account_id, token="fixture-token", enabled=True)
    else:
        account = channels.register_channel_account(role.id, channel, account_id=account_id, enabled=True)
    thread = channels.ensure_channel_thread(role.id, channel, account.account_id, "42", "private", title="访客")

    async def scenario():
        async with channel_events(role.id) as next_event:
            channels.append_event(thread, direction="inbound", content="新的发言", external_message_id="event-1",
                                  metadata={"senderId": "42", "senderName": "Alice", "senderIsOwner": True})
            payload = await next_event()
            assert payload["message"]["metadata"]["senderIsOwner"] is False
            assert "senderAvatarUrl" in payload["message"]["metadata"]
            assert "avatarUrl" in payload["thread"]
            assert payload["thread"]["latestMessage"]["content"] == "新的发言"
            assert datetime.fromisoformat(payload["thread"]["latestMessage"]["createdAt"]) == datetime.fromisoformat(payload["message"]["createdAt"])
            return payload

    payload = asyncio.run(scenario())
    history = client.get(f"/api/roles/{role.id}/channels/threads/{thread.thread_id}/messages").json()
    assert payload["message"] == history["messages"][0]


def test_queued_live_message_rechecks_binding_when_read(channel_api):
    role, channels, _, _ = channel_api
    account = channels.save_account(role.id, account_id="queued-events", token="fixture-token", enabled=True)
    thread = channels.ensure_channel_thread(role.id, "telegram", account.account_id, "42", "private")
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id=role.id, channel="telegram", scope="platform",
                                         platform_user_id="42", account_id=account.account_id, chat_id="42")

    async def scenario():
        async with channel_events(role.id) as next_event:
            channels.append_event(thread, direction="inbound", content="排队中的消息", external_message_id="queued-1",
                                  metadata={"senderId": "42", "senderIsOwner": True})
            channels.unbind_identity(channels.list_identities()[0]["identityId"])
            payload = await next_event()
            assert payload["message"]["metadata"]["senderIsOwner"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("channel", "scope"),
    [("telegram", "platform"), ("qq", "platform"), ("qqbot", "account"), ("feishu", "account")],
)
def test_http_conversation_projection_covers_channel_identity_scope(channel_api, channel, scope):
    role, channels, _, client = channel_api
    account_id = f"{channel}-projection"
    display_name = f"{channel} Bot"
    if channel == "telegram":
        account = channels.save_account(
            role.id, account_id=account_id, token="fixture-token", enabled=True, display_name=display_name,
        )
        channels.set_platform_identity(account_id, f"{channel}-bot")
    else:
        account = channels.register_channel_account(
            role.id, channel, account_id=account_id, enabled=True,
            display_name=display_name, platform_id=f"{channel}-bot",
        )
    chat_id = f"{channel}-chat"
    owner_id = f"{channel}-owner"
    pairing_code = channels.create_pairing_code()
    assert channels.consume_pairing_code(
        pairing_code,
        role_id=role.id,
        channel=channel,
        scope=scope,
        platform_user_id=owner_id,
        account_id=account.account_id,
        chat_id=chat_id,
    )
    thread = channels.ensure_channel_thread(role.id, channel, account.account_id, chat_id, "private", title=f"{channel} 私聊")
    channels.append_event(
        thread,
        direction="inbound",
        content="主人原始消息",
        external_message_id=f"{channel}-inbound",
        metadata={
            "senderId": owner_id,
            "senderName": "主人",
            "senderIsOwner": False,
            "replyToContent": "被引用的消息",
            "replyToSenderName": "Alice",
            "mentionedIds": ["member-1"],
            "attachments": [{
                "id": "a" * 32,
                "name": "截图.png",
                "mediaType": "image/png",
                "size": 5,
                "kind": "image",
                "path": "private/local/path.png",
                "text": "private extracted text",
            }],
        },
    )
    channels.append_event(
        thread,
        direction="outbound",
        content="结果未确认的角色回复",
        external_message_id=f"{channel}-outbound",
        role="assistant",
        metadata={"deliveryStatus": "uncertain", "platformMessageId": f"platform-{channel}-2"},
    )

    listed = client.get(f"/api/roles/{role.id}/channels/threads", params={"networkOnly": "true"})
    assert listed.status_code == 200
    row = next(item for item in listed.json()["threads"] if item["threadId"] == thread.thread_id)
    assert row["channel"] == channel
    assert row["viaAccount"]["accountId"] == account_id
    assert row["viaAccount"]["displayName"] == display_name
    assert row["latestMessage"]["content"] == "结果未确认的角色回复"

    endpoint = f"/api/roles/{role.id}/channels/threads/{thread.thread_id}/messages"
    newest = client.get(endpoint, params={"networkOnly": "true", "limit": 1})
    assert newest.status_code == 200
    page = newest.json()
    assert page["hasMore"] is True
    assert len(page["messages"]) == 1
    assert page["messages"][0]["content"] == "结果未确认的角色回复"
    assert page["messages"][0]["metadata"]["deliveryStatus"] == "uncertain"
    assert page["messages"][0]["metadata"]["viaAccount"]["accountId"] == account_id
    older = client.get(endpoint, params={
        "networkOnly": "true", "limit": 1, "before": page["nextBefore"],
    })
    assert older.status_code == 200
    original = older.json()["messages"][0]
    assert original["content"] == "主人原始消息"
    assert original["metadata"]["senderIsOwner"] is True
    assert original["metadata"]["replyToContent"] == "被引用的消息"
    assert original["metadata"]["mentionedIds"] == ["member-1"]
    attachment = original["metadata"]["attachments"][0]
    assert attachment["id"] == "a" * 32
    assert attachment["kind"] == "image"
    assert "path" not in attachment and "text" not in attachment


def test_http_conversation_read_rejects_another_roles_thread(channel_api):
    role, channels, _, client = channel_api
    account = channels.save_account(role.id, account_id="scope-source", token="fixture-token", enabled=True)
    thread = channels.ensure_channel_thread(role.id, "telegram", account.account_id, "42", "private")
    channels.append_event(thread, direction="inbound", content="角色私聊内容", external_message_id="scope-message")
    foreign_role = main.store.create(RoleInput(name="另一个角色", profile=RoleProfile(profile="第二角色")))

    response = client.get(
        f"/api/roles/{foreign_role.id}/channels/threads/{thread.thread_id}/messages",
        params={"networkOnly": "true"},
    )

    assert response.status_code == 404
    assert "角色私聊内容" not in response.text


def test_http_listening_history_uses_independent_group_cursor_without_creating_transcript_messages(channel_api):
    role, channels, _, client = channel_api
    account = channels.register_channel_account(role.id, "qq", account_id="qq-listening-api", enabled=True)
    thread = channels.ensure_channel_thread(role.id, "qq", account.account_id, "group-42", "group", title="旁听群")
    channels.set_group_listening(role.id, account.account_id, thread.chat_id, True)
    for index in range(3):
        channels.listening.hear(
            thread,
            f"旁听消息 {index + 1}",
            f"heard-{index + 1}",
            {"senderId": f"member-{index + 1}", "senderName": f"成员 {index + 1}"},
        )

    endpoint = f"/api/roles/{role.id}/channels/threads/{thread.thread_id}/listening/messages"
    newest = client.get(endpoint, params={"limit": 2})
    assert newest.status_code == 200
    page = newest.json()
    assert [item["content"] for item in page["messages"]] == ["旁听消息 2", "旁听消息 3"]
    assert page["hasMore"] is True
    assert page["nextBefore"] == 2
    assert all(item["metadata"]["listening"] is True for item in page["messages"])
    older = client.get(endpoint, params={"limit": 2, "before": page["nextBefore"]})
    assert [item["content"] for item in older.json()["messages"]] == ["旁听消息 1"]
    assert channels.sessions.list_messages(thread.session_key) == []


def test_http_conversation_keeps_account_snapshot_after_account_removal(channel_api):
    role, channels, _, client = channel_api
    account = channels.save_account(
        role.id, account_id="telegram-removed-history", token="fixture-token", enabled=True,
        display_name="旧 Telegram Bot",
    )
    thread = channels.ensure_channel_thread(
        role.id, "telegram", account.account_id, "42", "private", title="历史联系人",
    )
    channels.append_event(
        thread,
        direction="inbound",
        content="账号移除前的消息",
        external_message_id="removed-history-message",
        metadata={"senderId": "contact-42", "senderName": "联系人"},
    )

    removed = client.delete(f"/api/roles/{role.id}/channels/accounts/{account.account_id}")
    assert removed.status_code == 204
    listed = client.get(f"/api/roles/{role.id}/channels/threads", params={"networkOnly": "true"})
    row = next(item for item in listed.json()["threads"] if item["threadId"] == thread.thread_id)
    assert row["accountRemoved"] is True
    assert row["viaAccount"]["accountId"] == account.account_id
    assert row["viaAccount"]["displayName"] == "旧 Telegram Bot"
    history = client.get(
        f"/api/roles/{role.id}/channels/threads/{thread.thread_id}/messages",
        params={"networkOnly": "true"},
    )
    assert history.status_code == 200
    assert history.json()["messages"][0]["content"] == "账号移除前的消息"
    assert history.json()["messages"][0]["metadata"]["viaAccount"]["displayName"] == "旧 Telegram Bot"
