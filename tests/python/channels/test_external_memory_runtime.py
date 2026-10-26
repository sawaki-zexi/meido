import asyncio
import json

from backend.app import main
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.model_adapter import ModelTextDelta
from test_meme_turn import meme_turn, payload
from test_natural_turn import natural


def test_natural_wait_rechecks_recent_activity_and_keeps_current_notes_separate(natural):
    s = natural
    groups = MultichannelGroupRepository(s.channels)
    other = s.channels.ensure_channel_thread(s.role.id, "qq", s.adapter.account_id, "200", "group", title="另一群")
    private = s.channels.ensure_channel_thread(s.role.id, "qq", s.adapter.account_id, "33", "private")
    groups.update_context(s.role.id, other.thread_id, summary="另一群动态-720")
    groups.update_context(s.role.id, s.thread.thread_id, group_note="本群专属笔记-721")
    with s.channels._connect() as db:
        s.channels.external_memory.write_context_in(db, private, summary="待配对私聊动态-722")
    async def run():
        try:
            s.result = {"action": "wait", "text": ""}
            await s.receive("before-wait", sender="22")
            await s.clock.advance(1)
            assert len(s.calls) == 1
            code = s.channels.create_pairing_code()
            assert s.channels.consume_pairing_code(code, role_id=s.role.id, channel="qq", scope="platform",
                platform_user_id="33", account_id=s.adapter.account_id, chat_id="33")
            s.result = {"action": "silence", "text": ""}
            await s.clock.advance(2)
            assert len(s.calls) == 2
            first, second = [json.dumps(messages, ensure_ascii=False) for messages, _ in s.calls]
            assert "待配对私聊动态-722" in first and "待配对私聊动态-722" not in second
            assert all("另一群动态-720" in prompt and "本群专属笔记-721" in prompt for prompt in (first, second))
        finally:
            await s.scheduler.close()
    asyncio.run(run())


def test_ordinary_group_runtime_injects_current_note_and_other_activity_without_raw_history(meme_turn, monkeypatch):
    s = meme_turn
    groups = MultichannelGroupRepository(s.channels)
    current = s.channels.ensure_channel_thread(s.role.id, "qq", s.adapter.account_id, "100", "group")
    other = s.channels.ensure_channel_thread(s.role.id, "qq", s.adapter.account_id, "200", "group")
    groups.update_context(s.role.id, current.thread_id, group_note="当前笔记-723")
    groups.update_context(s.role.id, other.thread_id, summary="其他群摘要-724")
    s.channels.append_event(other, direction="inbound", content="其他群原文-725", external_message_id="other", metadata={"senderId": "22"})
    requests = []
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta("群回复")
    monkeypatch.setattr(main, "model_adapter", Model())
    async def run():
        event = await main.channel_host.accept_event(s.adapter.account_id, payload("current", image=False, sender="22"))
        await main._handle_channel_event(event)
    asyncio.run(run())
    assert len(requests) == 1
    prompt = json.dumps(requests[0], ensure_ascii=False)
    assert "当前笔记-723" in prompt and "其他群摘要-724" in prompt and "其他群原文-725" not in prompt
