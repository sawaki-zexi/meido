from __future__ import annotations

from backend.app.mail.adapter import RemoteMail
from backend.app.mail.followup import classify_thread
from backend.app.mail.models import MailConnection, MailItem
from backend.app.mail.service import MailService
from backend.app.mail.store import MailStore


class Credentials:
    def get(self, key: str) -> str:
        return "fixture-secret" if key == "qq-password" else ""

    def set(self, key: str, value: str) -> None:
        del key, value


def _service(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection(
        host="imap.qq.com", port=993, username="owner@qq.com", verified=True,
        auto_draft_ready=True, enabled=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))
    return store, MailService(store, credential_store=Credentials())


def test_empty_thread_is_unknown_without_raising():
    analysis = classify_thread([])

    assert analysis.state == "unknown"
    assert analysis.next_action == "verify_thread"
    assert analysis.evidence_event_ids == ()


def test_quoted_commitment_does_not_create_a_new_owner_commitment(tmp_path):
    store, service = _service(tmp_path)
    item, _ = store.upsert_item(
        RemoteMail(
            "quoted-commitment", "Sent", "<quoted-commitment@example.com>", "quoted-thread", "owner@qq.com",
            "owner@qq.com", "teacher@example.com", "Re: 材料", "2026-10-20T09:00:00Z",
            "收到。\n\nOn Tue, teacher wrote:\n我会周五发材料。",
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "closed"
    assert state.analysis is not None
    assert state.analysis["commitments"] == []


def test_request_with_a_date_is_not_treated_as_a_counterparty_commitment(tmp_path):
    store, service = _service(tmp_path)
    item, _ = store.upsert_item(
        RemoteMail(
            "request-date", "INBOX", "<request-date@example.com>", "request-date-thread", "teacher@example.com",
            "teacher@example.com", "owner@qq.com", "材料", "2026-10-20T09:00:00Z",
            "请你周五提交材料。",
        ),
        account="owner@qq.com",
    )
    store.update_item(item.id, status="no_reply")

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "closed"
    assert state.analysis is not None
    assert state.analysis["commitments"] == []


def test_owner_started_thread_is_tracked_and_closing_message_closes_it(tmp_path):
    store, service = _service(tmp_path)
    item, _ = store.upsert_item(
        RemoteMail(
            "1", "Sent", "<owner-close@example.com>", "owner-close-thread", "owner@qq.com", "owner@qq.com",
            "teacher@example.com", "Re: 面试", "2026-10-20T09:00:00Z", "谢谢，事情已经办完了。",
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "closed"
    assert state.analysis is not None
    assert state.analysis["nextAction"] == "none"
    assert item.id in state.analysis["evidenceEventIds"]


def test_owner_informational_message_is_actioned_without_waiting_for_a_reply(tmp_path):
    store, service = _service(tmp_path)
    item, _ = store.upsert_item(
        RemoteMail(
            "informational", "Sent", "<informational@example.com>", "informational-thread", "owner@qq.com",
            "owner@qq.com", "teacher@example.com", "项目材料", "2026-10-20T09:00:00Z",
            "材料已发送，供你参考。",
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "closed"
    assert state.next_reminder_at is None
    assert state.analysis is not None
    assert state.analysis["nextAction"] == "none"


def test_owner_explicit_request_without_question_mark_waits_for_counterparty(tmp_path):
    store, service = _service(tmp_path)
    item, _ = store.upsert_item(
        RemoteMail(
            "request", "Sent", "<request@example.com>", "request-thread", "owner@qq.com",
            "owner@qq.com", "teacher@example.com", "项目材料", "2026-10-20T09:00:00Z",
            "请查看材料并告诉我意见。",
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "awaiting_reply"
    assert state.analysis is not None
    assert state.analysis["nextAction"] == "wait_counterparty"


def test_counterparty_commitment_is_waited_for_even_when_message_needs_no_reply(tmp_path):
    store, service = _service(tmp_path)
    item, _ = store.upsert_item(
        RemoteMail(
            "2", "INBOX", "<counterparty-commitment@example.com>", "counterparty-thread", "teacher@example.com",
            "teacher@example.com", "owner@qq.com", "材料安排", "2026-10-20T09:00:00Z",
            "我周五发材料，不用回复。",
        ),
        account="owner@qq.com",
    )
    store.update_item(item.id, status="no_reply")

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "awaiting_reply"
    assert state.analysis is not None
    assert state.analysis["nextAction"] == "wait_counterparty_commitment"
    assert state.analysis["commitments"][0]["owner"] == "counterparty"


def test_owner_acknowledgement_does_not_clear_an_unfulfilled_counterparty_commitment(tmp_path):
    store, service = _service(tmp_path)
    first, _ = store.upsert_item(
        RemoteMail(
            "promise", "INBOX", "<promise-ack@example.com>", "promise-ack-thread", "teacher@example.com",
            "teacher@example.com", "owner@qq.com", "材料安排", "2026-10-20T09:00:00Z",
            "我周五发材料。",
        ),
        account="owner@qq.com",
    )
    store.update_item(first.id, status="no_reply")
    store.upsert_item(
        RemoteMail(
            "ack", "Sent", "<promise-ack-out@example.com>", "promise-ack-thread", "owner@qq.com",
            "teacher@example.com", "teacher@example.com", "Re: 材料安排", "2026-10-20T10:00:00Z",
            "好的，谢谢。",
            "<promise-ack@example.com>", "<promise-ack@example.com>",
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(first.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "awaiting_reply"
    assert state.analysis is not None
    assert state.analysis["nextAction"] == "wait_counterparty_commitment"


def test_owner_commitment_remains_an_owner_action(tmp_path):
    store, service = _service(tmp_path)
    item, _ = store.upsert_item(
        RemoteMail(
            "3", "Sent", "<owner-commitment@example.com>", "owner-commitment-thread", "owner@qq.com", "owner@qq.com",
            "teacher@example.com", "材料安排", "2026-10-20T09:00:00Z", "我会明天提交材料。",
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "to_reply"
    assert state.analysis is not None
    assert state.analysis["nextAction"] == "owner_complete_commitment"
    assert state.analysis["commitments"][0]["owner"] == "owner"


def test_commitment_deadline_is_saved_and_drives_followup_reminder(tmp_path):
    store, service = _service(tmp_path)
    item, _ = store.upsert_item(
        RemoteMail(
            "deadline", "Sent", "<deadline@example.com>", "deadline-thread", "owner@qq.com", "owner@qq.com",
            "teacher@example.com", "材料安排", "2026-10-20T09:00:00Z", "我会在 2026-11-05 前提交材料。",
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    service.update_followup_preferences(enabled=True, delay_hours=1)

    state = service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "to_reply"
    assert state.analysis is not None
    commitment = state.analysis["commitments"][0]
    assert commitment["status"] == "open"
    assert commitment["dueAt"] == "2026-11-05T23:59:59+00:00"
    assert state.analysis["nextDueAt"] == commitment["dueAt"]
    assert state.next_reminder_at == commitment["dueAt"]


def test_relative_weekday_deadline_distinguishes_this_week_and_next_week():
    def make(body: str) -> MailItem:
        return MailItem(
            id="weekday",
            source_uid="weekday",
            mailbox="Sent",
            message_id="<weekday@example.com>",
            thread_id="weekday-thread",
            sender="owner@qq.com",
            reply_to="teacher@example.com",
            recipients="teacher@example.com",
            subject="安排",
            received_at="2026-10-19T09:00:00Z",  # Monday
            body_text=body,
            direction="outbound",
            status="sent",
        )

    from backend.app.mail.followup import _due_at

    assert _due_at(make("我会本周五提交材料。")) == "2026-10-23T23:59:59+00:00"
    assert _due_at(make("我会下周五提交材料。")) == "2026-10-30T23:59:59+00:00"
    assert _due_at(make("我会周五提交材料。")) == "2026-10-23T23:59:59+00:00"


def test_deadline_preserves_explicit_time_and_received_mail_timezone():
    def make(body: str, received_at: str) -> MailItem:
        return MailItem(
            id="timezone",
            source_uid="timezone",
            mailbox="Sent",
            message_id="<timezone@example.com>",
            thread_id="timezone-thread",
            sender="owner@qq.com",
            reply_to="teacher@example.com",
            recipients="teacher@example.com",
            subject="安排",
            received_at=received_at,
            body_text=body,
            direction="outbound",
            status="sent",
        )

    from backend.app.mail.followup import _due_at

    assert _due_at(make("我会明天提交材料。", "2026-10-19T23:00:00+08:00")) == "2026-10-20T15:59:59+00:00"
    assert _due_at(make("我会在 2026-11-05 17:00+08:00 提交材料。", "2026-10-20T09:00:00Z")) == "2026-11-05T09:00:00+00:00"


def test_later_completion_marks_owner_commitment_fulfilled_and_closes_thread(tmp_path):
    store, service = _service(tmp_path)
    first, _ = store.upsert_item(
        RemoteMail(
            "commitment", "Sent", "<commitment@example.com>", "fulfilled-thread", "owner@qq.com", "owner@qq.com",
            "teacher@example.com", "材料安排", "2026-10-20T09:00:00Z", "我会明天提交材料。",
        ),
        account="owner@qq.com",
        direction="outbound",
    )
    store.upsert_item(
        RemoteMail(
            "completion", "Sent", "<completion@example.com>", "fulfilled-thread", "owner@qq.com", "owner@qq.com",
            "teacher@example.com", "Re: 材料安排", "2026-10-21T09:00:00Z", "材料已提交，谢谢。",
            "<commitment@example.com>", "<commitment@example.com>",
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(first.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "closed"
    assert state.analysis is not None
    commitment = state.analysis["commitments"][0]
    assert commitment["status"] == "fulfilled"
    assert commitment["fulfilledByEventId"] != commitment["eventId"]
    assert state.analysis["nextDueAt"] is None


def test_counterparty_completion_does_not_reopen_a_closed_thread(tmp_path):
    store, service = _service(tmp_path)
    first, _ = store.upsert_item(
        RemoteMail(
            "promise", "INBOX", "<promise@example.com>", "counterparty-fulfilled", "teacher@example.com", "teacher@example.com",
            "owner@qq.com", "材料安排", "2026-10-20T09:00:00Z", "我周五发材料，不用回复。",
        ),
        account="owner@qq.com",
    )
    store.update_item(first.id, status="no_reply")
    store.upsert_item(
        RemoteMail(
            "done", "INBOX", "<done@example.com>", "counterparty-fulfilled", "teacher@example.com", "teacher@example.com",
            "owner@qq.com", "Re: 材料安排", "2026-10-21T09:00:00Z", "材料已发送。",
            "<promise@example.com>", "<promise@example.com>",
        ),
        account="owner@qq.com",
    )

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(first.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "closed"
    assert state.analysis is not None
    assert state.analysis["commitments"][0]["status"] == "fulfilled"


def test_latest_owner_reply_resolves_an_old_inbound_question(tmp_path):
    store, service = _service(tmp_path)
    incoming, _ = store.upsert_item(
        RemoteMail(
            "4", "INBOX", "<question@example.com>", "resolved-thread", "teacher@example.com", "teacher@example.com",
            "owner@qq.com", "问题", "2026-10-20T09:00:00Z", "请确认你是否参加？",
        ),
        account="owner@qq.com",
    )
    outgoing, _ = store.upsert_item(
        RemoteMail(
            "5", "Sent", "<answer@example.com>", "resolved-thread", "owner@qq.com", "owner@qq.com",
            "teacher@example.com", "Re: 问题", "2026-10-20T10:00:00Z", "我确认参加，谢谢。",
            "<question@example.com>", "<question@example.com>",
        ),
        account="owner@qq.com",
        direction="outbound",
    )
    assert incoming.thread_id == outgoing.thread_id

    service.refresh_followup_states("owner@qq.com")

    state = service.thread_followup_state(incoming.thread_id, account="owner@qq.com")
    assert state is not None
    assert state.state == "closed"
    assert state.analysis is not None
    assert state.analysis["openQuestions"] == []


def test_partial_reply_keeps_unanswered_question_in_multi_question_thread():
    def item(identifier: str, direction: str, body: str, received_at: str) -> MailItem:
        return MailItem(
            id=identifier,
            source_uid=identifier,
            mailbox="Sent" if direction == "outbound" else "INBOX",
            message_id=f"<{identifier}@example.com>",
            thread_id="multi-question",
            sender="owner@qq.com" if direction == "outbound" else "teacher@example.com",
            reply_to="teacher@example.com" if direction == "outbound" else "owner@qq.com",
            recipients="teacher@example.com" if direction == "outbound" else "owner@qq.com",
            subject="事项",
            received_at=received_at,
            body_text=body,
            direction=direction,
            status="new" if direction == "inbound" else "sent",
        )

    incoming = item("multi-in", "inbound", "请确认合同，并请确认时间。", "2026-10-20T09:00:00Z")
    outgoing = item("multi-out", "outbound", "合同已确认，谢谢。", "2026-10-20T10:00:00Z")

    analysis = classify_thread([incoming, outgoing])
    assert analysis.state == "to_reply"
    assert len(analysis.open_questions) == 1
    assert "时间" in str(analysis.open_questions[0]["summary"])
    assert analysis.open_questions[0]["questionId"] == "multi-in:q:2"
