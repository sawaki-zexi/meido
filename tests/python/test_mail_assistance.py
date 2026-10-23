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

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.mail import api as mail_api
from backend.app.mail import service as service_module
from backend.app.mail.adapter import QQMailAdapter, RemoteDraft, RemoteMail, MailAdapterError
from backend.app.mail.agent import MailManageTool
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
        self.fail_append = False

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
        if self.fail_append:
            raise RuntimeError("synthetic interrupted draft write")
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
    middle_reply = RemoteMail("2", "Sent", "<middle@example.com>", "middle-thread", "owner@qq.com", "owner@qq.com",
                              "teacher@example.com", "较近称呼", "2026-10-01T09:00:00Z", "老师您好，进展如下。")
    state.mailbox.sent = [middle_reply, old_reply] + [RemoteMail(str(i), "Sent", f"<shop-{i}@example.com>", f"shop-{i}",
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
    assert [row["subject"] for row in prompt["sentSamples"]] == ["较近称呼", "往日称呼"]
    assert any(row["sender"] == "owner@qq.com" for row in prompt["thread"])
    assert len(state.mailbox.draft_rows) == 1
    finish_job(state.client, state.client.post("/api/mail/process-pending", json={"requestId": "again"}))
    assert len(state.mailbox.draft_rows) == 1


def test_thread_detail_orders_rfc_and_iso_dates_chronologically(mail_app):
    state = mail_app
    root = RemoteMail("1", "INBOX", "<root-order@example.com>", "order-thread", "teacher@example.com", "teacher@example.com",
        "owner@qq.com", "安排", "Fri, 21 Jun 2024 10:00:00 +0000", "第一封")
    later = RemoteMail("2", "INBOX", "<later-order@example.com>", "order-thread", "teacher@example.com", "teacher@example.com",
        "owner@qq.com", "Re: 安排", "2025-01-05T10:00:00Z", "较新回复", "<root-order@example.com>", "<root-order@example.com>")
    older = RemoteMail("3", "INBOX", "<older-order@example.com>", "order-thread", "teacher@example.com", "teacher@example.com",
        "owner@qq.com", "Re: 安排", "Sun, 05 Jan 2020 10:00:00 +0000", "较早回复", "<root-order@example.com>", "<root-order@example.com>")
    state.store.upsert_item(root, account="owner@qq.com")
    state.store.upsert_item(later, account="owner@qq.com")
    state.store.upsert_item(older, account="owner@qq.com")

    items = state.client.get("/api/mail/items").json()["items"]
    detail = state.client.get(f"/api/mail/items/{items[0]['id']}").json()
    assert [entry["body"] for entry in detail["thread"]] == ["较早回复", "第一封", "较新回复"]


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
    assert imap.query == 'OR OR TO "teacher@example.com" CC "teacher@example.com" BCC "teacher@example.com"'
    assert [row.message_id for row in rows] == ["<sent-1@example.com>"]
    assert rows[0].in_reply_to == "<root@example.com>"
    assert rows[0].references == "<root@example.com>"
    with pytest.raises(MailAdapterError):
        adapter.related_sent("Sent", 'teacher@example.com" OR ALL', 24)


@pytest.mark.parametrize("recipient_header", ["Cc", "Bcc"])
def test_qq_related_sent_accepts_exact_copied_recipient(recipient_header):
    class Imap:
        def select(self, _folder, readonly=True):
            assert readonly
            return "OK", [b"1"]

        def uid(self, command, *args):
            if command == "SEARCH":
                assert args[-1] == 'OR OR TO "teacher@example.com" CC "teacher@example.com" BCC "teacher@example.com"'
                return "OK", [b"1"]
            assert command == "FETCH" and args[-1] == "(BODY.PEEK[])"
            message = EmailMessage()
            message["From"] = "owner@qq.com"
            message["To"] = "other@example.com"
            message[recipient_header] = "teacher@example.com"
            message["Message-ID"] = "<cc@example.com>"
            message["Date"] = "Tue, 20 Oct 2026 09:00:00 +0000"
            message.set_content("cc reply")
            return "OK", [(b"BODY", message.as_bytes())]

    adapter = QQMailAdapter(MailConnection("imap.qq.com", 993, "owner@qq.com"), "synthetic")
    adapter._imap = Imap()
    rows = adapter.related_sent("Sent", "teacher@example.com", 24)
    assert [row.message_id for row in rows] == ["<cc@example.com>"]
    assert "teacher@example.com" in rows[0].recipients


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


def test_role_answer_replay_ignores_new_run_id_but_keeps_original_job(mail_app):
    state = mail_app
    state.responses[:] = [{"decision": "needs_input", "reason": "等待主人选择", "questions": ["周五能参加吗？"], "body": ""}]
    state.mailbox.incoming = [incoming()]
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "role-replay-question", "process": True}))
    item = state.client.get("/api/mail/items").json()["items"][0]
    question = state.client.get(f"/api/mail/items/{item['id']}").json()["preparation"]
    first = state.service.enqueue_answer(item["id"], question_id=question["id"], answer="可以参加",
                                         request_id="role-answer-replay", source_role_id="role-1", run_id="run-original")
    replay = state.service.enqueue_answer(item["id"], question_id=question["id"], answer="可以参加",
                                          request_id="role-answer-replay", source_role_id="role-1", run_id="run-replayed")

    assert replay["jobId"] == first["jobId"]
    assert state.service.job_status(first["jobId"])["payload"]["runId"] == "run-original"


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


