from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


MailStatus = Literal["new", "no_reply", "queued", "processing", "drafted", "failed", "uncertain", "sent", "needs_input"]
DraftStatus = Literal["created", "manual_modified", "stale", "missing", "uncertain", "sent"]
SentAssociationState = Literal["pending", "matched", "ambiguous", "unknown"]
ThreadFollowupState = Literal["to_reply", "awaiting_reply", "closed", "unknown"]


@dataclass(frozen=True, slots=True)
class MailConnection:
    host: str
    port: int
    username: str
    use_ssl: bool = True
    enabled: bool = False
    paused: bool = False
    verified: bool = False
    auto_draft_ready: bool = False
    inbox_folder: str = ""
    sent_folder: str = ""
    drafts_folder: str = ""
    last_error: str | None = None
    last_sync_at: str | None = None
    updated_at: str = ""
    account_epoch: int = 0


@dataclass(frozen=True, slots=True)
class MailItem:
    id: str
    source_uid: str
    mailbox: str
    message_id: str
    thread_id: str
    sender: str
    reply_to: str
    recipients: str
    subject: str
    received_at: str
    body_text: str
    in_reply_to: str = ""
    references: str = ""
    status: MailStatus = "new"
    needs_reply: bool | None = None
    draft_id: str | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    direction: Literal["inbound", "outbound"] = "inbound"


@dataclass(frozen=True, slots=True)
class MailDraft:
    id: str
    mail_id: str
    remote_uid: str | None
    folder: str
    body: str
    subject: str
    recipients: str
    message_id: str
    generated_hash: str
    remote_hash: str
    status: DraftStatus
    stale: bool
    remote_missing: bool
    created_at: str
    updated_at: str
    last_error: str | None = None
    revision: int = 1


@dataclass(frozen=True, slots=True)
class MailDraftSend:
    draft_id: str
    state: SentAssociationState
    sent_uid: str | None
    sent_message_id: str | None
    sent_at: str | None
    match_basis: str
    candidate_count: int
    checked_at: str | None
    last_error: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class MailThreadState:
    thread_id: str
    state: ThreadFollowupState
    reason: str
    latest_event_at: str | None
    next_reminder_at: str | None
    reminder_enabled: bool
    event_fingerprint: str
    last_evaluated_at: str | None
    last_error: str | None
    updated_at: str
    manual_state: str | None = None
    manual_event_fingerprint: str | None = None
    analysis: dict[str, object] | None = None
    account_epoch: int = 0


@dataclass(frozen=True, slots=True)
class MailFollowupReminder:
    reminder_id: str
    thread_id: str
    due_key: str
    state: str
    due_at: str
    attempts: int
    last_error: str | None
    role_id: str | None
    created_at: str
    updated_at: str
    delivered_at: str | None
    account_epoch: int = 0


@dataclass(frozen=True, slots=True)
class MailSyncResult:
    trigger: str
    discovered: int
    created: int
    unchanged: int
    processed: int
    errors: tuple[str, ...]
    finished_at: str
