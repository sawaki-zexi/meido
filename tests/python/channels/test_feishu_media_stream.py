from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.app.channels.feishu import FeishuAdapter, FeishuConfig
from backend.app.channels.models import Attachment, ChatType, OutboundMessage
from backend.app.channels.base import ChannelAdapterError, PlatformRejectedError
from backend.app.channels.models import DeliveryStatus
from backend.app.channels.transports import FeishuWebhookTransport


def _payload(message_type: str, content: dict[str, object], *, message_id: str = "m-1"):
    return {
        "header": {"event_type": "im.message.receive_v1", "event_id": f"e-{message_id}"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou-1"}, "name": "Alice"},
            "message": {
                "message_id": message_id,
                "chat_id": "oc-1",
                "chat_type": "p2p",
                "message_type": message_type,
                "content": json.dumps(content),
            },
        },
    }


def test_feishu_image_and_post_media_are_resolved_with_bounded_downloads() -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def transport(operation: str, params):
            calls.append((operation, dict(params)))
            return b"image-bytes"

        adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app", max_attachment_bytes=32), transport=transport)
        image = adapter.parse_event(_payload("image", {"image_key": "img-key"}))
        assert image is not None
        assert image.chat_type is ChatType.PRIVATE
        assert image.content == "[图片]"
        media = await adapter.download_media(image)
        assert media[0].data == b"image-bytes"
        assert calls[0][0] == "im.v1.message.resource.get"
        assert calls[0][1]["message_id"] == "m-1"
        assert calls[0][1]["file_key"] == "img-key"
        assert calls[0][1]["max_bytes"] == 32

        post = adapter.parse_event(_payload("post", {
            "zh_cn": {"title": "title", "content": [[
                {"tag": "text", "text": "body"},
                {"tag": "img", "image_key": "post-image"},
            ]]},
        }, message_id="m-2"))
        assert post is not None
        assert post.content == "title\nbody"
        assert [item.url for item in post.attachments] == ["post-image"]

        oversized = FeishuAdapter(FeishuConfig(account_id="b", app_id="app", max_attachment_bytes=4), transport=lambda *_: _too_large())
        oversized_event = oversized.parse_event(_payload("image", {"image_key": "too-large"}))
        assert oversized_event is not None
        with pytest.raises(ChannelAdapterError, match="大小限制"):
            await oversized.download_media(oversized_event)

    async def _too_large():
        return b"12345"

    asyncio.run(run())


def test_feishu_outbound_attachment_uses_upload_key_then_native_media_message(tmp_path) -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def transport(operation: str, params):
            calls.append((operation, dict(params)))
            if operation == "im.v1.message.create":
                return {"data": {"message_id": f"sent-{len(calls)}"}}
            if operation == "im.v1.media.create":
                assert params["data"] == b"file body"
                assert params["file_name"] == "note.md"
                return {"file_key": "uploaded-key"}
            raise AssertionError(operation)

        path = tmp_path / "note.md"
        path.write_bytes(b"file body")
        adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"), transport=transport)
        inbound = adapter.parse_event(_payload("text", {"text": "hello"}))
        assert inbound is not None
        target = (await adapter.list_targets())[0]
        result = await adapter.send(OutboundMessage(target, "reply", attachments=(
            Attachment(
                kind="file", url=str(path), file_name="note.md", mime_type="text/markdown"
            ),
        )))
        assert result.platform_message_id == "sent-1"
        assert [operation for operation, _ in calls] == [
            "im.v1.message.create", "im.v1.media.create", "im.v1.message.create",
        ]
        media_message = calls[-1][1]
        assert media_message["msg_type"] == "file"
        assert json.loads(media_message["content"]) == {"file_key": "uploaded-key"}

    asyncio.run(run())


