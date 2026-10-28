from __future__ import annotations

import asyncio
import base64

import pytest

from backend.app.channels import (
    FeishuAdapter,
    FeishuConfig,
    NapCatAdapter,
    NapCatConfig,
    OutboundMessage,
    QQBotAdapter,
    QQBotConfig,
    Target,
    TargetKind,
    UnsupportedTargetError,
)
from backend.app.channels.credentials import NamespacedCredentials, credential_key
from backend.app.channels.models import Attachment, ChatType, DeliveryStatus, target_from_chat


def test_napcat_group_event_is_normalized_for_host_listener_policy() -> None:
    adapter = NapCatAdapter(NapCatConfig(account_id="123", group_listening=False))
    payload = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 123,
        "group_id": 456,
        "message_id": 1,
        "user_id": 99,
        "sender": {"user_id": 99, "nickname": "member"},
        "message": [{"type": "text", "data": {"text": "hello"}}],
    }
    untriggered = adapter.parse_event(payload)
    assert untriggered is not None
    assert untriggered.triggered is False
    payload["message"] = [
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": "hello"}},
    ]
    event = adapter.parse_event(payload)
    assert event is not None
    assert event.chat_type is ChatType.GROUP
    assert event.triggered is True
    assert event.mentioned is True


def test_napcat_group_member_target_uses_private_message_shape() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def transport(operation: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((operation, params))
        return {"message_id": 7}

    adapter = NapCatAdapter(NapCatConfig(account_id="123"), transport=transport)
    target = target_from_chat(
        account_id="123",
        chat_id="99",
        kind=TargetKind.GROUP_MEMBER,
        display_name="member",
        group_id="456",
    )
    adapter.remember_target(target)
    result = asyncio.run(adapter.send(OutboundMessage(target, "hello")))

    assert result.status is DeliveryStatus.SENT
    assert calls == [("send_private_msg", {"user_id": 99, "group_id": 456, "message": "hello"})]


def test_napcat_only_role_reply_triggers_group_turn() -> None:
    adapter = NapCatAdapter(NapCatConfig(account_id="123", group_listening=False))
    base = {
        "post_type": "message", "message_type": "group", "self_id": 123,
        "group_id": 456, "user_id": 99, "sender": {"user_id": 99},
    }
    unrelated = {**base, "message_id": 1, "message": [{"type": "reply", "data": {"id": "old", "sender_id": 88}}]}
    unrelated_event = adapter.parse_event(unrelated)
    assert unrelated_event is not None
    assert unrelated_event.triggered is False
    to_role = {**base, "message_id": 2, "message": [{"type": "reply", "data": {"id": "old", "sender_id": 123}}]}
    event = adapter.parse_event(to_role)
    assert event is not None
    assert event.triggered is True
    assert event.reply_to_sender_id == "123"


def test_napcat_cq_payload_parses_structural_mention_and_reply() -> None:
    adapter = NapCatAdapter(NapCatConfig(account_id="123"))
    event = adapter.parse_event({
        "post_type": "message", "message_type": "group", "self_id": 123,
        "group_id": 456, "user_id": 99, "message_id": 3,
        "sender": {"user_id": 99},
        "message": "[CQ:reply,id=2][CQ:at,qq=123] hello",
    })
    assert event is not None
    assert event.triggered is True
    assert event.mentioned_ids == ("123",)


def test_napcat_resolves_reply_target_after_process_restart() -> None:
    calls: list[str] = []

    async def transport(operation: str, _params: dict[str, object]) -> dict[str, object]:
        calls.append(operation)
        return {"message": {"user_id": 123}}

    adapter = NapCatAdapter(NapCatConfig(account_id="account"), transport=transport)
    payload = {
        "post_type": "message", "message_type": "group", "self_id": 123,
        "group_id": 456, "user_id": 99, "message_id": 4,
        "sender": {"user_id": 99}, "message": "[CQ:reply,id=-8] hello",
    }
    event = asyncio.run(adapter.accept_event(payload))
    assert event is not None
    assert event.triggered is True
    assert event.reply_to_sender_id == "123"
    assert calls == ["get_msg"]


def test_napcat_group_listening_emits_non_triggering_event() -> None:
    adapter = NapCatAdapter(NapCatConfig(account_id="123", group_listening=True))
    event = adapter.parse_event({
        "post_type": "message",
        "message_type": "group",
        "self_id": 123,
        "group_id": 456,
        "message_id": 2,
        "user_id": 99,
        "sender": {"user_id": 99},
        "message": [{"type": "text", "data": {"text": "旁听"}}],
    })
    assert event is not None
    assert event.triggered is False


def test_accept_event_deduplicates_platform_event() -> None:
    adapter = QQBotAdapter(QQBotConfig(account_id="app", app_id="app"))
    payload = {
        "t": "C2C_MESSAGE_CREATE",
        "d": {"id": "m-1", "author": {"user_openid": "u-1"}, "content": "hi"},
    }
    first = asyncio.run(adapter.accept_event(payload))
    second = asyncio.run(adapter.accept_event(payload))
    assert first is not None
    assert second is None
    assert first.chat_type is ChatType.PRIVATE


def test_qqbot_rejects_group_target_and_unknown_target() -> None:
    adapter = QQBotAdapter(QQBotConfig(account_id="app", app_id="app"))
    adapter.parse_event({"t": "C2C_MESSAGE_CREATE", "d": {"id": "m-1", "author": {"user_openid": "u-1"}}})
    group = Target("group", TargetKind.GROUP, "group", "app", "g")
    with pytest.raises(UnsupportedTargetError):
        asyncio.run(adapter.send(OutboundMessage(group, "no")))
    unknown = Target("app:private:u-2", TargetKind.PRIVATE, "u-2", "app", "u-2")
    with pytest.raises(UnsupportedTargetError):
        asyncio.run(adapter.send(OutboundMessage(unknown, "no")))


def test_qqbot_streaming_replaces_one_c2c_message_and_finishes_it() -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def transport(operation: str, params):
            calls.append((operation, dict(params)))
            return {"id": "stream-1"}

        adapter = QQBotAdapter(QQBotConfig(account_id="app", app_id="app"), transport=transport)
        adapter.parse_event({
            "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "incoming-1", "author": {"user_openid": "user-1"}, "content": "hi"},
        })
        target = (await adapter.list_targets())[0]
        session = await adapter.stream_start(target, reply_to_message_id="incoming-1")
        await adapter.stream_update(session, "Hello")
        await adapter.stream_update(session, "Hello there")
        result = await adapter.stream_finalize(session, "Hello there!")

        assert result.status is DeliveryStatus.SENT
        assert result.platform_message_id == "stream-1"
        assert [operation for operation, _ in calls] == ["stream_c2c"] * 3
        first, second, final = [params["body"] for _, params in calls]
        assert first == {
            "input_mode": "replace", "input_state": 1, "content_type": "markdown",
            "content_raw": "Hello", "event_id": "incoming-1", "msg_id": "incoming-1",
            "msg_seq": first["msg_seq"], "index": 0,
        }
        assert second["stream_msg_id"] == "stream-1"
        assert second["index"] == 1
        assert second["input_state"] == 1
        assert final["stream_msg_id"] == "stream-1"
        assert final["index"] == 2
        assert final["input_state"] == 10
        assert final["content_raw"] == "Hello there!"
        assert "nonce" not in final
        assert {(body["msg_id"], body["event_id"], body["msg_seq"]) for body in (first, second, final)} == {
            ("incoming-1", "incoming-1", first["msg_seq"]),
        }

    asyncio.run(run())


def test_feishu_attachment_failure_after_text_acceptance_is_uncertain(tmp_path) -> None:
    async def run() -> None:
        calls: list[str] = []

        async def transport(operation: str, _params):
            calls.append(operation)
            if operation == "im.v1.message.create":
                return {"message_id": "text-1"}
            raise ConnectionError("network disconnected")

        path = tmp_path / "attachment.txt"
        path.write_text("file", encoding="utf-8")
        adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"), transport=transport)
        adapter.parse_event({
            "header": {"event_type": "im.message.receive_v1", "event_id": "e-1"},
            "event": {"sender": {"sender_id": {"open_id": "ou-1"}}, "message": {"message_id": "m-1", "chat_id": "oc-1", "chat_type": "p2p", "content": '{"text":"hello"}'}},
        })
        target = (await adapter.list_targets())[0]
        result = await adapter.send(OutboundMessage(target, "caption", attachments=(Attachment("file", str(path)),)))
        assert result.status is DeliveryStatus.UNCERTAIN
        assert result.platform_message_id == "text-1"
        assert calls == ["im.v1.message.create", "im.v1.media.create"]

    asyncio.run(run())


def test_qqbot_stream_rejection_preserves_preview_without_active_fallback() -> None:
    class RejectedRequest(Exception):
        response = type("Response", (), {"status_code": 400})()

    async def run() -> None:
        operations: list[str] = []

        async def transport(operation: str, params):
            operations.append(operation)
            if operation == "stream_c2c" and params["body"]["input_state"] == 10:
                raise RejectedRequest()
            if operation == "stream_c2c":
                return {"id": "stream-1"}
            if operation == "send_c2c":
                return {"id": "plain-1"}
            return {}

        adapter = QQBotAdapter(QQBotConfig(account_id="app", app_id="app"), transport=transport)
        adapter.parse_event({
            "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "incoming-1", "author": {"user_openid": "user-1"}, "content": "hi"},
        })
        target = (await adapter.list_targets())[0]
        session = await adapter.stream_start(target)
        await adapter.stream_update(session, "Preview")
        result = await adapter.stream_finalize(session, "Final")

        assert result.status is DeliveryStatus.FAILED
        assert result.platform_message_id == "stream-1"
        assert operations == ["stream_c2c", "stream_c2c"]
        assert result.raw['partialDelivery'] and result.raw['deliveredContent'] == 'Preview'

    asyncio.run(run())


def test_qqbot_sends_only_verified_local_image_attachments(tmp_path) -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def transport(operation: str, params):
            calls.append((operation, dict(params)))
            if operation == "upload_c2c_file":
                return {"file_info": "file-info-1"}
            return {"id": "message-1"}

        image = tmp_path / "photo.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
        adapter = QQBotAdapter(QQBotConfig(account_id="app", app_id="app", max_attachment_bytes=20), transport=transport)
        adapter.parse_event({
            "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "incoming-1", "author": {"user_openid": "user-1"}, "content": "hi"},
        })
        target = (await adapter.list_targets())[0]
        result = await adapter.send(OutboundMessage(
            target, "caption", attachments=(Attachment("image", str(image), mime_type="image/png"),),
        ))

        assert result.status is DeliveryStatus.SENT
        assert result.platform_message_id == "message-1"
        assert [operation for operation, _ in calls] == ["send_c2c", "upload_c2c_file", "send_c2c_image"]
        upload_body = calls[1][1]["body"]
        assert upload_body["file_type"] == 1
        assert upload_body["srv_send_msg"] is False
        assert base64.b64decode(upload_body["file_data"]) == image.read_bytes()
        assert calls[2][1]["file_info"] == "file-info-1"

        rejected = await adapter.send(OutboundMessage(
            target, "", attachments=(Attachment("image", "https://example.test/photo.png"),),
        ))
        assert rejected.status is DeliveryStatus.FAILED
        assert len(calls) == 3

        invalid = tmp_path / "invalid.png"
        invalid.write_bytes(b"not an image")
        bad_signature = await adapter.send(OutboundMessage(
            target, "", attachments=(Attachment("image", str(invalid)),),
        ))
        assert bad_signature.status is DeliveryStatus.FAILED
        oversized = tmp_path / "large.gif"
        oversized.write_bytes(b"GIF89a" + b"x" * 20)
        too_large = await adapter.send(OutboundMessage(
            target, "", attachments=(Attachment("image", str(oversized)),),
        ))
        assert too_large.status is DeliveryStatus.FAILED
        assert len(calls) == 3

    asyncio.run(run())


def test_qqbot_partial_image_delivery_is_uncertain(tmp_path) -> None:
    async def run() -> None:
        operations: list[str] = []

        async def transport(operation: str, _params):
            operations.append(operation)
            if operation == "send_c2c":
                return {"id": "text-1"}
            raise TimeoutError("image upload timed out")

        image = tmp_path / "photo.gif"
        image.write_bytes(b"GIF89aimage-data")
        adapter = QQBotAdapter(QQBotConfig(account_id="app", app_id="app"), transport=transport)
        adapter.parse_event({
            "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "incoming-1", "author": {"user_openid": "user-1"}, "content": "hi"},
        })
        target = (await adapter.list_targets())[0]
        result = await adapter.send(OutboundMessage(
            target, "caption", attachments=(Attachment("image", str(image), mime_type="image/gif"),),
        ))

        assert result.status is DeliveryStatus.UNCERTAIN
        assert result.platform_message_id == "text-1"
        assert operations == ["send_c2c", "upload_c2c_file"]

    asyncio.run(run())


def test_qqbot_accepts_supported_image_signatures(tmp_path) -> None:
    async def run() -> None:
        async def transport(operation: str, _params):
            return {"file_info": "file-info"} if operation == "upload_c2c_file" else {"id": "sent"}

        adapter = QQBotAdapter(QQBotConfig(account_id="app", app_id="app"), transport=transport)
        adapter.parse_event({
            "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "incoming-1", "author": {"user_openid": "user-1"}, "content": "hi"},
        })
        target = (await adapter.list_targets())[0]
        formats = (
            ("photo.jpg", b"\xff\xd8\xffdata", "image/jpeg"),
            ("photo.webp", b"RIFF\x04\x00\x00\x00WEBPdata", "image/webp"),
            ("photo.gif", b"GIF87adata", "image/gif"),
        )
        for name, data, mime_type in formats:
            path = tmp_path / name
            path.write_bytes(data)
            result = await adapter.send(OutboundMessage(
                target, "", attachments=(Attachment("image", str(path), mime_type=mime_type),),
            ))
            assert result.status is DeliveryStatus.SENT

    asyncio.run(run())


def test_feishu_private_event_and_group_rejection() -> None:
    adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"))
    event = adapter.parse_event({
        "header": {"event_type": "im.message.receive_v1", "event_id": "e-1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou-1"}, "name": "Alice"},
            "message": {"message_id": "m-1", "chat_id": "oc-1", "chat_type": "p2p", "content": '{"text":"hello"}'},
        },
    })
    assert event is not None
    assert event.content == "hello"
    assert event.sender_id == "ou-1"
    assert adapter.parse_event({
        "header": {"event_type": "im.message.receive_v1", "event_id": "e-2"},
        "event": {"sender": {"sender_id": {"open_id": "ou-1"}}, "message": {"message_id": "m-2", "chat_id": "oc-g", "chat_type": "group", "content": '{"text":"group"}'}},
    }) is None


