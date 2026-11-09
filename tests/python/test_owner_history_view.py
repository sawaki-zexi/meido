import asyncio
import io
import json
from dataclasses import replace

import pytest
from PIL import Image

from backend.app.agent_runtime.message_history_tools import build_message_history_tools
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from backend.app.channels.attachment_tool import ReadChannelAttachmentTool
from backend.app.chat_attachments import save_attachment, resolve_attachment
from backend.app.conversation_history import ConversationHistoryView
from test_message_history_tools import setup_history


def pair(channels, thread, sender="owner"):
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id=thread.role_id, channel=thread.channel, scope="platform",
        platform_user_id=sender, account_id=thread.account_id, chat_id=thread.chat_id)


def private(channels, chat="owner"):
    if channels.get_account("qq") is None:
        channels.register_channel_account("r1", "qq", account_id="qq")
    return channels.ensure_channel_thread("r1", "qq", "qq", chat, "private")


def test_empty_frozen_grants_skip_queries_without_accepting_later_authority(tmp_path, monkeypatch):
    sessions, channels, _, _ = setup_history(tmp_path)
    allowed = sessions.append_message('role:r1', 'user', '本角色历史')
    checks, connections = [], []
    enabled = [True]

    def authorized():
        checks.append(enabled[0])
        return enabled[0]

    view = ConversationHistoryView(channels, role_id='r1', authorized=authorized)
    assert view.snapshot()['grants'] == {}
    thread = private(channels)
    pair(channels, thread)
    sessions.append_message(thread.session_key, 'user', '后来新增授权的秘密')
    connect = channels._connect

    def observed():
        connections.append(True)
        return connect()

    monkeypatch.setattr(channels, '_connect', observed)
    for _ in range(1200):
        view.require_current()
    assert len(checks) == 1200
    assert connections == [], 'empty frozen grants have no current grants to compare on stream callbacks'
    assert view.snapshot()['grants'] == {}
    assert [message.id for message in view.read()] == [allowed.id]
    enabled[0] = False
    with pytest.raises(PermissionError, match='历史来源已失效'):
        view.require_current()