def test_recovered_answer_revalidates_original_question_after_preparation_was_saved(mail_app):
    state = mail_app
    state.responses[:] = [
        {"decision": "needs_input", "reason": "等待主人选择", "questions": ["周五能参加吗？"], "body": ""},
        {"decision": "draft", "reason": "主人已确认", "questions": [], "body": "我可以参加。"},
    ]
    state.mailbox.incoming = [incoming()]
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "interrupted-initial", "process": True}))
    item = state.client.get("/api/mail/items").json()["items"][0]
    question = state.client.get(f"/api/mail/items/{item['id']}").json()["preparation"]

    state.mailbox.fail_append = True
    async def run_answer():
        return await state.service.process_item(item["id"], question_id=question["id"],
                                                answer="可以参加", request_id="interrupted-answer")

    with pytest.raises(RuntimeError, match="synthetic interrupted"):
        state.client.portal.call(run_answer)
    assert state.store.task_status(f"answer:{item['id']}:{question['id']}:interrupted-answer", account="owner@qq.com") == "uncertain"
    saved_answer = state.store.preparation(account="owner@qq.com", task_key=f"answer:{item['id']}:{question['id']}:interrupted-answer")
    assert saved_answer["decision"] == "draft"
    assert state.mailbox.draft_rows == []
    state.mailbox.incoming.append(incoming("2", body="时间改到周六，请重新确认。", message_id="<changed-after-answer@example.com>", references="<invite@example.com>"))
    state.mailbox.fail_append = False

    async def retry_answer():
        return await state.service.process_item(item["id"], question_id=question["id"],
                                                answer="可以参加", request_id="interrupted-answer")

    with pytest.raises(ValueError, match="问题已更新"):
        state.client.portal.call(retry_answer)
    assert state.mailbox.draft_rows == []


def test_revoking_role_permission_blocks_a_queued_answer_before_draft_creation(mail_app):
    state = mail_app
    state.responses[:] = [
        {"decision": "needs_input", "reason": "等待主人选择", "questions": ["周五能参加吗？"], "body": ""},
        {"decision": "draft", "reason": "主人已确认", "questions": [], "body": "我可以参加。"},
    ]
    state.mailbox.incoming = [incoming()]
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "role-question", "process": True}))
    item = state.client.get("/api/mail/items").json()["items"][0]
    question = state.client.get(f"/api/mail/items/{item['id']}").json()["preparation"]
    state.client.portal.call(state.service.close)
    state.service.update_preferences(writing_style="", role_access=True)

    async def queue_answer():
        tool = MailManageTool(state.service)
        context = ToolContext("role-1", "role:role-1", "run-1", CancellationToken(), "answer-call")
        return await tool.execute({"action": "answer", "mailId": item["id"], "questionId": question["id"], "body": "可以参加"}, context)

    queued = state.client.portal.call(queue_answer)
    job_id = json.loads(queued.content)["jobId"]
    job = state.service.job_status(job_id)
    assert job["payload"]["sourceRoleId"] == "role-1"
    state.service.update_preferences(writing_style="", role_access=False)
    state.client.portal.call(state.service.start)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = state.client.get(f"/api/mail/jobs/{job_id}").json()["job"]
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert job["status"] == "failed"
    assert "许可已撤销" in job["error"]
    assert state.mailbox.draft_rows == []


def test_draft_records_real_input_sources_and_reports_unavailable_context(mail_app):
    state = mail_app
    state.mailbox.incoming = [incoming(body="来信" * 7000)]

    async def knowledge(query):
        return [SimpleNamespace(text="主人已同意参加。" * 200, document_id="decision-note", title="面试计划", location="第二段", url="javascript:alert(1)")]

    state.service.owner_search = knowledge
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "sources", "process": True}))
    item = state.client.get("/api/mail/items").json()["items"][0]
    detail = state.client.get(f"/api/mail/items/{item['id']}").json()
    preparation = detail["drafts"][0]["preparation"]
    source = next(row for row in preparation["sources"] if row["id"] == "decision-note")
    assert source["title"] == "面试计划"
    assert source["location"] == "第二段"
    assert source["url"] == ""
    assert source["truncated"] is True
    assert source["providedChars"] == 1200
    current = next(row for row in preparation["sources"] if row["kind"] == "current_email")
    assert current["truncated"] is True
    assert current["providedChars"] == 12000
    assert any("当前来信正文已截断" in warning for warning in preparation["warnings"])
    assert preparation["model"] == "synthetic-model"
    assert "synthetic-secret" not in json.dumps(detail)

    async def unavailable(query):
        raise RuntimeError("synthetic-secret should never be shown")

    state.service.owner_search = unavailable
    state.responses[:] = [{"decision": "draft", "body": "收到。", "reason": "仅参考来信", "questions": []}]
    finish_job(state.client, state.client.post(f"/api/mail/items/{item['id']}/regenerate", json={"requestId": "degraded"}))
    details = state.client.get(f"/api/mail/items/{item['id']}").json()
    assert any("主人资料读取失败" in warning for warning in details["preparation"]["warnings"])
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
