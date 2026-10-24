import asyncio
import json
import time
import pytest
from contextlib import asynccontextmanager
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.agent_runtime import CancellationToken, ToolContext
import backend.app.mail.adapter as mail_adapter_module
from backend.app.mail.adapter import MailAdapterError, QQMailAdapter, RemoteDraft, RemoteMail
from backend.app.mail.agent import MailManageTool
from backend.app.mail import api as mail_api
from backend.app.mail import service as mail_service_module
from backend.app.mail.models import MailConnection
from backend.app.mail.service import MailService
from backend.app.mail.store import MailStore


class Credentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        self.values[key] = value


class FakeAdapter:
    def __init__(self):
        self.draft_rows = []
        self.counter = 0
        self.inbox_items = []
        self.append_calls = []
        self.uid_validities = {}
        self.fetch_inbox_calls = []

    def close(self):
        pass

    def drafts(self, folder):
        return list(self.draft_rows)

    def sent_samples(self, folder, limit=8):
        return []

    def related_sent(self, folder, participant, limit=24):
        return self.sent_samples(folder, limit)

    def fetch_inbox(self, folder, after_uid=0, limit=None):
        self.fetch_inbox_calls.append((folder, after_uid, limit))
        rows = sorted(
            (item for item in self.inbox_items if item.folder == folder and int(item.uid) > after_uid),
            key=lambda item: int(item.uid),
        )
        return rows[:limit] if limit is not None else rows

    def folder_uid_validity(self, folder):
        return self.uid_validities.get(folder)

    def append_draft(self, folder, *, marker, recipients, subject, body, in_reply_to="", references=""):
        self.counter += 1
        self.append_calls.append({"folder": folder, "marker": marker, "recipients": recipients, "subject": subject, "body": body, "in_reply_to": in_reply_to, "references": references})
        row = RemoteDraft(marker, str(self.counter), folder, f"<{marker}@meido>", subject, recipients, body, f"remote-{self.counter}")
        self.draft_rows.append(row)
        return row


def make_service(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    credentials = Credentials()
    credentials.set("qq-password", "secret")
    store.save_connection(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com", verified=True, auto_draft_ready=True, enabled=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts"))
    adapter = FakeAdapter()

    async def complete(messages, configuration):
        return json.dumps({"needsReply": True, "body": "您好，收到您的邮件了。"}, ensure_ascii=False)

    service = MailService(store, credential_store=credentials, complete_model=complete, configuration_getter=lambda: object())
    service.update_preferences(writing_style="", role_access=True)
    service._adapter = lambda connection=None: adapter
    return service, store, adapter


def test_sync_restarts_from_zero_when_provider_uidvalidity_changes(tmp_path):
    service, store, adapter = make_service(tmp_path)
    first = RemoteMail(
        "1", "INBOX", "<uid-old@example.com>", "uid-old-thread", "sender@example.com", "sender@example.com",
        "owner@qq.com", "旧 UID", "2026-10-10T00:00:00Z", "旧内容", uid_validity="100",
    )
    second = RemoteMail(
        "1", "INBOX", "<uid-new@example.com>", "uid-new-thread", "sender@example.com", "sender@example.com",
        "owner@qq.com", "新 UID", "2026-10-11T00:00:00Z", "新内容", uid_validity="200",
    )
    adapter.uid_validities["INBOX"] = "100"
    adapter.inbox_items = [first]
    asyncio.run(service.sync(process=False))
    assert adapter.fetch_inbox_calls[-1][:2] == ("INBOX", 0)
    assert store.checkpoint("INBOX", account="owner@qq.com") == 1

    adapter.uid_validities["INBOX"] = "200"
    adapter.inbox_items = [second]
    asyncio.run(service.sync(process=False))

    assert adapter.fetch_inbox_calls[-1][:2] == ("INBOX", 0)
    assert store.count_items(account="owner@qq.com") == 2
    assert store.sync_state("INBOX", account="owner@qq.com")["uidValidity"] == "200"


def test_sync_paginates_large_inbox_deltas_and_advances_checkpoint_per_page(tmp_path):
    service, store, adapter = make_service(tmp_path)
    adapter.inbox_items = [
        RemoteMail(
            str(uid), "INBOX", f"<page-{uid}@example.com>", f"page-thread-{uid}", "sender@example.com",
            "sender@example.com", "owner@qq.com", f"主题 {uid}", "2026-10-20T00:00:00Z", f"内容 {uid}",
        )
        for uid in range(1, 402)
    ]

    result = asyncio.run(service.sync(process=False))

    assert result.discovered == 401
    assert store.count_items(account="owner@qq.com") == 401
    assert store.checkpoint("INBOX", account="owner@qq.com") == 401
    assert [call[1] for call in adapter.fetch_inbox_calls] == [0, 200, 400]


def test_adapter_inbox_pages_read_oldest_uid_first():
    messages = {}
    for uid in range(1, 7):
        message = EmailMessage()
        message["Message-ID"] = f"<page-{uid}@example.com>"
        message["From"] = "sender@example.com"
        message["To"] = "owner@qq.com"
        message["Subject"] = f"主题 {uid}"
        message["Date"] = "Tue, 20 Oct 2026 09:00:00 +0000"
        message.set_content(f"内容 {uid}")
        messages[str(uid).encode()] = message.as_bytes()

    class Client:
        def select(self, _folder, readonly=True):
            assert readonly is True
            return "OK", [b"6"]

        def uid(self, command, uid, _query):
            if command == "SEARCH":
                return "OK", [b"6 2 5 1 4 3"]
            return "OK", [(uid + b" BODY[]", messages[uid])]

    adapter = QQMailAdapter(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"), "secret")
    adapter._imap = Client()

    first = adapter.fetch_inbox("INBOX", 0, limit=2)
    second = adapter.fetch_inbox("INBOX", int(first[-1].uid), limit=2)
    sent_first = adapter.fetch_sent("Sent", 0, limit=2)
    sent_incremental = adapter.fetch_sent("Sent", 2, limit=2)

    assert [item.uid for item in first] == ["1", "2"]
    assert [item.uid for item in second] == ["3", "4"]
    assert [item.uid for item in sent_first] == ["5", "6"]
    assert [item.uid for item in sent_incremental] == ["3", "4"]


def test_adapter_sent_without_reply_to_preserves_recipients_for_followup():
    message = EmailMessage()
    message["Message-ID"] = "<sent-no-reply-to@example.com>"
    message["From"] = "owner@qq.com"
    message["To"] = "teacher@example.com"
    message["Subject"] = "Re: 项目进度"
    message["Date"] = "Tue, 20 Oct 2026 09:00:00 +0000"
    message.set_content("请确认预计完成时间。")
    raw = message.as_bytes()

    class Client:
        def select(self, _folder, readonly=True):
            assert readonly is True
            return "OK", [b"1"]

        def uid(self, command, uid, _query):
            if command == "SEARCH":
                return "OK", [b"1"]
            return "OK", [(uid + b" BODY[]", raw)]

    adapter = QQMailAdapter(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"), "secret")
    adapter._imap = Client()

    sent = adapter.fetch_sent("Sent")

    assert sent[0].reply_to == ""
    assert sent[0].recipients == "teacher@example.com"


def test_adapter_reconnects_read_only_fetch_after_transient_imap_failure(monkeypatch):
    message = EmailMessage()
    message["Message-ID"] = "<retry@example.com>"
    message["From"] = "sender@example.com"
    message["To"] = "owner@qq.com"
    message["Subject"] = "重连"
    message["Date"] = "Tue, 20 Oct 2026 09:00:00 +0000"
    message.set_content("连接恢复后读取")
    raw = message.as_bytes()

    class Client:
        def __init__(self, fail_search=False):
            self.fail_search = fail_search
            self.logged_out = False

        def select(self, _folder, readonly=True):
            assert readonly is True
            return "OK", [b"1"]

        def logout(self):
            self.logged_out = True

        def uid(self, command, uid, _query):
            if command == "SEARCH":
                if self.fail_search:
                    self.fail_search = False
                    raise OSError("connection reset")
                return "OK", [b"1"]
            return "OK", [(uid + b" BODY[]", raw)]

    first = Client(fail_search=True)
    second = Client()
    adapter = QQMailAdapter(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"), "secret")
    adapter._imap = first
    monkeypatch.setattr(adapter, "_connect", lambda: second)
    sleeps = []
    monkeypatch.setattr(mail_adapter_module.time, "sleep", sleeps.append)

    rows = adapter.fetch_inbox("INBOX", 0, limit=2)

    assert [row.uid for row in rows] == ["1"]
    assert first.logged_out is True
    assert sleeps == [mail_adapter_module.READ_RETRY_BASE_DELAY_SECONDS]


def test_mail_status_exposes_sync_queue_and_reconciliation_state(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    store.prepare_sync_state("INBOX", account="owner@qq.com", uid_validity="100")
    service.enqueue_job("process", target_id="mail-1", request_id="observability")

    status = service.public_status()
    stats = service.mail_stats()

    assert status["syncStates"] == [{
        "folder": "INBOX", "lastUid": 0, "uidValidity": "100", "updatedAt": status["syncStates"][0]["updatedAt"],
    }]
    assert status["queue"]["jobs"]["queued"] == 1
    assert status["reconciliation"]["total"] == 0
    assert stats["queue"]["jobs"]["queued"] == 1


def add_owner_item(store, remote):
    return store.upsert_item(remote, account="owner@qq.com")


def test_mail_processing_is_idempotent_and_keeps_reply_metadata(tmp_path):
    service, store, adapter = make_service(tmp_path)
    item, created = add_owner_item(store, SimpleNamespace(
        uid="12", folder="INBOX", message_id="<source@example.com>", thread_id="thread-1", sender="Alice <alice@example.com>",
        reply_to="alice@example.com", recipients="owner@qq.com", subject="项目进度", received_at="2026-10-14T00:00:00Z", body_text="请问进度如何？", in_reply_to="", references="",
    ))
    assert created is True

    result = asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))

    assert result.status == "drafted"
    assert len(store.list_drafts()) == 1
    assert store.list_drafts()[0].recipients == "alice@example.com"
    assert store.list_drafts()[0].message_id.startswith("<draft-")

    repeat = asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))
    assert repeat.status == "drafted"
    assert len(store.list_drafts()) == 1
    assert len(adapter.draft_rows) == 1


