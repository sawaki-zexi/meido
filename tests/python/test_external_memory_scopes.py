import asyncio
import pytest

from backend.app.channels.consolidation import ChannelMemoryMaintenance
from backend.app.channels.group_tools import MultichannelGroupRepository
from test_channel_api import channel_api as channel_api
from test_external_memory_api import memory_url


@pytest.mark.parametrize("channel", ["qqbot", "feishu"])
@pytest.mark.parametrize("same_app", [False, True])
def test_application_profiles_share_only_proven_application_identity(channel_api, channel, same_app):
    role, channels, _, client = channel_api
    snapshots = []
    groups = MultichannelGroupRepository(channels)
    async def summarize(thread, messages, context, profiles):
        snapshots.append(profiles["open-id"]["profile"])
        return {"members": [{"senderId": "open-id", "profile": "喜欢书" if thread.account_id == "first" else "喜欢茶"}]}
    worker = ChannelMemoryMaintenance(lambda: channels, lambda: groups, summarize, lambda *a: None)
    result = []
    for account_id, app_id in (("first", "application-a"), ("second", "application-a" if same_app else "application-b")):
        channels.register_channel_account(role.id, channel, account_id=account_id, platform_id=app_id)
        thread = channels.ensure_channel_thread(role.id, channel, account_id, "group", "group")
        for index in range(18):
            channels.append_event(thread, direction="inbound", content="真实发言", external_message_id=str(index),
                metadata={"senderId": "open-id", "senderName": "同名"})
        assert asyncio.run(worker.consolidate(thread.thread_id, listening=False))
        url = memory_url(role.id, thread.thread_id)
        result.append((url, client.get(url).json()["members"][0]))
        channels.delete_account(account_id)
    assert snapshots == ["", "喜欢书" if same_app else ""]
    assert (result[0][1]["key"] == result[1][1]["key"]) is same_app
    old = client.get(result[0][0] + "/members/" + result[0][1]["key"]).json()
    assert old["readOnly"] and old["profile"] == ("喜欢茶" if same_app else "喜欢书")
    assert client.put(result[0][0] + "/note", json={"note": "不可改历史", "expectedRevision": 1}).status_code == 403
    if not same_app:
        found = groups.find_members(role.id, name="同名", channel=channel)
        assert {item["member_scope"] for item in found} == {"application:application-a", "application:application-b"}
        lookup = groups.member_profile(role.id, channel, "open-id")
        assert lookup["ambiguous"] and lookup["sources"] == []
        selected = groups.member_profile(role.id, channel, "open-id", member_scope="application:application-a")
        assert [item["profile"] for item in selected["sources"]] == ["喜欢书"]


def test_member_api_rejects_unobserved_thread_other_role_and_bot_self(channel_api):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "qq", account_id="qq", platform_id="99")
    known = channels.ensure_channel_thread(role.id, "qq", "qq", "one", "group")
    unobserved = channels.ensure_channel_thread(role.id, "qq", "qq", "two", "group")
    for index in range(18):
        channels.append_event(known, direction="inbound", content="hi", external_message_id=str(index),
            metadata={"senderId": "22" if index % 2 else "99"})
    async def summarize(thread, messages, context, profiles):
        assert "99" not in profiles
        return {"members": [{"senderId": "22", "profile": "成员"}, {"senderId": "99", "profile": "角色自身"}]}
    worker = ChannelMemoryMaintenance(lambda: channels, lambda: MultichannelGroupRepository(channels), summarize, lambda *a: None)
    assert asyncio.run(worker.consolidate(known.thread_id, listening=False))
    member = client.get(memory_url(role.id, known.thread_id)).json()["members"][0]
    assert member["senderId"] == "22"
    wrong = memory_url(role.id, unobserved.thread_id) + "/members/" + member["key"]
    assert client.get(wrong).status_code == 404
    assert client.put(wrong, json={"profile": "forged", "shorthand": "", "expectedRevision": member["revision"]}).status_code == 404
    assert client.get(memory_url("foreign", known.thread_id)).status_code == 404


def test_owner_private_room_is_excluded_when_chat_id_differs_from_sender_and_rebinding_invalidates_page(channel_api):
    role, channels, _, client = channel_api
    channels.register_channel_account(role.id, "feishu", account_id="app", platform_id="verified")
    thread = channels.ensure_channel_thread(role.id, "feishu", "app", "private-room", "private")
    url = memory_url(role.id, thread.thread_id)
    before = client.get(url).json()
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id=role.id, channel="feishu", scope="account",
        platform_user_id="sender-open-id", account_id="app", chat_id="private-room")
    assert client.get(url).status_code == 403
    assert not channels.public_thread_payload(thread)["externalMemoryAvailable"]
    channels.unbind_identity(channels.list_identities()[0]["identityId"])
    assert client.put(url + "/note", json={"note": "旧页面仍不可提交", "expectedRevision": before["revision"]}).status_code == 409