def test_feishu_webhook_challenge_is_not_a_chat_event() -> None:
    adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app", verification_token_key="verify"))
    assert adapter.webhook_challenge({"type": "url_verification", "token": "verify", "challenge": "c"}, expected_token="verify") == {"challenge": "c"}
    with pytest.raises(Exception):
        adapter.webhook_challenge({"type": "url_verification", "token": "bad", "challenge": "c"}, expected_token="verify")


def test_delivery_timeout_is_uncertain() -> None:
    async def transport(_operation: str, _params: dict[str, object]) -> dict[str, object]:
        raise TimeoutError("request timed out after write")

    adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"), transport=transport)
    event = adapter.parse_event({
        "header": {"event_type": "im.message.receive_v1", "event_id": "e-1"},
        "event": {"sender": {"sender_id": {"open_id": "ou-1"}}, "message": {"message_id": "m-1", "chat_id": "oc-1", "chat_type": "p2p", "content": '{"text":"hello"}'}},
    })
    assert event is not None
    target = next(iter(asyncio.run(adapter.list_targets())))
    result = asyncio.run(adapter.send(OutboundMessage(target, "reply")))
    assert result.status is DeliveryStatus.UNCERTAIN


def test_delivery_connection_and_protocol_errors_are_uncertain_but_platform_rejection_is_failed() -> None:
    class Rejected(Exception):
        response = type("Response", (), {"status_code": 403})()

    async def run() -> None:
        for error in (ConnectionError("connection reset"), ValueError("protocol response incomplete"), RuntimeError("unknown transport failure")):
            async def transport(_operation: str, _params: dict[str, object], error=error):
                raise error
            adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"), transport=transport)
            event = adapter.parse_event({
                "header": {"event_type": "im.message.receive_v1", "event_id": "e-1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou-1"}},
                    "message": {"message_id": "m-1", "chat_id": "oc-1", "chat_type": "p2p", "content": '{"text":"hello"}'},
                },
            })
            assert event is not None
            target = (await adapter.list_targets())[0]
            assert (await adapter.send(OutboundMessage(target, "reply"))).status is DeliveryStatus.UNCERTAIN

        async def rejected(_operation: str, _params: dict[str, object]):
            raise Rejected()
        adapter = FeishuAdapter(FeishuConfig(account_id="b", app_id="app"), transport=rejected)
        event = adapter.parse_event({
            "header": {"event_type": "im.message.receive_v1", "event_id": "e-2"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou-2"}},
                "message": {"message_id": "m-2", "chat_id": "oc-2", "chat_type": "p2p", "content": '{"text":"hello"}'},
            },
        })
        assert event is not None
        target = (await adapter.list_targets())[0]
        assert (await adapter.send(OutboundMessage(target, "reply"))).status is DeliveryStatus.FAILED

    asyncio.run(run())