def test_retry_repairs_item_state_when_local_draft_survived_a_crash(tmp_path):
    service, store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="18", folder="INBOX", message_id="<recovered-state@example.com>", thread_id="recovered-state-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="恢复状态", received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="", references="",
    ))
    asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))
    saved = store.draft_for_task(f"auto:{item.id}:v1", account="owner@qq.com")
    store.update_item(item.id, status="processing", needs_reply=None, draft_id=None, last_error="")

    recovered = asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))

    assert recovered.status == "drafted"
    assert recovered.needs_reply is True
    assert recovered.draft_id == saved.id
    assert len(adapter.draft_rows) == 1


def test_mail_processing_requires_completed_qq_draft_interop(tmp_path):
    service, store, adapter = make_service(tmp_path)
    store.update_connection(verified=False, auto_draft_ready=False)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="15", folder="INBOX", message_id="<not-ready@example.com>", thread_id="not-ready-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="未验证", received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="", references="",
    ))

    with pytest.raises(ValueError, match="QQ 草稿能力"):
        asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))

    assert adapter.append_calls == []
    assert store.get_item(item.id, account="owner@qq.com").status == "new"


def test_role_cannot_create_a_draft_before_qq_draft_interop(tmp_path):
    service, store, adapter = make_service(tmp_path)
    store.update_connection(verified=False, auto_draft_ready=False)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="16", folder="INBOX", message_id="<role-not-ready@example.com>", thread_id="role-not-ready-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="未验证角色稿", received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="", references="",
    ))

    with pytest.raises(ValueError, match="QQ 草稿能力"):
        asyncio.run(service.role_action("draft", mail_id=item.id, query="回复内容", request_id="draft-before-ready", role_id="role-1", run_id="run-1"))

    assert adapter.append_calls == []
    assert store.list_drafts_for_mail(item.id, account="owner@qq.com") == []


def test_reply_draft_references_include_the_source_message_id(tmp_path):
    service, _store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(_store, SimpleNamespace(
        uid="13", folder="INBOX", message_id="<source-root@example.com>", thread_id="thread-root", sender="Alice <alice@example.com>",
        reply_to="alice@example.com", recipients="owner@qq.com", subject="根邮件", received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="", references="",
    ))

    asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))

    assert adapter.append_calls[0]["in_reply_to"] == "<source-root@example.com>"
    assert adapter.append_calls[0]["references"] == "<source-root@example.com>"


def test_reply_draft_does_not_address_owner_when_reply_to_is_self(tmp_path):
    service, store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="13-self", folder="INBOX", message_id="<self-reply-to@example.com>", thread_id="self-reply-to-thread",
        sender="Alice <alice@example.com>", reply_to="owner@qq.com", recipients="owner@qq.com", subject="回复",
        received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="", references="",
    ))

    asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))

    assert adapter.append_calls[0]["recipients"] == "Alice <alice@example.com>"


def test_reply_draft_preserves_existing_reference_chain(tmp_path):
    service, _store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(_store, SimpleNamespace(
        uid="14", folder="INBOX", message_id="<source-leaf@example.com>", thread_id="thread-chain", sender="Alice <alice@example.com>",
        reply_to="alice@example.com", recipients="owner@qq.com", subject="回复", received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="<source-parent@example.com>", references="<source-root@example.com> <source-parent@example.com>",
    ))

    asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))

    assert adapter.append_calls[0]["references"] == "<source-root@example.com> <source-parent@example.com> <source-leaf@example.com>"


def test_auto_draft_prompt_includes_thread_owner_context_and_sent_samples_without_qq_credentials(tmp_path):
    service, store, adapter = make_service(tmp_path)
    root, _ = add_owner_item(store, SimpleNamespace(
        uid="30", folder="INBOX", message_id="<prompt-root@example.com>", thread_id="prompt-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="原邮件", received_at="2026-10-13T00:00:00Z", body_text="约定周五联系。", in_reply_to="", references="",
    ))
    current, _ = add_owner_item(store, SimpleNamespace(
        uid="31", folder="INBOX", message_id="<prompt-current@example.com>", thread_id=root.thread_id, sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="跟进", received_at="2026-10-14T00:00:00Z", body_text="请确认时间。", in_reply_to=root.message_id, references=root.message_id,
    ))
    service.owner_search = lambda _query: asyncio.sleep(0, result=[SimpleNamespace(text="主人偏好：工作日使用简洁称呼。")])
    adapter.sent_samples = lambda _folder, _limit=8: [RemoteMail("42", "Sent", "<sample@example.com>", "sample-thread", "owner@qq.com", "owner@qq.com", "sender@example.com", "已发送样本", "2026-10-10T00:00:00Z", "您好，我会在周五回复。")]
    captured = {}

    async def complete(messages, configuration):
        captured["messages"] = messages
        captured["configuration"] = configuration
        return json.dumps({"needsReply": True, "body": "您好，周五可以。"}, ensure_ascii=False)

    service.complete_model = complete

    result = asyncio.run(service.process_item(current.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))

    prompt = json.loads(captured["messages"][1]["content"])
    system_prompt = captured["messages"][0]["content"]
    assert result.status == "drafted"
    assert adapter.draft_rows[0].body == "您好，周五可以。"
    assert any(entry["body"] == "约定周五联系。" for entry in prompt["thread"])
    assert prompt["ownerContext"] == "主人偏好：工作日使用简洁称呼。"
    assert prompt["sentSamples"] == [{"subject": "已发送样本", "body": "您好，我会在周五回复。"}]
    assert "不要复述或逐句镜像来信" in system_prompt
    assert "使用最新邮件的主要语言" in system_prompt
    assert "不要自行提出可用时间" in system_prompt
    assert "secret" not in json.dumps(captured["messages"], ensure_ascii=False)
    assert "candidate-secret" not in json.dumps(captured["messages"], ensure_ascii=False)


