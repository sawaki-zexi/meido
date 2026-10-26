"""QQ file IDs reach the real turn; rejected files stay visible, not silently lost."""
import asyncio

import httpx
import pytest

from backend.app import main
from backend.app.channels import media
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.model_adapter import ModelTextDelta
from test_meme_turn import meme_turn, payload


def test_cq_file_entities_decode_once_without_changing_opaque_percent_ids():
    adapter = NapCatAdapter(NapCatConfig("qq"))
    event = payload("cq-file", image=False)
    event["message"] = "说明&amp;[CQ:file,name=笔记&#44;&#91;新版&#93;&amp;amp;.txt,id=id&#44;&#91;&#93;&amp;%2F,size=7]"
    parsed = adapter.parse_event(event)
    assert parsed.content == "说明&[文件：笔记,[新版]&amp;.txt]"
    assert parsed.attachments[0].file_id == "id,[]&%2F"


@pytest.mark.parametrize("name,size,data,url,reason,should_download", [
    ("report.pdf", 10, b"pdf", True, "仅支持 TXT/MD", False),
    ("large.txt", 2 * 1024 * 1024 + 1, b"large", True, "超过 2 MiB", False),
    ("encoding.txt", 1, b"\xff", True, "UTF-8", True),
    ("binary.md", 3, b"a\0b", True, "二进制", True),
    ("missing.txt", 10, b"missing", False, "没有可用下载地址", False),
    ("timeout.txt", 1, None, True, "接收超时", True),
    ("quota.txt", 1, b"file", True, "存储空间不足", True),
])
def test_file_rejection_is_visible_in_model_and_saved_conversation(
    meme_turn, monkeypatch, name, size, data, url, reason, should_download,
):
    state, requests, downloads, resolutions = meme_turn, [], [], []
    original = state.adapter._transport
    async def transport(operation, params):
        if operation == "get_group_file_url":
            resolutions.append(params)
            return {"data": {"url": "https://files.qq.com/doc"} if url else {}}
        return await original(operation, params)
    async def download(link, *, max_bytes, resolved):
        downloads.append(link)
        assert max_bytes == 2 * 1024 * 1024
        if data is None:
            await asyncio.Event().wait()
        return data, "application/octet-stream"
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("已看到文件接收状态")
    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_file", download)
    if data is None:
        monkeypatch.setattr("backend.app.channels.qq_files.FILE_TIMEOUT_SECONDS", 0.03)
    if name == "quota.txt":
        monkeypatch.setattr(main, "_MAX_CHANNEL_ATTACHMENT_STORAGE_BYTES", 0)
    monkeypatch.setattr(main, "model_adapter", Model())
    async def scenario():
        try:
            event = payload("file-status", image=False, sender="22")
            event["message"].append({"type": "file", "data": {
                "name": name, "file_id": "opaque-file-id", "size": size,
            }})
            await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert len(requests) == 1
    current = next(item for item in reversed(requests[0]) if item["role"] == "user")
    assert name in str(current["content"]) and reason in str(current["content"])
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, "100", chat_type="group")
    message = next(item for item in state.sessions.list_messages(thread.session_key) if item.role == "user")
    assert name in message.content and reason in message.content
    assert not message.metadata.get("attachments")
    assert bool(downloads) is should_download
    if name.endswith("pdf") or size > 2 * 1024 * 1024:
        assert not resolutions


@pytest.mark.parametrize("during", ["lookup", "download"])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("kind", ["account", "group"])
def test_revocation_during_file_url_resolution_stops_download_and_model(meme_turn, monkeypatch, during, reopen, kind):
    state, requests, downloads = meme_turn, [], []
    original = state.adapter._transport
    def revoke():
        if kind == "group":
            state.channels.set_group_replies(state.role.id, state.adapter.account_id, "100", False)
            if reopen:
                state.channels.set_group_replies(state.role.id, state.adapter.account_id, "100", True)
            return
        state.channels.register_channel_account(state.role.id, "qq", account_id=state.adapter.account_id,
            enabled=False, allow_group=True)
        if reopen:
            state.channels.register_channel_account(state.role.id, "qq", account_id=state.adapter.account_id,
                enabled=True, allow_group=True)
    async def transport(operation, params):
        if operation == "get_group_file_url":
            if during == "lookup":
                revoke()
            return {"data": {"url": "https://files.qq.com/doc"}}
        return await original(operation, params)
    async def download(link, *, max_bytes, resolved):
        downloads.append(link)
        if during == "download":
            revoke()
        return b"must not be read", "text/plain"
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("must not run")
    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_file", download)
    monkeypatch.setattr(main, "model_adapter", Model())
    async def scenario():
        try:
            event = payload("revoked-file", image=False, sender="22")
            event["message"].append({"type": "file", "data": {"name": "secret.txt", "file_id": "file-id"}})
            await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert bool(downloads) is (during == "download")
    assert not requests
    assert not list(state.roles.workspace_path(state.role.id).glob("attachments/*.txt"))