def test_feishu_streaming_uses_cardkit_entity_with_sequenced_updates_and_finalize() -> None:
    async def run() -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def transport(operation: str, params):
            calls.append((operation, dict(params)))
            if operation == "cardkit.create":
                return {"card_id": "card-1"}
            if operation == "im.v1.message.create":
                return {"message_id": "interactive-1"}
            return {"ok": True}

        adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"), transport=transport)
        inbound = adapter.parse_event(_payload("text", {"text": "hello"}))
        assert inbound is not None
        target = (await adapter.list_targets())[0]
        session = await adapter.stream_start(target)
        await adapter.stream_update(session, "Hel")
        await adapter.stream_update(session, "Hello")
        result = await adapter.stream_finalize(session, "Hello, world")
        assert result.platform_message_id == "interactive-1"
        assert calls[0][0] == "cardkit.create"
        card_json = json.loads(str(calls[0][1]["card_json"]))
        assert card_json["config"]["streaming_mode"] is True
        assert card_json["body"]["elements"][0]["element_id"] == "reply_md"
        assert calls[1][0] == "im.v1.message.create"
        assert json.loads(str(calls[1][1]["content"])) == {
            "type": "card", "data": {"card_id": "card-1"},
        }
        update = next(params for operation, params in calls if operation == "cardkit.update")
        assert update["content"] == "Hello"
        assert update["sequence"] == 1
        final_operation, final_params = calls[-2]
        assert final_operation == "cardkit.finalize"
        assert final_params["content"] == "Hello, world"
        assert final_params["settings_sequence"] == final_params["sequence"] + 1
        assert calls[-1][0] == 'cardkit.close'
        assert calls[-1][1]['sequence'] == final_params['sequence'] + 1

    asyncio.run(run())


def test_feishu_initial_cardkit_permission_rejection_stops_without_another_send():
    async def scenario():
        calls = []
        async def transport(operation, params):
            calls.append((operation, dict(params)))
            if operation == "cardkit.create":
                raise PlatformRejectedError("cardkit scope denied")
            assert operation == "im.v1.message.create"
            assert params["msg_type"] == "interactive"
            assert params["reply_message_id"] == "trigger"
            assert json.loads(params["content"])["body"]["elements"][0]["content"] == "complete reply"
            return {"message_id": "ordinary-card"}
        adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"), transport=transport)
        adapter.parse_event(_payload("text", {"text": "hello"}))
        target = (await adapter.list_targets())[0]
        session = await adapter.stream_start(target, reply_to_message_id="trigger")
        await adapter.stream_update(session, "complete")
        result = await adapter.stream_finalize(session, "complete reply")
        assert result.status is DeliveryStatus.FAILED
        assert result.platform_message_id is None
        assert [operation for operation, _ in calls] == ["cardkit.create"]
    asyncio.run(scenario())


def test_feishu_uncertain_card_creation_never_falls_back_with_another_send():
    async def scenario():
        operations = []
        async def transport(operation, params):
            operations.append(operation)
            raise TimeoutError
        adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"), transport=transport)
        adapter.parse_event(_payload("text", {"text": "hello"}))
        target = (await adapter.list_targets())[0]
        session = await adapter.stream_start(target)
        result = await adapter.stream_finalize(session, 'final answer')
        assert result.status is DeliveryStatus.UNCERTAIN
        assert result.platform_message_id is None
        assert operations == ["cardkit.create"]
    asyncio.run(scenario())


def test_feishu_visible_preview_with_rejected_finalize_preserves_receipt_without_final_text():
    async def scenario():
        operations = []
        async def transport(operation, params):
            operations.append(operation)
            if operation == "cardkit.create":
                return {"card_id": "card"}
            if operation == "im.v1.message.create":
                return {"message_id": "preview"}
            raise PlatformRejectedError("final update denied")
        adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"), transport=transport)
        adapter.parse_event(_payload("text", {"text": "hello"}))
        session = await adapter.stream_start((await adapter.list_targets())[0])
        result = await adapter.stream_finalize(session, "final answer")
        assert result.status is DeliveryStatus.FAILED
        assert result.platform_message_id == "preview"
        assert result.raw['deliveredContent'] == ''
        assert operations.count("im.v1.message.create") == 1
    asyncio.run(scenario())