def test_qq_adapter_serializes_reply_thread_headers():
    class Client:
        raw = b""

        def append(self, _folder, _flags, _date, raw):
            self.raw = raw
            return "OK", [b"APPENDUID"]

    adapter = QQMailAdapter(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"), "secret")
    adapter._imap = Client()

    adapter.append_draft("Drafts", marker="draft-thread-test", recipients="sender@example.com", subject="Re: topic", body="reply", in_reply_to="<leaf@example.com>", references="<root@example.com> <parent@example.com> <leaf@example.com>")
    message = BytesParser(policy=policy.default).parsebytes(adapter._imap.raw)

    assert message["In-Reply-To"] == "<leaf@example.com>"
    assert message["References"] == "<root@example.com> <parent@example.com> <leaf@example.com>"


def test_regenerate_creates_new_draft_without_overwriting_existing(tmp_path):
    service, store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="7", folder="INBOX", message_id="<source-2@example.com>", thread_id="thread-2", sender="Bob <bob@example.com>",
        reply_to="bob@example.com", recipients="owner@qq.com", subject="合同", received_at="2026-10-14T00:00:00Z", body_text="请确认。", in_reply_to="", references="",
    ))
    asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))
    first = store.list_drafts()[0]
    store.update_draft(first.id, status="manual_modified", stale=True, remote_hash="manually-edited")

    asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}, regenerate=True, request_id="owner-request-1"))

    drafts = store.list_drafts()
    assert len(drafts) == 2
    assert sorted(d.revision for d in drafts) == [1, 2]
    assert any(d.status == "manual_modified" for d in drafts)
    assert len(adapter.draft_rows) == 2


def test_mail_runtime_tool_does_not_expose_credentials(tmp_path):
    service, store, adapter = make_service(tmp_path)
    tool = MailManageTool(service)
    context = ToolContext("role-1", "role:role-1", "run-1", CancellationToken(), "call-1")

    result = asyncio.run(tool.execute({"action": "status"}, context))

    assert result.is_error is False
    assert "secret" not in result.content
    assert "qq-password" not in result.content


def test_mail_runtime_tool_can_search_synchronized_mail(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    add_owner_item(store, SimpleNamespace(
        uid="701", folder="INBOX", message_id="<role-search-1@example.com>", thread_id="role-search-1",
        sender="teacher@example.com", reply_to="teacher@example.com", recipients="owner@qq.com", subject="项目排期",
        received_at="2026-10-20T09:00:00Z", body_text="请确认下周安排", in_reply_to="", references="",
    ))
    add_owner_item(store, SimpleNamespace(
        uid="702", folder="INBOX", message_id="<role-search-2@example.com>", thread_id="role-search-2",
        sender="other@example.com", reply_to="other@example.com", recipients="owner@qq.com", subject="其他主题",
        received_at="2026-10-20T10:00:00Z", body_text="没有相关内容", in_reply_to="", references="",
    ))
    tool = MailManageTool(service)
    context = ToolContext("role-1", "role:role-1", "run-search", CancellationToken(), "call-search")

    result = asyncio.run(tool.execute({"action": "list", "query": "排期"}, context))

    assert result.is_error is False
    assert [item["subject"] for item in json.loads(result.content)["items"]] == ["项目排期"]


def test_mail_runtime_tool_returns_account_scoped_inbox_stats(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="703", folder="INBOX", message_id="<stats@example.com>", thread_id="stats-thread",
        sender="teacher@example.com", reply_to="teacher@example.com", recipients="owner@qq.com", subject="统计",
        received_at="2026-10-20T09:00:00Z", body_text="请查看", in_reply_to="", references="",
    ))
    store.update_item(item.id, status="needs_input", needs_reply=True)
    tool = MailManageTool(service)
    context = ToolContext("role-1", "role:role-1", "run-stats", CancellationToken(), "call-stats")

    result = asyncio.run(tool.execute({"action": "stats"}, context))

    payload = json.loads(result.content)
    assert result.is_error is False
    assert payload["total"] == 1
    assert payload["byStatus"]["needs_input"] == 1
    assert "body" not in payload


def test_new_mail_in_existing_thread_marks_previous_draft_stale(tmp_path):
    service, store, adapter = make_service(tmp_path)
    original, _ = add_owner_item(store, SimpleNamespace(
        uid="1", folder="INBOX", message_id="<root@example.com>", thread_id="thread-shared", sender="Alice <alice@example.com>",
        reply_to="alice@example.com", recipients="owner@qq.com", subject="安排", received_at="2026-10-13T00:00:00Z", body_text="周五有空吗？", in_reply_to="", references="",
    ))
    store.save_draft(original.id, task_key="auto:original", remote_uid="1", folder="Drafts", body="周五可以。", subject="Re: 安排", recipients="alice@example.com", message_id="<draft-original>", generated_hash="g1", remote_hash="r1", draft_id="draft-original")
    adapter.draft_rows.append(RemoteDraft("draft-original", "1", "Drafts", "<draft-original>", "Re: 安排", "alice@example.com", "周五可以。", "r1"))
    adapter.inbox_items.append(RemoteMail("2", "INBOX", "<reply@example.com>", "thread-shared", "Alice <alice@example.com>", "alice@example.com", "owner@qq.com", "Re: 安排", "2026-10-14T00:00:00Z", "那改成周六可以吗？", "<root@example.com>", "<root@example.com>"))

    result = asyncio.run(service.sync(trigger="manual", process=False))

    draft = store.list_drafts_for_mail(original.id)[0]
    assert result.created == 1
    assert draft.status == "stale"
    assert draft.stale is True


def test_reminder_reports_failure_even_without_a_created_draft(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    messages = []
    service.available_roles = lambda: ["role-1"]
    service.reminder_sender = lambda role_id, event_id, content: messages.append((role_id, event_id, content)) or event_id
    store.set_setting("reminder_role_id", "role-1")
    store.create_reminder_batch("batch-errors", [], failure_count=1, failure_summaries=["预算确认：模型不可用"])

    asyncio.run(service.deliver_reminder_batches())

    assert len(messages) == 1
    assert "1 封邮件处理失败或待核实" in messages[0][2]
    assert "预算确认：模型不可用" in messages[0][2]
    assert store.pending_reminder_batches() == []


def test_reminder_claims_are_single_owner_and_restart_recovery_reopens_inflight(tmp_path):
    _service, store, _adapter = make_service(tmp_path)
    account = "owner@qq.com"
    epoch = store.connection().account_epoch
    store.create_reminder_batch("claim-batch", [], account=account)
    assert store.claim_reminder_batch("claim-batch", account=account) is True
    assert store.claim_reminder_batch("claim-batch", account=account) is False

    store.create_followup_reminder(
        account,
        "claim-thread",
        due_key="claim-key",
        due_at="2026-10-01T00:00:00+00:00",
        account_epoch=epoch,
    )
    reminder = store.followup_reminders(account=account)[0]
    assert store.claim_followup_reminder(reminder.reminder_id, account=account, account_epoch=epoch) is True
    assert store.claim_followup_reminder(reminder.reminder_id, account=account, account_epoch=epoch) is False

    store.recover_interrupted_reminders()

    batch = store.reminder_batches(account=account)[0]
    recovered = store.followup_reminders(account=account)[0]
    assert batch["status"] == "failed"
    assert batch["lastError"] == "服务停止后恢复提醒投递"
    assert recovered.state == "failed"
    assert recovered.last_error == "服务停止后恢复提醒投递"


def test_uncertain_draft_state_is_included_in_the_reminder_batch(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="19", folder="INBOX", message_id="<uncertain-reminder@example.com>", thread_id="uncertain-reminder-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="草稿待核实", received_at="2026-10-14T00:00:00Z", body_text="请确认", in_reply_to="", references="",
    ))
    draft = store.save_draft(item.id, task_key=f"auto:{item.id}:v1", remote_uid="1", folder="Drafts", body="reply", subject="Re: 草稿待核实", recipients="sender@example.com", message_id="<old-draft>", generated_hash="g", remote_hash="r")
    store.update_item(item.id, status="uncertain", needs_reply=True, draft_id=draft.id, last_error="QQ 草稿已删除")

    result = asyncio.run(service.process_pending())

    assert result["errors"]
    assert "草稿待核实" in result["errors"][0]
    assert "QQ 草稿已删除" in result["errors"][0]
    batch = store.reminder_batches(account="owner@qq.com")[0]
    assert batch["failureCount"] == 1
    assert "QQ 草稿已删除" in batch["failureSummaries"][0]