def test_frozen_view_preserves_turn_groups_and_excludes_future_and_pre_pairing_replies(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    thread = private(channels)
    sessions.append_message(thread.session_key, "user", "配对前原文", run_id="guest")
    pair(channels, thread)
    sessions.append_message(thread.session_key, "user", "已配对问题", run_id="first")
    current = sessions.append_message("role:r1", "user", "当前桌面问题", run_id="current")
    view = ConversationHistoryView(channels, role_id="r1", through_message_id=current.id)
    sessions.append_message(thread.session_key, "user", "后来的手机问题", run_id="future")
    sessions.append_message(thread.session_key, "assistant", "配对前的迟到答案", run_id="guest", metadata={"deliveryStatus": "sent"})
    sessions.append_agent_message(thread.session_key, AssistantMessage(tool_calls=(ToolCall("read", "read_file", {"path": "x"}),), stop_reason="tool_use"), run_id="first")
    sessions.append_agent_message(thread.session_key, ToolResultMessage("read", "read_file", "工具原文"), run_id="first")
    sessions.append_message(thread.session_key, "assistant", "较早问题的完整答案", run_id="first", metadata={"deliveryStatus": "sent"})
    sessions.append_message(thread.session_key, "assistant", "未确认投递", run_id="first", metadata={"deliveryStatus": "uncertain"})
    messages = view.runtime_messages()
    text = str(messages)
    assert all(value not in text for value in ("配对前原文", "后来的手机问题", "配对前的迟到答案", "未确认投递"))
    assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant", "user"]
    assert messages[1].tool_calls[0].id == messages[2].tool_call_id == "read"
    assert messages[-1].source_message_ids == (current.id,)
    # Later completion of a source cannot enlarge an already consumed snapshot.
    sessions.append_message(thread.session_key, "assistant", "更晚的答案", run_id="first", metadata={"deliveryStatus": "sent"})
    assert "更晚的答案" not in str(view.runtime_messages())


def test_frozen_grants_do_not_expand_and_repair_revokes_old_view_without_losing_new_turn_history(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    first = private(channels)
    pair(channels, first)
    allowed = sessions.append_message(first.session_key, "user", "已授权旧消息")
    view = ConversationHistoryView(channels, role_id="r1")
    other = private(channels, "second")
    pair(channels, other, "second")
    sessions.append_message(other.session_key, "user", "之后才配对的消息")
    assert [message.id for message in view.read()] == [allowed.id]
    before = channels.owner_chat_context_since(role_id="r1", channel="qq", account_id="qq", chat_id="owner")
    pair(channels, first)
    assert channels.owner_chat_context_since(role_id="r1", channel="qq", account_id="qq", chat_id="owner") == before
    with pytest.raises(PermissionError):
        view.read()
    assert allowed.id in {message.id for message in ConversationHistoryView(channels, role_id="r1").read()}
    identity = next(item for item in channels.list_identities() if item["platformUserId"] == "owner")
    channels.unbind_identity(identity["identityId"])
    pair(channels, first)
    assert allowed.id not in {message.id for message in ConversationHistoryView(channels, role_id="r1").read()}


def test_owner_view_keeps_only_confirmed_parts_with_source_labels_and_original_records(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    thread = private(channels)
    pair(channels, thread)
    sessions.append_message(thread.session_key, 'user', 'question', run_id='partial')
    original = sessions.append_message(thread.session_key, 'assistant', 'CONFIRMED UNSENT_TAIL', 'failed', run_id='partial', metadata={
        'channel': 'qq', 'deliveryStatus': 'uncertain', 'partialDelivery': True,
        'parts': [{'kind': 'text', 'content': 'CONFIRMED', 'status': 'sent', 'platformMessageId': 'real'},
                  {'kind': 'text', 'content': ' UNSENT_TAIL', 'status': 'uncertain', 'platformMessageId': None}],
        'content_blocks': [{'type': 'text', 'text': 'CONFIRMED UNSENT_TAIL'}],
    })
    view = ConversationHistoryView(channels, role_id='r1')
    projected = next(item for item in view.read() if item.id == original.id)
    assert projected.content == 'CONFIRMED' and projected.status == 'completed'
    runtime = view.runtime_messages()
    assert '回复来源' in runtime[-1].content and 'CONFIRMED' in runtime[-1].content
    from backend.app.agent_runtime.meido_provider import MeidoProvider
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelTextBlock
    from backend.app.models import RoleInput, RoleProfile
    requests = []
    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools=(), **kwargs):
            requests.extend(messages)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock('ok'),)))
    role = RoleInput(name='fixture', profile=RoleProfile(profile='fixture')).model_copy(update={'id': 'r1'})
    provider = MeidoProvider(Adapter(), role, None)
    async def render():
        async for _ in provider.stream_response(model='', system='', messages=runtime, tools=[], signal=CancellationToken()):
            pass
    asyncio.run(render())
    sent = [block.text for message in requests for block in message.content if hasattr(block, 'text')]
    assert any('回复来源' in text for text in sent) and 'CONFIRMED' in sent
    assert not any('UNSENT_TAIL' in text for text in sent)
    assert sessions.list_messages(thread.session_key)[-1].content == 'CONFIRMED UNSENT_TAIL'


