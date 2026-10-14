import asyncio
import json
import time
import pytest
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.mail.adapter import MailAdapterError, QQMailAdapter, RemoteDraft, RemoteMail
from backend.app.mail.agent import MailManageTool
from backend.app.mail import api as mail_api
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

    def close(self):
        pass

    def drafts(self, folder):
        return list(self.draft_rows)

    def sent_samples(self, folder, limit=8):
        return []

    def fetch_inbox(self, folder, after_uid=0):
        return [item for item in self.inbox_items if item.folder == folder and int(item.uid) > after_uid]

    def append_draft(self, folder, *, marker, recipients, subject, body, in_reply_to="", references=""):
        self.counter += 1
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
    service._adapter = lambda connection=None: adapter
    return service, store, adapter


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


def test_role_created_draft_is_idempotent_and_read_operations_are_audited(tmp_path):
    service, store, adapter = make_service(tmp_path)
    item, _ = add_owner_item(store, SimpleNamespace(
        uid="3", folder="INBOX", message_id="<source-3@example.com>", thread_id="thread-3", sender="Carol <carol@example.com>",
        reply_to="carol@example.com", recipients="owner@qq.com", subject="进度", received_at="2026-10-14T00:00:00Z", body_text="有进展吗？", in_reply_to="", references="",
    ))

    first = asyncio.run(service.role_action("draft", mail_id=item.id, query="正在处理中。", request_id="draft-request", role_id="role-1", run_id="run-1"))
    second = asyncio.run(service.role_action("draft", mail_id=item.id, query="正在处理中。", request_id="draft-request", role_id="role-1", run_id="run-1"))
    asyncio.run(service.role_action("read", mail_id=item.id, role_id="role-1", run_id="run-2"))

    assert first["draftId"] == second["draftId"]
    assert len(adapter.draft_rows) == 1
    operations = [event["operation"] for event in store.list_audits()]
    assert operations.count("read") == 1
    assert operations.count("draft") == 2


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


def test_legacy_mail_and_checkpoint_data_migrate_to_saved_account_scope(tmp_path):
    path = tmp_path / "mail.db"
    store = MailStore(path)
    item, _ = store.upsert_item(SimpleNamespace(
        uid="9", folder="INBOX", message_id="<legacy@example.com>", thread_id="legacy-thread", sender="sender@example.com",
        reply_to="sender@example.com", recipients="owner@qq.com", subject="legacy", received_at="2026-10-13T00:00:00Z", body_text="body", in_reply_to="", references="",
    ))
    store.set_checkpoint("INBOX", 9)
    store.save_connection(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"))

    migrated = MailStore(path)

    current = migrated.get_item(item.id, account="owner@qq.com")
    assert current is not None
    assert current.source_uid.startswith("acct-")
    assert migrated.checkpoint("INBOX", account="owner@qq.com") == 9


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