def test_recovered_existing_draft_is_not_reported_as_a_new_reminder(tmp_path):
    service, store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="20", folder="INBOX", message_id="<recovered-reminder@example.com>", thread_id="recovered-reminder-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="已有草稿", received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="", references="",
    ))
    asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))
    original_draft = store.draft_for_task(f"auto:{item.id}:v1", account="owner@qq.com")
    store.update_item(item.id, status="uncertain", needs_reply=True, draft_id=original_draft.id, last_error="服务重启后任务结果待核实")

    result = asyncio.run(service.process_pending())

    assert result["processed"] == 1
    assert result["draftIds"] == []
    assert store.reminder_batches(account="owner@qq.com") == []


def test_reminder_batch_waits_without_a_role_then_delivers_when_configured(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    messages = []
    service._create_reminder_batch([], ["主题：模型不可用"], batch_key="job:missing-role")

    asyncio.run(service.deliver_reminder_batches())

    batch = store.reminder_batches(account="owner@qq.com")[0]
    assert batch["status"] == "pending"
    assert batch["lastError"] == "尚未指定邮件提醒角色"
    assert batch["failureCount"] == 1
    assert messages == []

    service.available_roles = lambda: ["role-1"]
    service.reminder_sender = lambda role_id, event_id, content: messages.append((role_id, event_id, content)) or event_id
    service.configure_reminder_role("role-1")
    asyncio.run(service.deliver_reminder_batches())

    delivered = store.reminder_batches(account="owner@qq.com")[0]
    assert delivered["status"] == "delivered"
    assert delivered["lastError"] is None
    assert delivered["deliveredAt"]
    assert len(messages) == 1


def test_reminder_batch_api_returns_only_the_active_account(tmp_path, monkeypatch):
    service, store, _adapter = make_service(tmp_path)
    store.create_reminder_batch("owner-batch", [], failure_count=1, account="owner@qq.com")
    store.create_reminder_batch("other-batch", [], failure_count=1, account="other@qq.com")
    monkeypatch.setattr(mail_api, "mail_service", service)

    app = FastAPI()
    app.include_router(mail_api.router)
    with TestClient(app) as client:
        response = client.get("/api/mail/reminder-batches?limit=8")

    assert response.status_code == 200
    batches = response.json()["batches"]
    assert [batch["batchId"] for batch in batches] == ["owner-batch"]


def test_reminder_batches_and_content_are_scoped_to_the_active_account(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    remote = SimpleNamespace(
        uid="1", folder="INBOX", message_id="<other@example.com>", thread_id="other-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="old@qq.com", subject="旧账号主题", received_at="2026-10-14T00:00:00Z", body_text="body", in_reply_to="", references="",
    )
    item, _ = store.upsert_item(remote, account="old@qq.com")
    draft = store.save_draft(item.id, task_key="old-draft", remote_uid="1", folder="Drafts", body="reply", subject="Re: 旧账号主题", recipients="sender@example.com", message_id="<draft-old>", generated_hash="g", remote_hash="r")
    store.create_reminder_batch("old-account-batch", [draft.id], account="old@qq.com")
    messages = []
    service.available_roles = lambda: ["role-1"]
    service.reminder_sender = lambda role_id, event_id, content: messages.append(content) or event_id
    store.set_setting("reminder_role_id", "role-1")

    asyncio.run(service.deliver_reminder_batches())

    assert messages == []
    assert len(store.pending_reminder_batches(account="old@qq.com")) == 1
    assert store.pending_reminder_batches(account="owner@qq.com") == []


def test_reminder_batch_deduplicates_replays_but_not_later_identical_windows(tmp_path):
    service, store, _adapter = make_service(tmp_path)

    service._create_reminder_batch([], ["主题：模型不可用"], batch_key="job:one")
    service._create_reminder_batch([], ["主题：模型不可用"], batch_key="job:one")
    service._create_reminder_batch([], ["主题：模型不可用"], batch_key="job:two")

    batches = store.pending_reminder_batches(account="owner@qq.com")
    assert len(batches) == 2
    assert all(batch["failureCount"] == 1 for batch in batches)


def test_connection_retry_reuses_test_draft_after_readback_failure(tmp_path, monkeypatch):
    service, store, _adapter = make_service(tmp_path)
    service.configure(host="imap.qq.com", port=993, username="owner@qq.com", password="candidate-secret")
    state = {"markers": set(), "readback_error": True, "append_calls": 0}

    class CandidateAdapter:
        def __init__(self, _connection, _password):
            pass

        def verify(self, write_test=False):
            assert write_test is False
            return {"folders": {"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}}

        def verify_test_draft_present(self, marker, _folder):
            if marker in state["markers"] and state["readback_error"]:
                state["readback_error"] = False
                raise MailAdapterError("草稿箱读取暂时失败")
            return marker in state["markers"]

        def append_test_draft(self, _folder, marker, _recipient):
            state["append_calls"] += 1
            state["markers"].add(marker)

        def close(self):
            pass

    monkeypatch.setattr(mail_service_module, "QQMailAdapter", CandidateAdapter)

    with pytest.raises(MailAdapterError, match="草稿箱读取暂时失败"):
        service.test_connection(write_test=True)
    first_marker = str(store.candidate()["marker"])
    assert store.test_draft()["status"] == "failed"
    assert state["append_calls"] == 1

    result = service.test_connection(write_test=True)

    assert result["candidate"]["marker"] == first_marker
    assert result["candidate"]["test_draft_present"] == 1
    assert store.test_draft()["status"] == "created"
    assert state["append_calls"] == 1


@pytest.mark.parametrize("failure_stage", ["credential", "database"])
def test_candidate_activation_failure_preserves_the_existing_connection(tmp_path, monkeypatch, failure_stage):
    service, store, _adapter = make_service(tmp_path)
    credentials = service.credentials
    service.configure(host="imap.qq.com", port=993, username="new@qq.com", password="candidate-secret")
    marker = "meido-test-candidate"
    folders = {"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}
    store.set_candidate_marker(marker, folders, present=True)
    store.create_test_draft(marker)
    store.mark_test_draft_created(marker)

    class CandidateAdapter:
        def __init__(self, _connection, _password):
            pass

        def verify_test_sent(self, _marker, _folders):
            return True

        def close(self):
            pass

    monkeypatch.setattr(mail_service_module, "QQMailAdapter", CandidateAdapter)
    if failure_stage == "credential":
        original_set = credentials.set

        def fail_active_secret(key, value):
            if key == "qq-password" and value == "candidate-secret":
                raise RuntimeError("credential write failed")
            original_set(key, value)

        credentials.set = fail_active_secret
    else:
        def fail_save(*_args, **_kwargs):
            raise RuntimeError("connection save failed")

        monkeypatch.setattr(store, "save_connection", fail_save)

    with pytest.raises(RuntimeError):
        service.confirm_test_draft_sent()

    assert store.connection().username == "owner@qq.com"
    assert store.connection().auto_draft_ready is True
    assert credentials.get("qq-password") == "secret"
    assert credentials.get("candidate-password") == "candidate-secret"
    assert store.candidate()["test_sent"] == 0
    assert store.test_draft()["status"] == "created"


def test_candidate_activation_commits_connection_and_test_state_together(tmp_path, monkeypatch):
    service, store, _adapter = make_service(tmp_path)
    service.configure(host="imap.qq.com", port=993, username="new@qq.com", password="candidate-secret")
    marker = "meido-test-success"
    folders = {"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}
    store.set_candidate_marker(marker, folders, present=True)
    store.create_test_draft(marker)
    store.mark_test_draft_created(marker)

    class CandidateAdapter:
        def __init__(self, _connection, _password):
            pass

        def verify_test_sent(self, _marker, _folders):
            return True

        def close(self):
            pass

    monkeypatch.setattr(mail_service_module, "QQMailAdapter", CandidateAdapter)

    result = service.confirm_test_draft_sent()

    assert result["username"] == "new@qq.com"
    assert result["autoDraftReady"] is True
    assert store.connection().username == "new@qq.com"
    assert store.candidate()["test_sent"] == 1
    assert store.test_draft()["status"] == "sent"
    assert service.credentials.get("qq-password") == "candidate-secret"
    assert service.credentials.get("candidate-password") == ""


def test_retry_does_not_report_a_deleted_qq_draft_as_available(tmp_path):
    service, store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="8", folder="INBOX", message_id="<deleted-draft@example.com>", thread_id="deleted-draft-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="已删除草稿", received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="", references="",
    ))
    asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))
    adapter.draft_rows.clear()
    service._refresh_remote_drafts(adapter, "Drafts", "owner@qq.com")
    assert store.get_item(item.id, account="owner@qq.com").status == "uncertain"

    retried = asyncio.run(service.process_item(item.id, adapter=adapter, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))

    assert retried.status == "uncertain"
    assert "已删除" in retried.last_error
    assert store.list_drafts_for_mail(item.id, account="owner@qq.com")[0].status == "missing"
    assert adapter.draft_rows == []


def test_sync_reports_remote_draft_refresh_failure(tmp_path):
    service, _store, adapter = make_service(tmp_path)
    adapter.drafts = lambda _folder: (_ for _ in ()).throw(MailAdapterError("草稿箱读取失败"))

    result = asyncio.run(service.sync(trigger="manual", process=False))

    assert result.errors
    assert "草稿箱读取失败" in result.errors[0]


def test_role_thread_action_audits_partial_failures(tmp_path, monkeypatch):
    service, store, _adapter = make_service(tmp_path)

    async def process_thread(*_args, **_kwargs):
        return {"threadId": "thread-1", "processed": 1, "draftIds": [], "errors": ["subject: model unavailable"]}

    monkeypatch.setattr(service, "process_thread", process_thread)
    result = asyncio.run(service.role_action("process_thread", thread_id="thread-1", role_id="role-1", run_id="run-1"))

    assert result["errors"]
    assert store.list_audits()[0]["status"] == "partial"


def test_role_can_explicitly_resume_the_mail_service(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    store.update_connection(enabled=False, paused=True)

    result = asyncio.run(service.role_action("resume", role_id="role-1", run_id="resume-run"))

    assert result["enabled"] is True
    assert result["paused"] is False
    assert store.list_audits()[0]["operation"] == "resume"


def test_role_created_draft_is_idempotent_and_read_operations_are_audited(tmp_path):
    service, store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="3", folder="INBOX", message_id="<source-3@example.com>", thread_id="thread-3", sender="Carol <carol@example.com>",
        reply_to="carol@example.com", recipients="owner@qq.com", subject="进度", received_at="2026-10-14T00:00:00Z", body_text="有进展吗？", in_reply_to="", references="",
    ))

    first = asyncio.run(service.role_action("draft", mail_id=item.id, query="正在处理中。", request_id="draft-request", role_id="role-1", run_id="run-1"))
    second = asyncio.run(service.role_action("draft", mail_id=item.id, query="正在处理中。", request_id="draft-request", role_id="role-1", run_id="run-2"))
    asyncio.run(service.role_action("read", mail_id=item.id, role_id="role-1", run_id="run-2"))

    assert first["draftId"] == second["draftId"]
    assert len(adapter.draft_rows) == 1
    operations = [event["operation"] for event in store.list_audits()]
    assert operations.count("read") == 1
    assert operations.count("draft") == 2


def test_role_can_create_a_new_version_from_an_existing_draft(tmp_path):
    service, store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="20", folder="INBOX", message_id="<source-20@example.com>", thread_id="thread-20", sender="Carol <carol@example.com>",
        reply_to="carol@example.com", recipients="owner@qq.com", subject="版本", received_at="2026-10-14T00:00:00Z", body_text="请确认。", in_reply_to="", references="",
    ))

    first = asyncio.run(service.role_action("draft", mail_id=item.id, query="原始回复", request_id="draft-v1", role_id="role-1", run_id="run-1"))
    original_remote = adapter.draft_rows[0]
    adapter.draft_rows[0] = RemoteDraft(original_remote.marker, original_remote.uid, original_remote.folder, original_remote.message_id, original_remote.subject, original_remote.recipients, "主人改过的回复", "manually-edited")
    second = asyncio.run(service.role_action("draft", mail_id=item.id, draft_id=first["draftId"], query="修改后的回复", request_id="draft-v2", role_id="role-1", run_id="run-2"))
    retry = asyncio.run(service.role_action("draft", mail_id=item.id, draft_id=first["draftId"], query="修改后的回复", request_id="draft-v2", role_id="role-1", run_id="run-3"))

    drafts = store.list_drafts_for_mail(item.id, account="owner@qq.com")
    assert second["draftId"] != first["draftId"]
    assert retry["draftId"] == second["draftId"]
    assert len(drafts) == 2
    assert {draft.body for draft in drafts} == {"原始回复", "修改后的回复"}
    original = next(draft for draft in drafts if draft.id == first["draftId"])
    assert original.status == "manual_modified"
    assert original.stale is True
    assert len(adapter.draft_rows) == 2


