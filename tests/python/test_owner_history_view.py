import asyncio
import io

import pytest
from PIL import Image

from backend.app.agent_runtime.message_history_tools import build_message_history_tools
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
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
    original = sessions.append_message(thread.session_key, "user", "这张图", run_id="old", metadata={"platformMessageId": "11", "attachments": [image]})
    sessions.append_message(thread.session_key, "user", "继续看引用图片", run_id="quote", metadata={"replyTo": "11", "attachments": [foreign, {"id": "missing", "name": "丢失.png", "path": image["path"]}]})
    view = ConversationHistoryView(channels, role_id="r1", attachment_resolver=lambda key: resolve_attachment(root, key))
    messages = view.runtime_messages()
    assert all(isinstance(message, UserMessage) for message in messages)
    assert len([attachment for attachment in messages[-1].attachments if attachment.image]) == 1
    assert sum("不属于" in attachment.text for attachment in messages[-1].attachments) == 2
    assert "引用主人" in messages[-1].content and original.content in messages[-1].content


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