def test_blocked_sender_aliases_are_case_insensitive_and_empty_alias_is_safe() -> None:
    feishu = FeishuAdapter(FeishuConfig(account_id="a", app_id="app", blocked_sender_ids={"Alice"}))
    assert feishu.parse_event({
        "header": {"event_type": "im.message.receive_v1", "event_id": "e-1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou-1"}, "name": "alice"},
            "message": {"message_id": "m-1", "chat_id": "oc-1", "chat_type": "p2p", "content": '{"text":"hello"}'},
        },
    }) is None
    napcat = NapCatAdapter(NapCatConfig(account_id="a", blocked_sender_ids={"ALICE"}))
    assert napcat.parse_event({"post_type": "message", "message_type": "private", "user_id": "u", "message_id": "m", "sender": {"user_id": "u", "nickname": "Alice"}, "message": []}) is None
    qqbot = QQBotAdapter(QQBotConfig(account_id="a", app_id="app", blocked_sender_ids={"ALICE"}))
    assert qqbot.parse_event({"t": "C2C_MESSAGE_CREATE", "d": {"id": "m", "author": {"user_openid": "u", "username": "alice"}}}) is None


def test_credentials_are_namespaced_per_account() -> None:
    class Store:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def get(self, key: str) -> str:
            return self.values.get(key, "")

        def set(self, key: str, value: str) -> None:
            self.values[key] = value

    store = Store()
    first = NamespacedCredentials(store, channel="qqbot", account_id="app-1")
    second = NamespacedCredentials(store, channel="qqbot", account_id="app-2")
    first.set("app_secret", "secret-1")
    second.set("app_secret", "secret-2")
    assert first.get("app_secret") == "secret-1"
    assert second.get("app_secret") == "secret-2"
    assert credential_key("qqbot", "app-1", "app_secret") != credential_key("qqbot", "app-2", "app_secret")