def test_persisted_job_worker_returns_final_result(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="4", folder="INBOX", message_id="<source-4@example.com>", thread_id="thread-4", sender="Drew <drew@example.com>",
        reply_to="drew@example.com", recipients="owner@qq.com", subject="报告", received_at="2026-10-14T00:00:00Z", body_text="请查看报告。", in_reply_to="", references="",
    ))

    async def run_job():
        service.start()
        accepted = service.enqueue_job("process", target_id=item.id, request_id="web-request-1", payload={"requestId": "web-request-1"})
        duplicate = service.enqueue_job("process", target_id=item.id, request_id="web-request-1", payload={"requestId": "web-request-1"})
        for _ in range(500):
            current = service.job_status(str(accepted["jobId"]))
            if current and current["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.01)
        completed = service.job_status(str(accepted["jobId"]))
        await service.close()
        return accepted, duplicate, completed

    accepted, duplicate, completed = asyncio.run(run_job())

    assert duplicate["jobId"] == accepted["jobId"]
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["result"]["status"] == "drafted"


def test_status_counts_are_not_limited_by_page_size_and_all_drafts_are_retained(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    for number in range(505):
        item, _ = store.upsert_item(SimpleNamespace(
            uid=str(number), folder="INBOX", message_id=f"<{number}@example.com>", thread_id=f"thread-{number}", sender="sender@example.com",
            reply_to="sender@example.com", recipients="owner@qq.com", subject=f"subject {number}", received_at=f"2026-10-{(number % 28) + 1:02}T00:00:00Z", body_text="hello", in_reply_to="", references="",
        ))
        store.save_draft(item.id, task_key=f"draft-task-{number}", remote_uid=str(number), folder="Drafts", body="reply", subject=f"Re: {number}", recipients="sender@example.com", message_id=f"<draft-{number}>", generated_hash="g", remote_hash="r", draft_id=f"draft-{number}")

    assert store.public_status()["items"] == 505
    assert store.public_status()["drafts"] == 505
    assert len(store.all_drafts()) == 505


def test_web_task_api_accepts_job_and_exposes_terminal_result(tmp_path, monkeypatch):
    service, store, _adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="5", folder="INBOX", message_id="<source-5@example.com>", thread_id="thread-5", sender="Eli <eli@example.com>",
        reply_to="eli@example.com", recipients="owner@qq.com", subject="交付", received_at="2026-10-14T00:00:00Z", body_text="请确认交付时间。", in_reply_to="", references="",
    ))
    monkeypatch.setattr(mail_api, "mail_service", service)

    @asynccontextmanager
    async def lifespan(_app):
        service.start()
        yield
        await service.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(mail_api.router)
    with TestClient(app) as client:
        accepted = client.post(f"/api/mail/items/{item.id}/process", json={"requestId": "http-request-1"})
        assert accepted.status_code == 200
        job_id = accepted.json()["job"]["jobId"]
        assert accepted.json()["job"]["status"] in {"queued", "running"}

        for _ in range(200):
            response = client.get(f"/api/mail/jobs/{job_id}")
            if response.json()["job"]["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)

        assert response.status_code == 200
        assert response.json()["job"]["status"] == "completed"
        assert response.json()["job"]["result"]["status"] == "drafted"


def test_web_api_rejects_draft_work_until_qq_draft_interop_is_complete(tmp_path, monkeypatch):
    service, store, _adapter = make_service(tmp_path)
    store.update_connection(verified=False, auto_draft_ready=False)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="17", folder="INBOX", message_id="<api-not-ready@example.com>", thread_id="api-not-ready-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="未验证 API", received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="", references="",
    ))
    monkeypatch.setattr(mail_api, "mail_service", service)

    app = FastAPI()
    app.include_router(mail_api.router)
    with TestClient(app) as client:
        response = client.post(f"/api/mail/items/{item.id}/process", json={"requestId": "before-ready"})

    assert response.status_code == 409
    assert "QQ 草稿能力" in response.json()["detail"]
    assert service.job_status(response.json().get("job", {}).get("jobId", "missing")) is None


