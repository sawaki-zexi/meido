from datetime import datetime, timedelta, timezone

from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


def test_recent_activities_are_role_scoped_sorted_bounded_and_recheck_pairing(tmp_path):
    channels = MultichannelStore(tmp_path / "sessions.db", SessionStore(tmp_path / "sessions.db"))
    channels.register_channel_account("r", "qq", account_id="qq")
    groups = MultichannelGroupRepository(channels)
    now = datetime.now(timezone.utc)
    threads = []
    for index in range(8):
        thread = channels.ensure_channel_thread("r", "qq", "qq", str(index), "group", title=f"群{index}")
        threads.append(thread)
        groups.update_context("r", thread.thread_id, summary=f"动态{index}")
        with channels._connect() as db:
            db.execute("UPDATE channel_group_context SET summary_updated_at=? WHERE thread_id=?",
                ((now - timedelta(hours=index)).isoformat(), thread.thread_id))
    private = channels.ensure_channel_thread("r", "qq", "qq", "22", "private", title="访客")
    with channels._connect() as db:
        channels.external_memory.write_context_in(db, private, summary="访客私聊动态")
        db.execute("UPDATE channel_group_context SET summary_updated_at=? WHERE thread_id=?", ((now - timedelta(days=4)).isoformat(), threads[1].thread_id))
    first = channels.external_memory.recent_activities("r", exclude_thread_id=threads[0].thread_id)
    assert [item["text"] for item in first] == ["访客私聊动态", "动态2", "动态3", "动态4", "动态5"]
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id="r", channel="qq", scope="platform", platform_user_id="22", account_id="qq", chat_id="22")
    assert [item["text"] for item in channels.external_memory.recent_activities("r", exclude_thread_id=threads[0].thread_id)] == [f"动态{i}" for i in range(2, 7)]
    assert channels.external_memory.recent_activities("another-role") == []
