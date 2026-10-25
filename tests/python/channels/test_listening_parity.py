from datetime import datetime, timedelta

import pytest

from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


def _group(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    store = MultichannelStore(tmp_path / "sessions.db", sessions)
    account = store.register_channel_account("role", "qq", account_id="qq", enabled=True, allow_group=True)
    thread = store.ensure_channel_thread("role", "qq", account.account_id, "group", "group")
    store.set_group_listening("role", "qq", "group", True)
    return store, sessions, thread


def test_daily_cap_keeps_overflow_only_in_recent_window_and_out_of_transcript(tmp_path):
    store, sessions, thread = _group(tmp_path)
    store.listening.set_cap("role", thread.thread_id, 2)
    for i in range(40):
        store.listening.hear(thread, f"content-{i}", str(i), {"senderId": "member"})
    assert [m["content"] for m in store.listening.page("role", thread.thread_id)["messages"]] == ["content-0", "content-1"]
    assert len(store.listening.recent("role", thread.thread_id)) == 30
    assert store.listening.recent("role", thread.thread_id)[-1]["content"] == "content-39"
    assert sessions.list_messages(thread.session_key) == []
    restarted = MultichannelStore(tmp_path / "sessions.db", sessions)
    assert [m["content"] for m in restarted.listening.recent("role", thread.thread_id)] == ["content-0", "content-1"]


def test_listening_cap_resets_by_local_day_and_deduplicates_stored_and_window(tmp_path):
    store, _, thread = _group(tmp_path)
    store.listening.set_default_cap(1)
    now = datetime.now().astimezone()
    first = store.listening.hear(thread, "one", "one", {}, timestamp=now)
    second = store.listening.hear(thread, "two", "two", {}, timestamp=now)
    assert first["sequence"] == 1 and second["sequence"] is None
    assert store.listening.hear(thread, "duplicate", "one", {}) is None
    assert store.listening.hear(thread, "duplicate", "two", {}) is None
    third = store.listening.hear(thread, "three", "three", {}, timestamp=now + timedelta(days=1))
    assert third["sequence"] == 2


def test_listening_permissions_clear_and_toggle_log_are_group_scoped(tmp_path):
    store, sessions, thread = _group(tmp_path)
    store.listening.hear(thread, "heard", "heard", {"senderId": "sender"})
    store.append_event(thread, direction="inbound", content="@role", external_message_id="trigger", metadata={"listening": False})
    store.append_event(thread, direction="inbound", content="legacy heard", external_message_id="legacy-heard", metadata={"listening": True})
    with pytest.raises(KeyError):
        store.listening.page("other-role", thread.thread_id)
    with pytest.raises(ValueError):
        store.listening.set_cap("role", thread.thread_id, True)
    store.set_group_listening("role", "qq", "group", False, operator="role")
    assert store.listening.hear(thread, "off", "off", {}) is None
    assert [toggle["operator"] for toggle in store.listening.state("role", thread.thread_id)["toggles"]] == ["role", "user"]
    assert store.clear_listening_messages("role", "qq", "group") == 2
    assert [message.content for message in sessions.list_messages(thread.session_key)] == ["@role"]
    assert store.listening.page("role", thread.thread_id)["messages"] == []


def test_listening_prompt_uses_only_current_group_and_respects_character_budget(tmp_path):
    store, _, thread = _group(tmp_path)
    for i in range(30):
        store.listening.hear(thread, "x" * 1000, str(i), {"senderId": "member"})
    prompt = store.listening.context("role", thread.thread_id)
    assert len(prompt) < 3100
    other = store.ensure_channel_thread("role", "qq", "qq", "other", "group")
    assert store.listening.context("role", other.thread_id) == ""


def test_natural_settings_default_off_scoped_and_persisted(tmp_path):
    store, sessions, thread = _group(tmp_path)
    other = store.ensure_channel_thread('role', 'qq', 'qq', 'other', 'group')
    assert store.listening.state('role', thread.thread_id)['naturalEnabled'] is False
    state = store.listening.set_natural_enabled('role', thread.thread_id, True)
    assert state['naturalEnabled'] and state['naturalEffective'] and not state['naturalPaused']
    assert not store.listening.state('role', other.thread_id)['naturalEnabled']
    with pytest.raises(KeyError):
        store.listening.set_natural_enabled('other-role', thread.thread_id, False)
    with pytest.raises(ValueError):
        store.listening.set_natural_enabled('role', thread.thread_id, 'true')
    restarted = MultichannelStore(tmp_path / 'sessions.db', sessions)
    assert restarted.listening.state('role', thread.thread_id)['naturalEnabled']


def test_natural_pause_preserves_choice_and_clear_invalidates_without_erasing_settings(tmp_path):
    store, _, thread = _group(tmp_path)
    store.listening.set_natural_enabled('role', thread.thread_id, True)
    store.set_group_listening('role', 'qq', 'group', False)
    state = store.listening.state('role', thread.thread_id)
    assert state['naturalEnabled'] and state['naturalPaused'] and not state['naturalEffective']
    with pytest.raises(ValueError):
        store.listening.set_natural_enabled('role', thread.thread_id, True)
    store.set_group_listening('role', 'qq', 'group', True)
    assert store.listening.state('role', thread.thread_id)['naturalEffective']
    invalidations = []
    store.listening.on_invalidate = invalidations.append
    before = store.listening.revision(thread.thread_id)
    store.listening.clear('role', thread.thread_id)
    assert store.listening.revision(thread.thread_id) > before
    assert invalidations == [thread.thread_id]
    assert store.listening.state('role', thread.thread_id)['naturalEnabled']
    store.listening.set_natural_enabled('role', thread.thread_id, False)
    assert not store.listening.state('role', thread.thread_id)['naturalEffective']
    assert len(invalidations) == 2