def test_mail_records_and_checkpoints_are_scoped_to_the_qq_account(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    remote = SimpleNamespace(
        uid="1", folder="INBOX", message_id="<same@example.com>", thread_id="thread-same", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="same", received_at="2026-10-14T00:00:00Z", body_text="body", in_reply_to="", references="",
    )

    first, created_first = store.upsert_item(remote, account="first@qq.com")
    second, created_second = store.upsert_item(remote, account="second@qq.com")
    store.set_checkpoint("INBOX", 17, account="first@qq.com")
    store.set_checkpoint("INBOX", 3, account="second@qq.com")

    assert created_first is True and created_second is True
    assert first.id != second.id
    assert store.count_items(account="first@qq.com") == 1
    assert store.count_items(account="second@qq.com") == 1
    assert store.checkpoint("INBOX", account="first@qq.com") == 17
    assert store.checkpoint("INBOX", account="second@qq.com") == 3


def test_uidvalidity_change_resets_checkpoint_without_overwriting_old_mail(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection(
        host="imap.qq.com", port=993, username="owner@qq.com", verified=True,
        auto_draft_ready=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))
    first = SimpleNamespace(
        uid="1", uid_validity="100", folder="INBOX", message_id="<old-uid@example.com>",
        thread_id="old-thread", sender="sender@example.com", reply_to="sender@example.com",
        recipients="owner@qq.com", subject="旧邮件", received_at="2026-10-10T00:00:00Z", body_text="旧内容",
        in_reply_to="", references="",
    )
    second = SimpleNamespace(
        uid="1", uid_validity="200", folder="INBOX", message_id="<new-uid@example.com>",
        thread_id="new-thread", sender="sender@example.com", reply_to="sender@example.com",
        recipients="owner@qq.com", subject="新邮件", received_at="2026-10-11T00:00:00Z", body_text="新内容",
        in_reply_to="", references="",
    )
    old, _ = store.upsert_item(first, account="owner@qq.com")
    store.prepare_sync_state("INBOX", account="owner@qq.com", uid_validity="100")
    store.set_checkpoint("INBOX", 9, account="owner@qq.com", uid_validity="100")

    changed = store.prepare_sync_state("INBOX", account="owner@qq.com", uid_validity="200")
    new, created = store.upsert_item(second, account="owner@qq.com")

    assert changed["changed"] is True
    assert changed["lastUid"] == 0
    assert changed["previousUidValidity"] == "100"
    assert created is True
    assert new.id != old.id
    assert store.checkpoint("INBOX", account="owner@qq.com") == 0
    assert store.sync_state("INBOX", account="owner@qq.com")["uidValidity"] == "200"
    assert store.count_items(account="owner@qq.com") == 2


def test_uidvalidity_change_does_not_merge_same_message_id_into_old_namespace(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection(
        host="imap.qq.com", port=993, username="owner@qq.com", verified=True,
        auto_draft_ready=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))
    first = SimpleNamespace(
        uid="1", uid_validity="100", folder="INBOX", message_id="<reused@example.com>",
        thread_id="old-thread", sender="sender@example.com", reply_to="sender@example.com",
        recipients="owner@qq.com", subject="旧命名空间", received_at="2026-10-10T00:00:00Z", body_text="旧正文",
        in_reply_to="", references="",
    )
    second = SimpleNamespace(
        uid="1", uid_validity="200", folder="INBOX", message_id="<reused@example.com>",
        thread_id="new-thread", sender="sender@example.com", reply_to="sender@example.com",
        recipients="owner@qq.com", subject="新命名空间", received_at="2026-10-11T00:00:00Z", body_text="新正文",
        in_reply_to="", references="",
    )

    old, _ = store.upsert_item(first, account="owner@qq.com")
    store.prepare_sync_state("INBOX", account="owner@qq.com", uid_validity="100")
    store.prepare_sync_state("INBOX", account="owner@qq.com", uid_validity="200")
    new, created = store.upsert_item(second, account="owner@qq.com")

    assert created is True
    assert new.id != old.id
    assert new.subject == "新命名空间"
    assert store.get_item(old.id, account="owner@qq.com").subject == "旧命名空间"
    assert store.count_items(account="owner@qq.com") == 2


def test_first_uidvalidity_observation_resets_an_existing_uid_only_checkpoint(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection(
        host="imap.qq.com", port=993, username="owner@qq.com", verified=True,
        auto_draft_ready=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))
    old = SimpleNamespace(
        uid="9", uid_validity="", folder="INBOX", message_id="<late-uidvalidity@example.com>",
        thread_id="old-thread", sender="sender@example.com", reply_to="sender@example.com",
        recipients="owner@qq.com", subject="旧 checkpoint", received_at="2026-10-10T00:00:00Z", body_text="旧正文",
        in_reply_to="", references="",
    )

    old_item, _ = store.upsert_item(old, account="owner@qq.com")
    store.prepare_sync_state("INBOX", account="owner@qq.com")
    store.set_checkpoint("INBOX", 9, account="owner@qq.com")

    changed = store.prepare_sync_state("INBOX", account="owner@qq.com", uid_validity="300")

    assert changed["changed"] is True
    assert changed["lastUid"] == 0
    assert changed["previousUidValidity"] == ""
    assert store.get_item(old_item.id, account="owner@qq.com").subject == "旧 checkpoint"


def test_qq_adapter_reads_uidvalidity_after_readonly_select():
    class Imap:
        def select(self, _folder, readonly=True):
            assert readonly
            return "OK", [b"0"]

        def response(self, name):
            assert name == "UIDVALIDITY"
            return "UIDVALIDITY", [b"98765"]

    adapter = QQMailAdapter(MailConnection("imap.qq.com", 993, "owner@qq.com"), "secret")
    adapter._imap = Imap()

    assert adapter.folder_uid_validity("INBOX") == "98765"


def test_reply_without_references_joins_the_known_parent_thread(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    parent, _ = store.upsert_item(SimpleNamespace(
        uid="1", folder="INBOX", message_id="<thread-root@example.com>", thread_id="remote-root", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="根邮件", received_at="2026-10-13T00:00:00Z", body_text="root", in_reply_to="", references="",
    ), account="owner@qq.com")
    reply, _ = store.upsert_item(SimpleNamespace(
        uid="2", folder="INBOX", message_id="<thread-reply@example.com>", thread_id="remote-reply", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="回复", received_at="2026-10-14T00:00:00Z", body_text="reply", in_reply_to="<thread-root@example.com>", references="",
    ), account="owner@qq.com")

    assert reply.thread_id == parent.thread_id
    assert [item.id for item in store.thread_items(parent.thread_id, account="owner@qq.com")] == [parent.id, reply.id]


def test_account_switch_hides_other_mail_threads_and_jobs(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    remote = SimpleNamespace(
        uid="1", folder="INBOX", message_id="<same@example.com>", thread_id="same-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="same", received_at="2026-10-14T00:00:00Z", body_text="body", in_reply_to="", references="",
    )
    old_item, _ = store.upsert_item(remote, account="old@qq.com")
    active_item, _ = store.upsert_item(remote, account="owner@qq.com")
    old_thread = store.thread_items(old_item.thread_id, account="old@qq.com")
    active_thread = store.thread_items(active_item.thread_id, account="owner@qq.com")
    job = service.enqueue_job("process", target_id=active_item.id, request_id="scoped-request")

    assert [item.id for item in old_thread] == [old_item.id]
    assert [item.id for item in active_thread] == [active_item.id]
    assert [item["id"] for item in service.list_items_public()] == [active_item.id]
    assert service.job_status(job["jobId"]) is not None

    store.update_connection(username="old@qq.com")

    assert service.job_status(job["jobId"]) is None
    assert [item["id"] for item in service.list_items_public()] == [old_item.id]
    assert store.thread_items(active_item.thread_id, account="old@qq.com") == []


def test_job_claim_and_retirement_are_scoped_to_account_epoch(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    stale = store.enqueue_job(
        job_id="stale-job", request_key="stale-request", kind="process",
        target_id="stale-mail", account="old@qq.com", account_epoch=1,
    )
    active = service.enqueue_job("process", target_id="active-mail", request_id="active-request")

    assert store.retire_stale_jobs(account="owner@qq.com", account_epoch=store.connection().account_epoch) == 1
    claimed = store.claim_next_job(account="owner@qq.com", account_epoch=store.connection().account_epoch)

    assert stale["status"] == "queued"
    assert claimed is not None and claimed["jobId"] == active["jobId"]
    assert store.get_job("stale-job", account="old@qq.com")["status"] == "failed"


def test_job_status_retires_old_same_account_generation_before_worker_runs(tmp_path):
    service, store, _adapter = make_service(tmp_path)
    job = service.enqueue_job("process", target_id="active-mail", request_id="generation-read")
    store.save_connection(MailConnection(
        "imap.qq.com", 993, "other@qq.com", verified=True, auto_draft_ready=True,
        enabled=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))
    store.save_connection(MailConnection(
        "imap.qq.com", 993, "owner@qq.com", verified=True, auto_draft_ready=True,
        enabled=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))

    observed = service.job_status(job["jobId"])

    assert observed is not None
    assert observed["status"] == "failed"
    assert observed["error"] == "邮件账号已切换，任务未执行"


def test_stale_draft_marking_is_scoped_to_the_active_account(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    thread_id = "a" * 32
    remote = SimpleNamespace(
        uid="1", folder="INBOX", message_id="<same-thread@example.com>", thread_id=thread_id, sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="same", received_at="2026-10-14T00:00:00Z", body_text="body", in_reply_to="", references="",
    )
    old_item, _ = store.upsert_item(remote, account="old@qq.com")
    active_item, _ = store.upsert_item(remote, account="owner@qq.com")
    for item, task_key in ((old_item, "old-draft"), (active_item, "active-draft")):
        store.save_draft(item.id, task_key=task_key, remote_uid="1", folder="Drafts", body="reply", subject="Re: same",
                         recipients="sender@example.com", message_id=f"<{task_key}@example.com>", generated_hash="g", remote_hash="r")

    assert store.mark_thread_drafts_stale(thread_id, account="owner@qq.com") == 1
    assert store.list_drafts_for_mail(old_item.id)[0].stale is False
    assert store.list_drafts_for_mail(active_item.id)[0].stale is True


def test_mail_list_can_filter_by_sent_association_state(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection(
        host="imap.qq.com", port=993, username="owner@qq.com", verified=True,
        auto_draft_ready=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))
    first, _ = add_owner_item(store, SimpleNamespace(
        uid="401", folder="INBOX", message_id="<association-pending@example.com>", thread_id="association-pending",
        sender="sender@example.com", reply_to="sender@example.com", recipients="owner@qq.com", subject="待确认",
        received_at="2026-10-20T09:00:00Z", body_text="请确认", in_reply_to="", references="",
    ))
    second, _ = add_owner_item(store, SimpleNamespace(
        uid="402", folder="INBOX", message_id="<association-ambiguous@example.com>", thread_id="association-ambiguous",
        sender="sender@example.com", reply_to="sender@example.com", recipients="owner@qq.com", subject="待核实",
        received_at="2026-10-20T10:00:00Z", body_text="请核实", in_reply_to="", references="",
    ))
    first_draft = store.save_draft(
        first.id, task_key="association-pending", remote_uid="1", folder="Drafts", body="回复",
        subject="Re: 待确认", recipients="sender@example.com", message_id="<draft-pending@example.com>",
        generated_hash="g1", remote_hash="r1",
    )
    second_draft = store.save_draft(
        second.id, task_key="association-ambiguous", remote_uid="2", folder="Drafts", body="回复",
        subject="Re: 待核实", recipients="sender@example.com", message_id="<draft-ambiguous@example.com>",
        generated_hash="g2", remote_hash="r2",
    )
    store.update_draft_send(first_draft.id, account="owner@qq.com", state="pending")
    store.update_draft_send(second_draft.id, account="owner@qq.com", state="ambiguous")

    assert [item.id for item in store.list_items(status="sent_pending", account="owner@qq.com")] == [first.id]
    assert store.count_items(status="sent_pending", account="owner@qq.com") == 1
    assert [item.id for item in store.list_items(status="sent_ambiguous", account="owner@qq.com")] == [second.id]


def test_mail_list_search_matches_subject_sender_body_and_preserves_account_scope(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection(
        host="imap.qq.com", port=993, username="owner@qq.com", verified=True,
        auto_draft_ready=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))
    subject_item, _ = add_owner_item(store, SimpleNamespace(
        uid="501", folder="INBOX", message_id="<search-subject@example.com>", thread_id="search-subject",
        sender="sender@example.com", reply_to="sender@example.com", recipients="owner@qq.com", subject="项目排期",
        received_at="2026-10-20T09:00:00Z", body_text="请确认下周安排", in_reply_to="", references="",
    ))
    body_item, _ = add_owner_item(store, SimpleNamespace(
        uid="502", folder="INBOX", message_id="<search-body@example.com>", thread_id="search-body",
        sender="teacher@example.com", reply_to="teacher@example.com", recipients="owner@qq.com", subject="会议",
        received_at="2026-10-20T10:00:00Z", body_text="周五下午三点线上沟通", in_reply_to="", references="",
    ))
    other_account, _ = store.upsert_item(SimpleNamespace(
        uid="501", folder="INBOX", message_id="<search-other@example.com>", thread_id="search-other",
        sender="sender@example.com", reply_to="sender@example.com", recipients="owner@qq.com", subject="项目排期",
        received_at="2026-10-20T11:00:00Z", body_text="另一账号数据", in_reply_to="", references="",
    ), account="other@qq.com")

    assert [item.id for item in store.list_items(query="排期", account="owner@qq.com")] == [subject_item.id]
    assert [item.id for item in store.list_items(query="三点", account="owner@qq.com")] == [body_item.id]
    assert store.count_items(query="项目", account="owner@qq.com") == 1
    assert other_account.id not in [item.id for item in store.list_items(query="排期", account="owner@qq.com")]


def test_uncertain_append_reconcile_backoff_survives_restart(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com", verified=True, auto_draft_ready=True))
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="31", folder="INBOX", message_id="<backoff@example.com>", thread_id="backoff-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="待核实", received_at="2026-10-14T00:00:00Z", body_text="请回复", in_reply_to="", references="",
    ))
    task_key = f"auto:{item.id}:v1"
    epoch = store.connection().account_epoch
    assert store.task_claim(task_key, item.id, account="owner@qq.com", account_epoch=epoch)
    assert store.task_begin_write(task_key, account="owner@qq.com", account_epoch=epoch)
    store.task_finish(task_key, "uncertain", "QQ 返回不确定", account="owner@qq.com")

    first = store.unconfirmed_append_task(item.id, account="owner@qq.com")
    assert first is not None and first["due"] is True
    store.note_task_reconcile(task_key, account="owner@qq.com", success=False)

    migrated = MailStore(tmp_path / "mail.db")
    second = migrated.unconfirmed_append_task(item.id, account="owner@qq.com")
    assert second is not None
    assert second["reconcileAttempts"] == 1
    assert second["due"] is False
    assert second["nextCheckAt"]

    migrated.note_task_reconcile(task_key, account="owner@qq.com", success=True)
    third = migrated.unconfirmed_append_task(item.id, account="owner@qq.com")
    assert third is not None and third["due"] is True and third["nextCheckAt"] is None


def test_legacy_mail_and_checkpoint_data_migrate_to_saved_account_scope(tmp_path):
    path = tmp_path / "mail.db"
    store = MailStore(path)
    item, _ = store.upsert_item(SimpleNamespace(
        uid="9", folder="INBOX", message_id="<legacy@example.com>", thread_id="legacy-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="legacy", received_at="2026-10-13T00:00:00Z", body_text="body", in_reply_to="", references="",
    ))
    legacy_thread_job = store.enqueue_job(job_id="legacy-thread-job", request_key="legacy-thread-request", kind="process_thread", target_id=item.thread_id)
    store.set_checkpoint("INBOX", 9)
    store.save_connection(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"))

    migrated = MailStore(path)

    current = migrated.get_item(item.id, account="owner@qq.com")
    assert current is not None
    assert current.source_uid.startswith("acct-")
    assert migrated.checkpoint("INBOX", account="owner@qq.com") == 9
    assert migrated.get_job(legacy_thread_job["jobId"], account="owner@qq.com")["targetId"] == current.thread_id
    follow_up, _ = migrated.upsert_item(SimpleNamespace(
        uid="10", folder="INBOX", message_id="<legacy-reply@example.com>", thread_id=current.thread_id, sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="Re: legacy", received_at="2026-10-14T00:00:00Z", body_text="reply", in_reply_to="<legacy@example.com>", references="<legacy@example.com>",
    ), account="owner@qq.com")
    assert follow_up.thread_id == current.thread_id


def test_legacy_draft_versions_receive_stable_revision_numbers(tmp_path):
    path = tmp_path / "legacy-drafts.db"
    store = MailStore(path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="17", folder="INBOX", message_id="<legacy-drafts@example.com>", thread_id="legacy-drafts",
        sender="sender@example.com", reply_to="sender@example.com", recipients="owner@qq.com",
        subject="versions", received_at="2026-10-13T00:00:00Z", body_text="body", in_reply_to="", references="",
    ))
    store.save_draft(item.id, task_key="legacy-version-1", remote_uid="1", folder="Drafts", body="one",
        subject="Re: versions", recipients="sender@example.com", message_id="<draft-one>", generated_hash="g1", remote_hash="r1")
    store.save_draft(item.id, task_key="legacy-version-2", remote_uid="2", folder="Drafts", body="two",
        subject="Re: versions", recipients="sender@example.com", message_id="<draft-two>", generated_hash="g2", remote_hash="r2")
    with store._connect() as db:
        db.execute("DROP INDEX mail_drafts_mail_revision")
        db.execute("ALTER TABLE mail_drafts DROP COLUMN revision")

    migrated = MailStore(path)

    assert sorted(draft.revision for draft in migrated.list_drafts_for_mail(item.id)) == [1, 2]


def test_draft_send_association_migrates_and_starts_pending(tmp_path):
    path = tmp_path / "draft-send.db"
    store = MailStore(path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="18", folder="INBOX", message_id="<draft-send@example.com>", thread_id="draft-send",
        sender="sender@example.com", reply_to="sender@example.com", recipients="owner@qq.com",
        subject="send association", received_at="2026-10-13T00:00:00Z", body_text="body", in_reply_to="", references="",
    ))
    draft = store.save_draft(item.id, task_key="draft-send-task", remote_uid="1", folder="Drafts",
        body="reply", subject="Re: send association", recipients="sender@example.com",
        message_id="<draft-send-copy>", generated_hash="g", remote_hash="r")
    with store._connect() as db:
        db.execute("DROP TABLE mail_draft_sends")

    migrated = MailStore(path)
    association = migrated.draft_send(draft.id, account="owner@qq.com")

    assert association is not None
    assert association.state == "pending"
    assert association.sent_uid is None
    assert migrated.get_draft(draft.id, account="owner@qq.com") is not None


def test_mail_api_rejects_item_and_thread_ids_from_another_account(tmp_path, monkeypatch):
    service, store, _adapter = make_service(tmp_path)
    remote = SimpleNamespace(
        uid="1", folder="INBOX", message_id="<same@example.com>", thread_id="same-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="same", received_at="2026-10-14T00:00:00Z", body_text="body", in_reply_to="", references="",
    )
    other_item, _ = store.upsert_item(remote, account="other@qq.com")
    monkeypatch.setattr(mail_api, "mail_service", service)

    @asynccontextmanager
    async def lifespan(_app):
        service.start()
        yield
        await service.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(mail_api.router)
    with TestClient(app) as client:
        assert client.get(f"/api/mail/items/{other_item.id}").status_code == 404
        assert client.post(f"/api/mail/threads/{other_item.thread_id}/process", json={"requestId": "cross-account"}).status_code == 404


def test_folder_discovery_uses_special_use_flags_and_accepts_empty_folders():
    class Client:
        def __init__(self):
            self.selected = []

        def list(self):
            return "OK", [
                b'(\\HasNoChildren \\Inbox) "/" "INBOX"',
                b'(\\HasNoChildren \\Sent) "/" "Owner Archive"',
                b'(\\HasNoChildren \\Drafts) "/" "Working Copy"',
            ]

        def select(self, folder, readonly=True):
            self.selected.append((folder, readonly))
            return "OK", [b"0"]

    client = Client()
    adapter = QQMailAdapter(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"), "secret")
    adapter._imap = client

    result = adapter.verify()

    assert result["folders"] == {"inbox": "INBOX", "sent": "Owner Archive", "drafts": "Working Copy"}
    assert result["counts"] == {"inbox": 0, "sent": 0, "drafts": 0}
    assert len(client.selected) == 3
    assert all(readonly for _folder, readonly in client.selected)


@pytest.mark.parametrize("identity", ["custom-header", "message-id", "subject"])
def test_test_draft_verification_survives_server_rewriting_identity_headers(identity):
    marker = "meido-test-unique-marker"
    message = EmailMessage()
    message["From"] = "owner@qq.com"
    message["To"] = "owner@qq.com"
    message["Subject"] = f"QQ 邮箱测试 {marker}" if identity == "subject" else "QQ 邮箱测试"
    if identity == "custom-header":
        message["X-Meido-Test-ID"] = marker
    elif identity == "message-id":
        message["Message-ID"] = f"<{marker}@local.meido>"
    message.set_content("test")
    raw = message.as_bytes()
    adapter = QQMailAdapter(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"), "secret")
    adapter._fetch = lambda _folder, _query="ALL", _limit=None: [("1", raw)]

    assert adapter.verify_test_draft_present(marker, "Drafts") is True
    assert adapter.verify_test_sent(marker, {"sent": "Sent"}) is True


def test_failed_uid_fetch_aborts_instead_of_skipping_checkpoint_data():
    class Client:
        def select(self, _folder, readonly=True):
            assert readonly is True
            return "OK", [b"2"]

        def uid(self, command, uid, _query):
            if command == "SEARCH":
                return "OK", [b"1 2"]
            if uid == b"1":
                return "OK", [(b"1 BODY[]", b"first")]
            return "NO", []

    adapter = QQMailAdapter(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"), "secret")
    adapter._imap = Client()

    with pytest.raises(MailAdapterError, match="UID 2"):
        adapter.fetch_inbox("INBOX", 0)
