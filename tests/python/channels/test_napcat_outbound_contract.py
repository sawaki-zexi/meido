from __future__ import annotations

import asyncio
import base64

from backend.app.channels import DeliveryStatus, NapCatAdapter, NapCatConfig, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.base import PlatformRejectedError
from backend.app.channels.models import Attachment


def target(adapter, kind=TargetKind.GROUP):
    item = target_from_chat(account_id=adapter.account_id, chat_id="456", kind=kind)
    adapter.remember_target(item)
    return item


def test_napcat_discovers_existing_friends_without_prior_message():
    async def run():
        calls = []
        async def transport(operation, params):
            calls.append((operation, params))
            if operation == "get_friend_list":
                return {"items": [{"user_id": 99, "nickname": "好友", "remark": "朋友备注"}]}
            if operation == "get_group_list":
                return {"items": []}
            return {"message_id": 100}
        adapter = NapCatAdapter(NapCatConfig(account_id="qq"), transport=transport)
        friends = await adapter.list_targets()
        assert len(friends) == 1 and friends[0].kind is TargetKind.PRIVATE
        assert friends[0].display_name == "朋友备注"
        result = await adapter.send(OutboundMessage(friends[0], "你好"))
        assert result.status is DeliveryStatus.SENT
        assert calls[-1] == ("send_private_msg", {"user_id": 99, "message": "你好"})
    asyncio.run(run())


def test_napcat_local_images_are_inline_and_unsupported_files_reject_before_sending(tmp_path):
    async def run():
        calls = []
        async def transport(operation, params):
            calls.append((operation, params))
            return {"message_id": 100}
        adapter = NapCatAdapter(NapCatConfig(account_id="qq"), transport=transport)
        destination = target(adapter)
        photo = tmp_path / "photo.png"
        photo.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")
        result = await adapter.send(OutboundMessage(destination, "图", attachments=(Attachment("image", str(photo)),)))
        assert result.status is DeliveryStatus.SENT
        image = calls[0][1]["message"][-1]
        assert image["type"] == "image"
        assert base64.b64decode(image["data"]["file"].removeprefix("base64://")) == photo.read_bytes()
        calls.clear()
        failed = await adapter.send(OutboundMessage(destination, "不发", attachments=(Attachment("image", str(photo)), Attachment("file", str(photo)))))
        assert failed.status is DeliveryStatus.FAILED and calls == []
    asyncio.run(run())


def test_napcat_long_text_uses_upstream_paragraph_nodes_and_separate_group_mentions():
    async def run():
        calls = []
        async def transport(operation, params):
            calls.append((operation, params))
            if operation == "get_login_info":
                return {"user_id": 123, "nickname": "角色QQ"}
            return {"message_id": 100 + len(calls)}
        adapter = NapCatAdapter(NapCatConfig(account_id="qq"), transport=transport)
        destination = target(adapter)
        text = "第一段" + "字" * 300 + "\n\n第二段"
        result = await adapter.send(OutboundMessage(destination, text, metadata={"mentionIds": ["99"]}))
        assert result.status is DeliveryStatus.SENT
        assert [operation for operation, _ in calls] == ["get_login_info", "send_group_msg", "send_group_forward_msg"]
        assert calls[1][1]["message"] == [{"type": "at", "data": {"qq": "99"}}]
        nodes = calls[2][1]["messages"]
        assert len(nodes) == 2
        assert nodes[0]["data"]["user_id"] == 123
        assert nodes[0]["data"]["nickname"] == "角色QQ"
        assert nodes[1]["data"]["content"] == [{"type": "text", "data": {"text": "第二段"}}]
    asyncio.run(run())


def test_napcat_many_lines_trigger_forward_and_short_text_keeps_native_send():
    async def run():
        calls = []
        async def transport(operation, params):
            calls.append((operation, params))
            return {"user_id": 123, "nickname": "QQ"} if operation == "get_login_info" else {"message_id": 100}
        adapter = NapCatAdapter(NapCatConfig(account_id="qq"), transport=transport)
        destination = target(adapter, TargetKind.PRIVATE)
        assert (await adapter.send(OutboundMessage(destination, "字" * 300))).status is DeliveryStatus.SENT
        assert calls[-1][0] == "send_private_msg"
        assert (await adapter.send(OutboundMessage(destination, "\n".join(["行"] * 11)))).status is DeliveryStatus.SENT
        assert calls[-1][0] == "send_private_forward_msg"
    asyncio.run(run())


def test_napcat_missing_receipt_and_partial_forward_are_uncertain():
    async def run():
        async def missing_receipt(_operation, _params):
            return {"res_id": "forward-reference-is-not-message-receipt"}
        adapter = NapCatAdapter(NapCatConfig(account_id="qq"), transport=missing_receipt)
        assert (await adapter.send(OutboundMessage(target(adapter), "消息"))).status is DeliveryStatus.UNCERTAIN
        calls = []
        async def partial(operation, params):
            calls.append(operation)
            if operation == "get_login_info":
                return {"user_id": 123}
            if operation == "send_group_forward_msg":
                raise PlatformRejectedError("拒绝后续合并转发")
            return {"message_id": 100}
        adapter = NapCatAdapter(NapCatConfig(account_id="qq"), transport=partial)
        result = await adapter.send(OutboundMessage(target(adapter), "字" * 301, metadata={"mentionIds": ["99"]}))
        assert result.status is DeliveryStatus.FAILED
        assert result.platform_message_id == '100'
        assert [part['status'] for part in result.raw['parts']] == ['sent', 'failed']
        assert result.raw['deliveredContent'] == ''
        assert calls == ["get_login_info", "send_group_msg", "send_group_forward_msg"]
    asyncio.run(run())


def test_napcat_real_flattened_reply_rpc_triggers_only_same_group_bot_quote():
    async def scenario():
        async def transport(operation, params):
            assert operation == "get_msg" and str(params["message_id"]) == "100"
            return {"message_id": 100, "group_id": 456, "sender": {"user_id": 123, "nickname": "bot"}, "message": [{"type": "text", "data": {"text": "quoted bot"}}]}
        adapter = NapCatAdapter(NapCatConfig(account_id="qq"), transport=transport)
        payload = {"post_type": "message", "message_type": "group", "self_id": 123, "group_id": 456, "user_id": 99, "message_id": 2, "message": [{"type": "reply", "data": {"id": "100"}}, {"type": "text", "data": {"text": "follow up"}}]}
        event = await adapter.accept_event(payload)
        assert event.triggered and event.reply_to_sender_id == "123"
        enriched = await adapter.enrich_reply(event)
        assert enriched.metadata["replyToContent"] == "quoted bot"
        payload["group_id"] = 999
        payload["message_id"] = 3
        foreign = await adapter.accept_event(payload)
        assert not foreign.triggered
    asyncio.run(scenario())
