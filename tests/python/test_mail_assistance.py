"""Mail assistance behavior through the HTTP and mailbox interfaces."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from email.message import EmailMessage

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.mail import api as mail_api
from backend.app.mail import service as service_module
from backend.app.mail.adapter import QQMailAdapter, RemoteDraft, RemoteMail, MailAdapterError
from backend.app.mail.models import MailConnection
from backend.app.mail.service import MailService
from backend.app.mail.store import MailStore


class MemoryCredentials:
    def get(self, key):
        return "synthetic-secret"

    def set(self, key, value):
        pass


class Mailbox:
    def __init__(self):
        self.incoming = []
        self.sent = []
        self.draft_rows = []

    def close(self):
        pass

    def fetch_inbox(self, folder, after_uid=0):
        return [row for row in self.incoming if int(row.uid) > after_uid]

    def sent_samples(self, folder, limit=8):
        return self.sent[-limit:]

    def related_sent(self, folder, participant, limit=24):
        return [row for row in self.sent if participant in row.recipients][-limit:]

    def drafts(self, folder):
        return list(self.draft_rows)

    def append_draft(self, folder, *, marker, recipients, subject, body, in_reply_to="", references=""):
        draft = RemoteDraft(marker, str(len(self.draft_rows) + 1), folder, f"<{marker}@meido>", subject, recipients, body, body)
        self.draft_rows.append(draft)
        return draft


def incoming(uid="1", *, body="可以参加周五面试吗？", message_id="<invite@example.com>", references=""):
    return RemoteMail(uid, "INBOX", message_id, "invite-thread", "teacher@example.com", "teacher@example.com",
                      "owner@qq.com", "面试安排", f"2026-10-{int(uid) + 10:02d}T09:00:00Z", body,
                      references.split()[-1] if references else "", references)


@pytest.fixture
def mail_app(tmp_path, monkeypatch):
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection("imap.qq.com", 993, "owner@qq.com", verified=True,
        auto_draft_ready=True, enabled=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts"))
    mailbox = Mailbox()
    prompts = []
    responses = [{"decision": "draft", "reason": "已具备回复信息", "body": "您好，周五可以。", "questions": []}]

    async def complete(messages, configuration):
        prompts.append(json.loads(messages[1]["content"]))
        return json.dumps(responses.pop(0), ensure_ascii=False)

    service = MailService(store, credential_store=MemoryCredentials(), complete_model=complete,
        configuration_getter=lambda: SimpleNamespace(model="synthetic-model", provider="test"))
    monkeypatch.setattr(service_module, "QQMailAdapter", lambda *args, **kwargs: mailbox)
    monkeypatch.setattr(mail_api, "mail_service", service)

    @asynccontextmanager
    async def lifespan(app):
        service.start()
        yield
        await service.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(mail_api.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, service=service, store=store, mailbox=mailbox,
                              prompts=prompts, responses=responses, root=tmp_path)


def test_owner_can_save_mail_style_and_role_permission_across_restart(mail_app):
    client = mail_app.client
    initial = client.get("/api/mail/preferences")
    assert initial.status_code == 200
    assert initial.json() == {"writingStyle": "", "roleAccess": False}
    saved = client.put("/api/mail/preferences", json={"writingStyle": "简短，不做额外承诺", "roleAccess": True})
    assert saved.status_code == 200
    restarted = MailService(MailStore(mail_app.root / "mail.db"), credential_store=MemoryCredentials())
    assert restarted.preferences() == {"writingStyle": "简短，不做额外承诺", "roleAccess": True}
    assert client.put("/api/mail/preferences", json={"writingStyle": "x" * 2001, "roleAccess": True}).status_code == 422
    assert client.get("/api/mail/preferences").json()["writingStyle"] == "简短，不做额外承诺"


def finish_job(client, response):
    assert response.status_code == 200, response.text
    job_id = response.json()["job"]["jobId"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = client.get(f"/api/mail/jobs/{job_id}").json()["job"]
        if job["status"] in {"completed", "failed"}:
            assert job["status"] == "completed", job
            return job["result"]
        time.sleep(0.02)
    pytest.fail("Mail job did not finish")


def test_reply_uses_older_same_recipient_history_and_saved_style_without_replying_to_sent_mail(mail_app):
    state = mail_app
    state.client.put("/api/mail/preferences", json={"writingStyle": "简短，不做额外承诺", "roleAccess": False})
    state.responses[:] = [{"needsReply": True, "body": "我会按约定提交。"}]
    state.mailbox.incoming = [incoming()]
    old_reply = RemoteMail("1", "Sent", "<old@example.com>", "old-thread", "owner@qq.com", "owner@qq.com",
                          "teacher@example.com", "往日称呼", "2026-09-01T09:00:00Z", "老师您好，我已收到。")
    state.mailbox.sent = [old_reply] + [RemoteMail(str(i), "Sent", f"<shop-{i}@example.com>", f"shop-{i}",
        "owner@qq.com", "owner@qq.com", f"shop{i}@example.com", "无关购物", "2026-10-10T10:00:00Z", "请开发票。") for i in range(2, 22)]
    state.mailbox.sent.append(RemoteMail("22", "Sent", "<own-thread@example.com>", "invite-thread", "owner@qq.com",
        "owner@qq.com", "teacher@example.com", "Re: 面试安排", "2026-10-10T11:00:00Z", "已说明我的研究方向。",
        "<invite@example.com>", "<invite@example.com>"))
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "history", "process": True}))
    listed = state.client.get("/api/mail/items").json()["items"]
    assert len(listed) == 1
    detail = state.client.get(f"/api/mail/items/{listed[0]['id']}").json()
    assert any(row["sender"] == "owner@qq.com" for row in detail["thread"])
    prompt = state.prompts[0]
    assert prompt["writingStyle"] == "简短，不做额外承诺"
    assert any("老师您好" in row["body"] for row in prompt["sentSamples"])
    assert any(row["sender"] == "owner@qq.com" for row in prompt["thread"])
    assert len(state.mailbox.draft_rows) == 1
    finish_job(state.client, state.client.post("/api/mail/process-pending", json={"requestId": "again"}))
    assert len(state.mailbox.draft_rows) == 1


def test_qq_related_sent_search_keeps_reply_headers_and_checks_exact_recipient():
    messages = {}
    for uid, recipient in ((b"1", "teacher@example.com"), (b"2", "teacher@example.com.attacker")):
        message = EmailMessage()
        message["From"] = "owner@qq.com"
        message["To"] = recipient
        message["Subject"] = "Old reply"
        message["Message-ID"] = f"<sent-{uid.decode()}@example.com>"
        message["In-Reply-To"] = "<root@example.com>"
        message["References"] = "<root@example.com>"
        message.set_content("Previously agreed.")
        messages[uid] = message.as_bytes()

    class Imap:
        query = ""

        def select(self, folder, readonly=True):
            assert readonly
            return "OK", [b"2"]

        def uid(self, command, *args):
            if command == "SEARCH":
                self.query = args[-1]
                return "OK", [b"1 2"]
            assert args[-1] == "(BODY.PEEK[])"
            return "OK", [(b"BODY", messages[args[0]])]

    adapter = QQMailAdapter(MailConnection("imap.qq.com", 993, "owner@qq.com"), "synthetic")
    imap = Imap()
    adapter._imap = imap
    rows = adapter.related_sent("Sent", "teacher@example.com", 24)
    assert imap.query == 'TO "teacher@example.com"'
    assert [row.message_id for row in rows] == ["<sent-1@example.com>"]
    assert rows[0].in_reply_to == "<root@example.com>"
    assert rows[0].references == "<root@example.com>"
    with pytest.raises(MailAdapterError):
        adapter.related_sent("Sent", 'teacher@example.com" OR ALL', 24)


def test_owner_can_answer_a_persisted_question_and_replay_without_duplicate_drafts(mail_app, monkeypatch):
    state = mail_app
    state.responses[:] = [
        {"decision": "needs_input", "reason": "尚未决定是否接受", "questions": ["是否接受周五面试？"], "body": ""},
        {"decision": "draft", "reason": "主人已接受", "questions": [], "body": "我可以参加周五面试。"},
    ]
    state.mailbox.incoming = [incoming()]
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "question", "process": True}))
    item = state.client.get("/api/mail/items").json()["items"][0]
    assert item["status"] == "needs_input"
    detail = state.client.get(f"/api/mail/items/{item['id']}").json()
    preparation = detail["preparation"]
    assert preparation["questions"] == ["是否接受周五面试？"]
    assert preparation["reason"] == "尚未决定是否接受"
    assert state.mailbox.draft_rows == []
    state.client.portal.call(state.service.close)

    restarted = MailService(MailStore(state.root / "mail.db"), credential_store=MemoryCredentials(),
        complete_model=state.service.complete_model, configuration_getter=state.service.configuration_getter)
    monkeypatch.setattr(mail_api, "mail_service", restarted)

    @asynccontextmanager
    async def lifespan(app):
        restarted.start()
        yield
        await restarted.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(mail_api.router)
    with TestClient(app) as client:
        recovered = client.get(f"/api/mail/items/{item['id']}").json()["preparation"]
        assert recovered["id"] == preparation["id"]
        answer = {"questionId": preparation["id"], "requestId": "my-decision", "answer": "可以参加"}
        result = finish_job(client, client.post(f"/api/mail/items/{item['id']}/answer", json=answer))
        assert result["status"] == "drafted"
        finish_job(client, client.post(f"/api/mail/items/{item['id']}/answer", json=answer))
        assert len(state.mailbox.draft_rows) == 1
        assert state.prompts[-1]["ownerAnswer"] == "可以参加"
        assert client.post(f"/api/mail/items/{item['id']}/answer", json={**answer, "answer": "不能参加"}).status_code == 409
        saved = client.get(f"/api/mail/items/{item['id']}").json()
        assert saved["drafts"][0]["preparation"]["reason"] == "主人已接受"


def test_queued_answer_is_rejected_after_a_new_message_and_old_question_is_marked_stale(mail_app):
    state = mail_app
    state.responses[:] = [{"decision": "needs_input", "reason": "等待主人选择", "questions": ["周五能参加吗？"], "body": ""}]
    state.mailbox.incoming = [incoming()]
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "initial", "process": True}))
    item = state.client.get("/api/mail/items").json()["items"][0]
    question = state.client.get(f"/api/mail/items/{item['id']}").json()["preparation"]
    state.client.portal.call(state.service.close)
    accepted = state.client.post(f"/api/mail/items/{item['id']}/answer", json={"questionId": question["id"], "requestId": "late-answer", "answer": "可以"})
    state.mailbox.incoming.append(incoming("2", body="改为周六，请重新确认。", message_id="<changed@example.com>", references="<invite@example.com>"))
    state.client.portal.call(state.service.start)
    job_id = accepted.json()["job"]["jobId"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = state.client.get(f"/api/mail/jobs/{job_id}").json()["job"]
        if job["status"] == "failed":
            break
        time.sleep(0.02)
    assert job["status"] == "failed"
    assert "已更新" in job["error"]
    assert state.client.get(f"/api/mail/items/{item['id']}").json()["preparation"]["stale"] is True
    assert state.mailbox.draft_rows == []


def test_draft_records_real_input_sources_and_reports_unavailable_context(mail_app):
    state = mail_app
    state.mailbox.incoming = [incoming()]

    async def knowledge(query):
        return [SimpleNamespace(text="主人已同意参加。", document_id="decision-note", title="面试计划", location="第二段", url="javascript:alert(1)")]

    state.service.owner_search = knowledge
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "sources", "process": True}))
    item = state.client.get("/api/mail/items").json()["items"][0]
    detail = state.client.get(f"/api/mail/items/{item['id']}").json()
    preparation = detail["drafts"][0]["preparation"]
    source = next(row for row in preparation["sources"] if row["id"] == "decision-note")
    assert source["title"] == "面试计划"
    assert source["location"] == "第二段"
    assert source["url"] == ""
    assert preparation["model"] == "synthetic-model"
    assert "synthetic-secret" not in json.dumps(detail)

    async def unavailable(query):
        raise RuntimeError("synthetic-secret should never be shown")

    state.service.owner_search = unavailable
    state.responses[:] = [{"decision": "draft", "body": "收到。", "reason": "仅参考来信", "questions": []}]
    finish_job(state.client, state.client.post(f"/api/mail/items/{item['id']}/regenerate", json={"requestId": "degraded"}))
    details = state.client.get(f"/api/mail/items/{item['id']}").json()
    assert "主人资料读取失败" in details["preparation"]["warnings"][0]
    assert "synthetic-secret" not in json.dumps(details)
    assert next(row for row in details["drafts"] if row["preparation"]["id"] == preparation["id"])["preparation"]["sources"] == preparation["sources"]


def test_later_queued_answer_cannot_replace_a_completed_draft_status(mail_app):
    state = mail_app
    state.responses[:] = [
        {"decision": "needs_input", "reason": "等待选择", "questions": ["是否参加？"], "body": ""},
        {"decision": "draft", "reason": "已确认", "questions": [], "body": "可以参加。"},
    ]
    state.mailbox.incoming = [incoming()]
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "start", "process": True}))
    item = state.client.get("/api/mail/items").json()["items"][0]
    question = state.client.get(f"/api/mail/items/{item['id']}").json()["preparation"]
    state.client.portal.call(state.service.close)
    data = {"questionId": question["id"], "answer": "可以", "requestId": "first"}
    first = state.client.post(f"/api/mail/items/{item['id']}/answer", json=data)
    second = state.client.post(f"/api/mail/items/{item['id']}/answer", json={**data, "requestId": "second"})
    state.client.portal.call(state.service.start)
    finish_job(state.client, first)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = state.client.get(f"/api/mail/jobs/{second.json()['job']['jobId']}").json()["job"]
        if job["status"] == "failed":
            break
        time.sleep(0.02)
    assert job["status"] == "failed"
    assert state.client.get(f"/api/mail/items/{item['id']}").json()["item"]["status"] == "drafted"
    assert len(state.mailbox.draft_rows) == 1