def test_history_boundary_survives_rowid_reuse_and_nickname_change(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    thread = private(channels); pair(channels, thread)
    old = sessions.append_message(thread.session_key, 'user', '旧消息')
    view = ConversationHistoryView(channels, role_id='r1')
    channels.ensure_channel_thread('r1', 'qq', 'qq', 'owner', 'private', title='新昵称')
    assert view.authorized()
    sessions.delete_message(old.id)
    sessions.append_message(thread.session_key, 'user', '删除后到达的新消息')
    assert view.read() == []


def test_confirmed_desktop_push_stays_in_history_without_internal_unsent_notices(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    sessions.append_proactive_message_once('role:r1', '未送达的内部提醒', event_id='internal')
    sessions.append_desktop_push_once('r1', 'source-run', 'push', '主人已收到的独立推送', attachments=[], authorized=lambda: True)
    view = ConversationHistoryView(channels, role_id='r1')
    messages = view.runtime_messages()
    assert len(messages) == 1 and '主人已收到的独立推送' in messages[0].content


def test_owned_image_and_quote_survive_but_foreign_or_missing_files_are_not_opened(tmp_path):
    sessions, channels, _, _ = setup_history(tmp_path)
    root = tmp_path / "r1"
    output = io.BytesIO(); Image.new("RGB", (2, 2), "red").save(output, "PNG")
    image = save_attachment(root, "photo.png", "image/png", output.getvalue())
    foreign = save_attachment(tmp_path / "r2", "foreign.png", "image/png", output.getvalue())
    thread = private(channels); pair(channels, thread)
    original = sessions.append_message(thread.session_key, "user", "这张图", run_id="old", metadata={"platformMessageId": "11", "attachments": [image], 'senderId':'owner', 'senderIsOwner':True})
    sessions.append_message(thread.session_key, "user", "继续看引用图片", run_id="quote", metadata={"replyTo": "11", "attachments": [foreign, {"id": "missing", "name": "丢失.png", "path": image["path"]}]})
    view = ConversationHistoryView(channels, role_id="r1", attachment_resolver=lambda key: resolve_attachment(root, key))
    messages = view.runtime_messages()
    assert all(isinstance(message, UserMessage) for message in messages)
    assert len([attachment for attachment in messages[-1].attachments if attachment.image]) == 1
    assert sum("不属于" in attachment.text for attachment in messages[-1].attachments) == 2
    assert "引用主人" in messages[-1].content and original.content in messages[-1].content


@pytest.mark.parametrize('source', ['allowed', 'legacy', 'pre-pair', 'other-chat'])
def test_authorized_quote_content_and_identity_reach_actual_request_without_cached_or_current_binding_inference(tmp_path, source):
    from backend.app.agent_runtime.meido_provider import MeidoProvider
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.model_adapter import ModelTextDelta
    from backend.app.models import RoleInput
    sessions, channels, _, _ = setup_history(tmp_path)
    thread = private(channels)
    if source != 'pre-pair': pair(channels, thread)
    origin = private(channels, 'ungranted-other') if source == 'other-chat' else thread
    metadata = {'channel':'qq', 'accountId':'qq', 'threadId':origin.thread_id,
        'chatId':origin.chat_id, 'chatType':'private', 'direction':'inbound', 'platformMessageId':'original'}
    if source != 'legacy': metadata.update(senderId='owner', senderIsOwner=True, senderName='Saved name')
    original = sessions.append_message(origin.session_key, 'user', 'AUTHORIZED_ORIGINAL_TEXT', metadata=metadata)
    if source == 'pre-pair': pair(channels, thread)
    current_meta = {'channel':'qq', 'accountId':'qq', 'threadId':thread.thread_id,
        'chatId':thread.chat_id, 'chatType':'private', 'direction':'inbound', 'platformMessageId':'current',
        'senderId':'owner', 'senderIsOwner':True, 'replyTo':'original', 'replyToContent':'CACHED_QUOTE_MUST_NOT_LEAK'}
    current = sessions.append_message(thread.session_key, 'user', 'CURRENT_QUESTION', metadata=current_meta)
    view = ConversationHistoryView(channels, role_id='r1', thread_id=thread.thread_id, through_message_id=current.id)
    projected = view.runtime_messages()
    requests = []

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta('ok')

    async def run():
        role = RoleInput(name='quote fixture', profile={'profile':'fixture'}).model_copy(update={'id':'r1'})
        provider = MeidoProvider(Adapter(), role, None, structured_without_tools=True)
        async for _ in provider.stream_response(model='', system='', messages=projected, tools=(), signal=CancellationToken()): pass

    asyncio.run(run())
    text = next(item['content'] for item in requests[0] if item['role']=='user' and 'CURRENT_QUESTION' in item['content'])
    facts = json.loads(next(line for line in text.splitlines() if line.startswith('[消息来源] ')).removeprefix('[消息来源] '))
    if source in {'allowed', 'legacy'}:
        assert facts['replyToContent'] == original.content and facts['replyMissing'] is False
        if source == 'allowed':
            assert facts['replyToSenderId'] == 'owner' and facts['replyToSenderIdentity'] == '已配对主人'
        else:
            assert facts['replyToSenderId'] is None and facts['replyToSenderIdentity'] == '未知发送者'
            assert '引用主人' not in text
    else:
        assert facts['replyToContent'] == '' and facts['replyMissing'] is True
        assert 'AUTHORIZED_ORIGINAL_TEXT' not in str(requests)
    assert 'CACHED_QUOTE_MUST_NOT_LEAK' not in str(requests)
    assert sessions.list_messages(thread.session_key)[-1].metadata['replyToContent'] == 'CACHED_QUOTE_MUST_NOT_LEAK'


def test_media_and_tool_wait_recheck_the_same_frozen_grant(tmp_path, monkeypatch):
    sessions, channels, _, context = setup_history(tmp_path)
    thread = private(channels); pair(channels, thread)
    sessions.append_message(thread.session_key, "user", "秘密", metadata={"attachments": [{"id": "anything", "name": "图"}]})
    def revoke(_key):
        channels.unbind_identity(channels.list_identities()[0]["identityId"])
        return None
    view = ConversationHistoryView(channels, role_id="r1", attachment_resolver=revoke)
    with pytest.raises(PermissionError):
        view.runtime_messages()
    pair(channels, thread)
    allowed = sessions.append_message(thread.session_key, "user", "新秘密")
    view = ConversationHistoryView(channels, role_id="r1")
    tool = build_message_history_tools(sessions, channels, role_id="r1", view=view)[1]
    original_read = tool.history.read
    def delayed_read(context):
        result = original_read(context)
        revoke("")
        return result
    monkeypatch.setattr(tool.history, "read", delayed_read)
    with pytest.raises(PermissionError):
        asyncio.run(tool.execute({"source_ref": allowed.id}, context))


@pytest.mark.parametrize("source", ["allowed", "pre-pair", "other-chat", "foreign-attachment"])
def test_current_quoted_attachment_grant_uses_only_visible_same_chat_and_role_files(tmp_path, source):
    sessions, channels, _, context = setup_history(tmp_path)
    root = tmp_path / "r1"
    direct = save_attachment(root, "direct.txt", "text/plain", b"DIRECT")
    quoted = save_attachment(tmp_path / "r2" if source == "foreign-attachment" else root,
        "quoted.txt", "text/plain", b"QUOTED")
    unrelated = save_attachment(root, "unrelated.txt", "text/plain", b"UNRELATED")
    downloaded = save_attachment(root, "fresh.txt", "text/plain", b"UNVERIFIED_DOWNLOAD")
    thread = private(channels)
    if source != "pre-pair":
        pair(channels, thread)
    origin_thread = private(channels, "second") if source == "other-chat" else thread
    if origin_thread != thread:
        pair(channels, origin_thread, "second")
    original = sessions.append_message(origin_thread.session_key, "user", "原文件",
        metadata={"attachments": [{**quoted, "path": unrelated["path"]}]})
    if source == "pre-pair":
        pair(channels, thread)
    sessions.append_message(thread.session_key, "user", "未被引用的文件", metadata={"attachments": [unrelated]})
    current = sessions.append_message(thread.session_key, "user", "读取引用",
        metadata={"replyTo": original.id, "attachments": [direct], "quotedAttachments": [downloaded]})
    view = ConversationHistoryView(channels, role_id="r1", thread_id=thread.thread_id,
        through_message_id=current.id, attachment_resolver=lambda key: resolve_attachment(root, key))
    shown = view.runtime_messages()[-1]
    tool = ReadChannelAttachmentTool("r1", thread.session_key, view.current_attachments(), authorized=view.authorized)
    context = replace(context, session_key=thread.session_key)

    async def check():
        direct_result = await tool.execute({"attachment_id": direct["id"]}, context)
        assert not direct_result.is_error and direct_result.content.startswith("DIRECT\n")
        result = await tool.execute({"attachment_id": quoted["id"]}, context)
        assert result.is_error is (source != "allowed")
        if source == "allowed":
            assert f"attachment_id={quoted['id']}" in shown.content
            assert result.content.startswith("QUOTED\n"), "historical path must not choose the file"
        for identifier in (unrelated["id"], downloaded["id"]):
            denied = await tool.execute({"attachment_id": identifier}, context)
            assert denied.is_error and "不属于当前 QQ 回合" in denied.content
        for other_context in (replace(context, role_id="r2"), replace(context, session_key="role:r1")):
            assert (await tool.execute({"attachment_id": direct["id"]}, other_context)).is_error

    asyncio.run(check())
    persisted = next(message for message in sessions.list_messages(origin_thread.session_key) if message.id == original.id)
    assert persisted.metadata == original.metadata, "projection must not rewrite the stored source"


@pytest.mark.parametrize("change", ["unbind", "remove-source", "during-resolution"])
def test_current_attachment_permission_is_revoked_after_snapshot_or_during_resolution(tmp_path, change):
    sessions, channels, _, context = setup_history(tmp_path)
    root = tmp_path / "r1"
    attachment = save_attachment(root, "private.txt", "text/plain", b"PRIVATE_FILE")
    thread = private(channels); pair(channels, thread)
    original = sessions.append_message(thread.session_key, "user", "原文件", metadata={"attachments": [attachment]})
    current = sessions.append_message(thread.session_key, "user", "引用", metadata={"replyTo": original.id})

    def resolve(identifier):
        result = resolve_attachment(root, identifier)
        if change == "during-resolution":
            channels.unbind_identity(channels.list_identities()[0]["identityId"])
        return result

    view = ConversationHistoryView(channels, role_id="r1", thread_id=thread.thread_id,
        through_message_id=current.id, attachment_resolver=resolve)
    if change == "during-resolution":
        with pytest.raises(PermissionError):
            view.current_attachments()
        return
    tool = ReadChannelAttachmentTool("r1", thread.session_key, view.current_attachments(), authorized=view.authorized)
    if change == "unbind":
        channels.unbind_identity(channels.list_identities()[0]["identityId"])
    else:
        sessions.delete_message(original.id)
    result = asyncio.run(tool.execute({"attachment_id": attachment["id"]}, replace(context, session_key=thread.session_key)))
    assert result.is_error and "PRIVATE_FILE" not in result.content