def test_feishu_attachment_only_message_uses_media_receipt_and_preserves_reply(tmp_path):
    path = tmp_path / "note.md"
    path.write_bytes(b"note")
    async def scenario():
        operations = []
        async def transport(operation, params):
            operations.append(operation)
            if operation == "im.v1.media.create":
                return {"file_key": "file-key"}
            assert params["msg_type"] == "file" and params["reply_message_id"] == "trigger"
            return {"message_id": "media-receipt"}
        adapter = FeishuAdapter(FeishuConfig(account_id="a", app_id="app"), transport=transport)
        adapter.parse_event(_payload("text", {"text": "hello"}))
        target = (await adapter.list_targets())[0]
        result = await adapter.send(OutboundMessage(target, "", reply_to_message_id="trigger", attachments=(Attachment("file", str(path), "note.md"),)))
        assert result.status is DeliveryStatus.SENT
        assert result.platform_message_id == "media-receipt"
        assert operations == ["im.v1.media.create", "im.v1.message.create"]
    asyncio.run(scenario())


def test_feishu_transport_uses_resource_upload_and_cardkit_endpoints() -> None:
    async def run() -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("tenant_access_token/internal"):
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "access", "expire": 3600})
            if request.url.path.endswith("/resources/file-key"):
                return httpx.Response(200, content=b"resource")
            if request.url.path.endswith("/im/v1/images"):
                assert b"image_type" in request.content and b"message" in request.content
                return httpx.Response(200, json={"code": 0, "data": {"image_key": "image-key"}})
            if request.url.path.endswith("/cardkit/v1/cards"):
                return httpx.Response(200, json={"code": 0, "data": {"card_id": "card-id"}})
            return httpx.Response(200, json={"code": 0, "data": {}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = FeishuWebhookTransport("app", "secret", endpoint="https://feishu.test/open-apis", http_client=client)
        try:
            resource = await transport("im.v1.message.resource.get", {
                "message_id": "msg-id", "file_key": "file-key", "type": "file", "max_bytes": 32,
            })
            assert resource == b"resource"
            uploaded = await transport("im.v1.media.create", {"kind": "image", "data": b"png", "file_name": "x.png"})
            assert uploaded == {"image_key": "image-key"}
            card = await transport("cardkit.create", {"card_json": "{\"schema\":\"2.0\"}"})
            assert card == {"card_id": "card-id"}
            await transport("cardkit.update", {
                "card_id": "card-id", "element_id": "reply_md", "content": "hi", "sequence": 3,
            })
            await transport("cardkit.finalize", {
                "card_id": "card-id", "element_id": "reply_md", "content": "final", "sequence": 4,
                "settings_sequence": 5, "settings": "{\"config\":{\"streaming_mode\":false}}",
            })
            await transport('cardkit.close', {
                'card_id': 'card-id', 'sequence': 5, 'settings': '{"config":{"streaming_mode":false}}',
            })
            paths = [request.url.path for request in requests]
            assert paths.count("/open-apis/auth/v3/tenant_access_token/internal") == 1
            assert "/open-apis/im/v1/messages/msg-id/resources/file-key" in paths
            assert "/open-apis/im/v1/images" in paths
            assert "/open-apis/cardkit/v1/cards" in paths
            assert "/open-apis/cardkit/v1/cards/card-id/elements/reply_md/content" in paths
            assert "/open-apis/cardkit/v1/cards/card-id/settings" in paths
            update_request = next(req for req in requests if req.url.path.endswith("/elements/reply_md/content"))
            assert json.loads(update_request.content)["sequence"] == 3
        finally:
            await client.aclose()

    asyncio.run(run())