@pytest.mark.parametrize("url,body,expected", [
    ("http://203.205.219.57:8080/ftn_handler/file", b"private file", "saved"),
    ("https://files.qq.com/ftn_handler/file", b"x" * (2 * 1024 * 1024 + 1), "超过 2 MiB"),
    ("http://127.0.0.1/secret", b"secret", "不可信"),
    ("file:///D:/secret.txt", b"secret", "不可信"),
    ("http://example.com/secret", b"secret", "不可信"),
], ids=["public-private-server", "stream-oversize", "loopback", "local-file", "foreign-domain"])
def test_private_file_lookup_uses_actual_bounded_http_downloader(meme_turn, monkeypatch, url, body, expected):
    state, fetched, requests = meme_turn, [], []
    async def transport(operation, params):
        if operation == "get_private_file_url":
            assert params == {"file_id": "opaque-private-id"}
            return {"data": {"url": url}}
        if operation == "send_private_msg":
            assert params["user_id"] == 22
            return {"message_id": "private-receipt"}
        return {"data": []}
    def http(request):
        fetched.append(str(request.url))
        return httpx.Response(200, content=body, headers={"content-type": "text/plain"})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(media.httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(http), **kw))
    monkeypatch.setattr(state.adapter, "_transport", transport)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("文件状态已确认")
    monkeypatch.setattr(main, "model_adapter", Model())
    async def scenario():
        try:
            event = payload("private-file", image=False, sender="22")
            event.update(message_type="private", user_id=22)
            event.pop("group_id")
            event["message"].append({"type": "file", "data": {"name": "private.txt", "file_id": "opaque-private-id"}})
            await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert len(requests) == 1
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, "22", chat_type="private")
    message = next(item for item in state.sessions.list_messages(thread.session_key) if item.role == "user")
    if expected == "saved":
        assert len(message.metadata["attachments"]) == 1
        assert message.metadata["attachments"][0]["platformFileId"] == "opaque-private-id"
    else:
        assert expected in message.content
        assert not message.metadata["attachments"]
    assert bool(fetched) is (expected != "不可信")


def test_file_count_limit_is_visible_and_does_not_download_tail(meme_turn, monkeypatch):
    state, downloads, requests = meme_turn, [], []
    async def download(link, *, max_bytes, resolved):
        downloads.append(link)
        return b"file", "text/plain"
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("已查看附件")
    monkeypatch.setattr(media, "download_qq_file", download)
    monkeypatch.setattr(main, "model_adapter", Model())
    async def scenario():
        try:
            event = payload("many-files", image=False, sender="22")
            event["message"].extend({"type": "file", "data": {"name": f"{i}.txt", "url": f"https://files.qq.com/{i}"}} for i in range(5))
            await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert len(downloads) == 4
    assert "4.txt；每条消息最多接收 4 个附件" in str(requests[0])


@pytest.mark.parametrize("replies", [True, False])
def test_listening_retains_file_name_without_lookup_download_or_model(meme_turn, monkeypatch, replies):
    state, requests = meme_turn, []
    state.channels.set_group_listening(state.role.id, state.adapter.account_id, "100", True)
    state.channels.set_group_replies(state.role.id, state.adapter.account_id, "100", replies)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("must not run")
    async def unexpected(*args, **kw):
        pytest.fail("listening must not query or download a file")
    monkeypatch.setattr(state.adapter, "_transport", unexpected)
    monkeypatch.setattr(media, "download_qq_file", unexpected)
    monkeypatch.setattr(main, "model_adapter", Model())
    async def scenario():
        event = payload("listened-file", image=False, triggered=False, sender="22")
        event["message"].append({"type": "file", "data": {"file": "D:\\platform\\listened.md", "id": "file-id"}})
        await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
    asyncio.run(asyncio.wait_for(scenario(), 10))
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, "100", chat_type="group")
    heard = state.channels.listening.recent(state.role.id, thread.thread_id)
    assert "[文件：listened.md]" in heard[0]["content"]
    assert not requests


