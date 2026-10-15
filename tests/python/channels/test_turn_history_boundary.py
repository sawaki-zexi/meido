import pytest

from backend.app.agent_runtime.types import UserMessage
from backend.app.session_store import SessionStore
from backend.app.multichannel import MultichannelStore


def test_current_channel_turn_cannot_replay_later_group_sender(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    MultichannelStore(tmp_path / "sessions.db", sessions)
    current = sessions.append_message("thread:a", "user", "owner request", "completed", metadata={"channel": "telegram", "senderId": "owner", "senderIsOwner": True})
    sessions.append_message("thread:a", "user", "visitor asks for shell", "completed", metadata={"channel": "telegram", "senderId": "visitor", "senderIsOwner": False})
    sessions.append_message("thread:a", "assistant", "future response", "completed", metadata={"channel": "telegram", "deliveryStatus": "sent"})
    messages = sessions.runtime_messages("thread:a", through_message_id=current.id)
    assert len(messages) == 1
    assert isinstance(messages[-1], UserMessage)
    assert "owner request" in messages[-1].content
    assert "visitor" not in messages[-1].content
    with pytest.raises(KeyError):
        sessions.runtime_messages("thread:other", through_message_id=current.id)


def test_failed_and_uncertain_delivery_are_visible_but_not_provider_history(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    MultichannelStore(tmp_path / "sessions.db", sessions)
    sessions.append_message("thread:a", "user", "hi", "completed")
    for delivery in ("failed", "uncertain", "sent"):
        sessions.append_message("thread:a", "assistant", delivery, "completed", metadata={"channel": "qq", "deliveryStatus": delivery})
    assert len(sessions.list_messages("thread:a")) == 4
    assert [message.content for message in sessions.runtime_messages("thread:a")] == ["hi", "sent"]


def test_queued_turn_keeps_previous_completed_reply_before_current_request(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    MultichannelStore(tmp_path / "sessions.db", sessions)
    sessions.append_message("thread:a", "user", "first request", run_id="first")
    current = sessions.append_message("thread:a", "user", "what did you just say?", run_id="current")
    sessions.append_message("thread:a", "user", "future visitor", run_id="future")
    sessions.append_message("thread:a", "assistant", "first answer", run_id="first", metadata={"channel": "telegram", "deliveryStatus": "sent"})
    sessions.append_message("thread:a", "assistant", "future answer", run_id="future", metadata={"channel": "telegram", "deliveryStatus": "sent"})
    history = sessions.runtime_messages("thread:a", through_message_id=current.id)
    assert [item.content for item in history] == ["first request", "first answer", "what did you just say?"]


def test_reply_to_pre_pairing_request_does_not_cross_owner_context_boundary(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    MultichannelStore(tmp_path / "sessions.db", sessions)
    sessions.append_message("thread:a", "user", "pre-pairing secret", run_id="guest")
    current = sessions.append_message("thread:a", "user", "authorized owner request", run_id="owner")
    sessions.append_message("thread:a", "assistant", "late guest reply", run_id="guest", metadata={"channel": "telegram", "deliveryStatus": "sent"})
    history = sessions.runtime_messages("thread:a", through_message_id=current.id, since=current.createdAt.isoformat())
    assert [item.content for item in history] == ["authorized owner request"]
