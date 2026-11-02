"""Quoted-file reads through QQ intake, frozen owner history and the model loop."""
import asyncio
import re

import pytest

from backend.app import main
from backend.app.channels import media
from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
from test_meme_turn import meme_turn


@pytest.mark.parametrize("owner", [True, False], ids=["owner", "guest"])
@pytest.mark.parametrize("extension", ["txt", "md"])
def test_private_quote_reads_displayed_id_beyond_preview_but_later_turn_rejects_it(
        meme_turn, monkeypatch, owner, extension):
    state = meme_turn
    sender = "11" if owner else "22"
    body = ("a" * 33000 + "QUOTED_FILE_TAIL").encode()
    requests, downloads = [], []
    displayed = {}

    def file_segment():
        return {"type": "file", "data": {"name": f"quoted.{extension}", "file_id": "file-id"}}

    async def transport(operation, params):
        if operation == "get_msg":
            assert params == {"message_id": "original"}
            return {"data": {"message_type": "private", "user_id": int(sender), "target_id": 99,
                "sender": {"user_id": int(sender)}, "message": [file_segment()]}}
        if operation == "get_private_file_url":
            assert params == {"file_id": "file-id"}
            return {"data": {"url": "https://qq.test/file"}}
        assert operation == "send_private_msg"
        return {"message_id": str(1000 + len(requests))}

    async def download(url, *, max_bytes, resolved):
        assert url == "https://qq.test/file" and resolved and len(body) <= max_bytes
        downloads.append(url)
        return body, "text/plain" if extension == "txt" else "text/markdown"

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            current = next(item for item in reversed(messages) if item["role"] == "user")
            content = current["content"]
            text = content if isinstance(content, str) else "\n".join(block.get("text", "") for block in content)
            assert "QUOTED_FILE_TAIL" not in text, "the requested tail is beyond the preview"
            if len(requests) in {1, 3}:
                assert "read_attachment" in {tool["function"]["name"] for tool in tools}
                identifier = re.findall(r"attachment_id=([a-f0-9]{32})", text)[-1]
                displayed["direct" if len(requests) == 1 else "quote"] = identifier
                yield ModelToolCall("read-tail", "read_attachment",
                    {"attachment_id": identifier, "character_offset": 33000})
                yield ModelResponseDone("tool_use")
            elif len(requests) == 5:
                assert "attachment_id=" not in text
                yield ModelToolCall("read-old", "read_attachment", {"attachment_id": displayed["quote"]})
                yield ModelResponseDone("tool_use")
            else:
                result = next(item for item in reversed(messages) if item["role"] == "tool")
                if len(requests) in {2, 4}:
                    assert result["content"].startswith("QUOTED_FILE_TAIL\n")
                    assert "已读到文件末尾" in result["content"]
                else:
                    assert "不属于当前 QQ 回合" in result["content"]
                yield ModelTextDelta("读取结果已收到")

    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_file", download)
    monkeypatch.setattr(main, "model_adapter", Model())

    def payload(identifier, segments):
        return {"post_type": "message", "message_type": "private", "self_id": 99,
            "user_id": int(sender), "message_id": identifier, "sender": {"user_id": int(sender)},
            "message": [{"type": "text", "data": {"text": "读取文件尾部"}}, *segments]}

    async def scenario():
        try:
            for identifier, segments in (("original", [file_segment()]),
                    ("quote", [{"type": "reply", "data": {"id": "original"}}]), ("later", [])):
                await main._dispatch_channel_payload(state.adapter.account_id, payload(identifier, segments),
                    source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(asyncio.wait_for(scenario(), 20))
    assert len(requests) == 6, "both reads and the old-ID rejection must reach the next model request"
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, sender, chat_type="private")
    stored = state.sessions.list_messages(thread.session_key)
    inputs = [item for item in stored if item.role == "user"]
    original = inputs[0].metadata["attachments"][0]["id"]
    fresh_quote = inputs[1].metadata["quotedAttachments"][0]["id"]
    assert original != fresh_quote
    assert displayed["quote"] == (original if owner else fresh_quote)
    results = [item for item in stored if item.messageType == "tool_result"]
    assert [item.isError for item in results] == [False, False, True]
    assert all(state.sessions.get_run(item.runId).status == "completed" for item in results)
    assert downloads == ["https://qq.test/file", "https://qq.test/file"]