def test_unpair_and_repair_during_lookup_does_not_reauthorize_old_file(meme_turn, monkeypatch):
    state, requests, downloads = meme_turn, [], []
    async def transport(operation, params):
        if operation == "get_group_file_url":
            binding = state.channels.list_identities()[0]
            state.channels.unbind_identity(binding["identityId"])
            code = state.channels.create_pairing_code(state.role.id, "qq")
            assert state.channels.consume_pairing_code(code, role_id=state.role.id, channel="qq", scope="platform",
                platform_user_id="11", account_id=state.adapter.account_id, chat_id="11")
            return {"data": {"url": "https://files.qq.com/file"}}
        return {"data": []}
    async def download(*args, **kw):
        downloads.append(True)
        return b"old file", "text/plain"
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("must not run")
    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_file", download)
    monkeypatch.setattr(main, "model_adapter", Model())
    async def scenario():
        event = payload("old-owner-file", image=False, sender="11")
        event["message"].append({"type": "file", "data": {"name": "old.txt", "file_id": "file-id"}})
        await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert not downloads and not requests


def test_buffered_input_from_retired_connection_cannot_start_file_lookup(meme_turn, monkeypatch):
    state, resolutions = meme_turn, []
    async def transport(operation, params):
        resolutions.append(operation)
        return {"data": {"url": "https://files.qq.com/file"}}
    monkeypatch.setattr(state.adapter, "_transport", transport)
    async def download(*args, **kw):
        return b"old file", "text/plain"
    monkeypatch.setattr(media, "download_qq_file", download)
    async def scenario():
        event = payload("retired-connection", image=False, sender="22")
        event["message"].append({"type": "file", "data": {"name": "old.txt", "file_id": "file-id"}})
        await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter,
            source_generation="retired-connection-generation")
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert not resolutions


@pytest.mark.parametrize("change", ["account", "platform", "pairing", "connection"])
def test_source_revoked_while_waiting_for_account_lock_stops_first_lookup(meme_turn, monkeypatch, change):
    state, operations, requests = meme_turn, [], []
    accepted = asyncio.Event()
    original_accept = main.channel_host.accept_event
    async def observe(*args):
        event = await original_accept(*args)
        accepted.set()
        return event
    async def transport(operation, params):
        operations.append(operation)
        return {"data": {"url": "https://files.qq.com/file"}}
    transport.connection_generation = "before"
    async def download(*args, **kw):
        return b"old file", "text/plain"
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("must not run")
    monkeypatch.setattr(main.channel_host, "accept_event", observe)
    monkeypatch.setattr(state.adapter, "_transport", transport)
    monkeypatch.setattr(media, "download_qq_file", download)
    monkeypatch.setattr(main, "model_adapter", Model())
    async def scenario():
        lock = asyncio.Lock()
        main.channel_event_locks[state.adapter.account_id] = lock
        await lock.acquire()
        event = payload("queued-file", image=False, sender="11")
        event["message"].append({"type": "file", "data": {"name": "old.txt", "file_id": "file-id"}})
        task = asyncio.create_task(main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter))
        try:
            await asyncio.wait_for(accepted.wait(), 3)
            if change == "account":
                state.channels.set_account_state(state.adapter.account_id, enabled=False)
                state.channels.set_account_state(state.adapter.account_id, enabled=True)
            elif change == "platform":
                state.channels.set_channel_enabled("qq", False)
                state.channels.set_channel_enabled("qq", True)
            elif change == "pairing":
                state.channels.unbind_identity(state.channels.list_identities()[0]["identityId"])
                code = state.channels.create_pairing_code(state.role.id, "qq")
                state.channels.consume_pairing_code(code, role_id=state.role.id, channel="qq", scope="platform",
                    platform_user_id="11", account_id=state.adapter.account_id, chat_id="11")
            else:
                transport.connection_generation = "after"
        finally:
            lock.release()
            await task
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert not operations and not requests
