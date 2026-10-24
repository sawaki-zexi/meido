"""Mail assistance behavior through the HTTP and mailbox interfaces."""
import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from email.message import EmailMessage

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.mail.context import prepare_model_text
from backend.app.mail.preparation import parse_reply
from backend.app.mail import api as mail_api
from backend.app.mail import service as service_module
from backend.app.mail.adapter import QQMailAdapter, RemoteDraft, RemoteMail, MailAdapterError, _body
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
        self.accept_append_then_fail = False
        self.hide_draft_reads = 0
        self.fetch_sent_calls = []

    def close(self):
        pass

    def fetch_inbox(self, folder, after_uid=0, limit=None):
        rows = sorted((row for row in self.incoming if int(row.uid) > after_uid), key=lambda row: int(row.uid))
        return rows[:limit] if limit is not None else rows

    def sent_samples(self, folder, limit=8):
        return self.sent[-limit:]

    def fetch_sent(self, folder, after_uid=0, limit=None):
        self.fetch_sent_calls.append((folder, after_uid, limit))
        rows = [row for row in self.sent if int(row.uid) > after_uid]
        return rows[-limit:] if limit is not None else rows

    def related_sent(self, folder, participant, limit=24):
        return [row for row in self.sent if participant in row.recipients][-limit:]

    def drafts(self, folder):
        if self.hide_draft_reads:
            self.hide_draft_reads -= 1
            return []
        return list(self.draft_rows)

    def append_draft(self, folder, *, marker, recipients, subject, body, in_reply_to="", references=""):
        if self.fail_append:
            raise RuntimeError("synthetic interrupted draft write")
        draft = RemoteDraft(marker, str(len(self.draft_rows) + 1), folder, f"<{marker}@meido>", subject, recipients, body, body)
        self.draft_rows.append(draft)
        if self.accept_append_then_fail:
            self.accept_append_then_fail = False
            self.hide_draft_reads = 2
            raise RuntimeError("synthetic response lost after accepted append")
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


def test_mail_list_api_searches_inbound_content_and_returns_filtered_count(mail_app):
    state = mail_app
    first = incoming(uid="1", body="请确认下周排期", message_id="<search-api-1@example.com>")
    second = incoming(uid="2", body="周五下午线上沟通", message_id="<search-api-2@example.com>")
    second = RemoteMail(second.uid, second.folder, second.message_id, second.thread_id, second.sender, second.reply_to,
                        second.recipients, "会议安排", second.received_at, second.body_text, second.in_reply_to, second.references)
    state.store.upsert_item(first, account="owner@qq.com")
    state.store.upsert_item(second, account="owner@qq.com")

    response = state.client.get("/api/mail/items", params={"query": "排期", "limit": 50, "offset": 0})

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert [item["subject"] for item in response.json()["items"]] == ["面试安排"]
    stats = state.client.get("/api/mail/stats", params={"query": "排期"})
    assert stats.status_code == 200
    assert stats.json()["total"] == 1
    assert stats.json()["byStatus"]["new"] == 1
    assert "body" not in stats.json()


def test_mail_list_endpoints_validate_pagination_bounds(mail_app):
    client = mail_app.client

    assert client.get("/api/mail/items", params={"limit": 0}).status_code == 422
    assert client.get("/api/mail/items", params={"offset": -1}).status_code == 422
    assert client.get("/api/mail/followup-reminders", params={"limit": 0}).status_code == 422
    assert client.get("/api/mail/reminder-batches", params={"limit": 101}).status_code == 422
    assert client.get("/api/mail/audit", params={"limit": 201}).status_code == 422


def test_mail_stats_counts_unpersisted_current_thread_followup_state(mail_app):
    state = mail_app
    state.store.upsert_item(incoming(uid="126", body="请确认统计里的线程。"), account="owner@qq.com")

    stats = state.service.mail_stats()

    assert stats["followups"]["to_reply"] == 1


def test_reply_provenance_records_pipeline_confidence_and_cleaned_history_context(mail_app):
    state = mail_app
    state.responses[:] = [{"decision": "draft", "confidence": "low", "reason": "请主人审核", "questions": [], "body": "收到。"}]
    state.mailbox.incoming = [incoming()]
    state.mailbox.sent = [RemoteMail("90", "Sent", "<history@example.com>", "history-thread", "owner@qq.com",
        "owner@qq.com", "teacher@example.com", "之前的回复", "2026-09-01T09:00:00Z",
        "老师您好，已经确认。\n\nOn Tue, someone wrote:\n> old quoted request")]

    async def knowledge(_query):
        return [SimpleNamespace(text="主人正在准备面试。", document_id="note-1", title="面试计划")]

    state.service.owner_search = knowledge
    state.client.put("/api/mail/preferences", json={"writingStyle": "简短直接", "roleAccess": False})
    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "provenance", "process": True}))
    item = state.client.get("/api/mail/items").json()["items"][0]
    detail = state.client.get(f"/api/mail/items/{item['id']}").json()
    preparation = detail["drafts"][0]["preparation"]
    prompt = state.prompts[0]

    assert preparation["provider"] == "test"
    assert preparation["model"] == "synthetic-model"
    assert len(preparation["promptHash"]) == 64
    assert preparation["contextMetadata"]["prompt"]["messageCount"] == 2
    assert preparation["contextMetadata"]["prompt"]["hash"] == preparation["promptHash"]
    assert preparation["pipelineVersion"] == 5
    assert preparation["confidence"] == "low"
    assert preparation["contextMetadata"]["sentHistory"] == {
        "retrievalFailed": False, "retrievedCount": 1, "injectedCount": 1, "maxRetrieved": 24, "maxExamples": 3,
    }
    assert preparation["contextMetadata"]["ownerKnowledge"]["injectedCount"] == 1
    assert preparation["contextMetadata"]["writingStyle"]["custom"] is True
    assert len(prompt["sentSamples"][0]["body"]) <= 600
    assert "old quoted request" not in prompt["sentSamples"][0]["body"]
    source = next(row for row in preparation["sources"] if row["kind"] == "sent_sample")
    assert source["quotedRemoved"] is True


def test_rich_model_usage_is_persisted_and_exposed_in_preparation_context(mail_app):
    state = mail_app
    state.responses[:] = [{"decision": "draft", "confidence": "high", "reason": "已具备信息", "questions": [], "body": "您好，已确认。"}]

    async def complete_with_usage(messages, configuration):
        state.prompts.append(json.loads(messages[1]["content"]))
        return service_module.MailModelCompletion(
            json.dumps(state.responses.pop(0), ensure_ascii=False),
            {"inputTokens": 111, "outputTokens": 17, "cost": {"total": 0.0012}},
        )

    state.service.complete_model = complete_with_usage
    state.mailbox.incoming = [incoming(uid="123", message_id="<usage-1@example.com>")]

    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "rich-usage", "process": True}))

    item = state.client.get("/api/mail/items").json()["items"][0]
    preparation = state.client.get(f"/api/mail/items/{item['id']}").json()["drafts"][0]["preparation"]
    assert preparation["modelUsage"] == {"inputTokens": 111, "outputTokens": 17, "cost": {"total": 0.0012}}
    assert preparation["contextMetadata"]["prompt"]["usage"] == preparation["modelUsage"]


def test_sent_sample_excludes_current_thread_by_message_identity_and_records_reply_headers(mail_app):
    state = mail_app
    current_base = incoming()
    current = RemoteMail(current_base.uid, current_base.folder, current_base.message_id, "incoming-thread",
        current_base.sender, current_base.reply_to, current_base.recipients, current_base.subject,
        current_base.received_at, current_base.body_text, current_base.in_reply_to, current_base.references)
    sent_copy = RemoteMail("77", "Sent", current.message_id, "different-provider-thread", "owner@qq.com",
        "owner@qq.com", "teacher@example.com", "Re: 面试安排", "2026-10-18T09:00:00Z", "线程中的主人回复",
        current.message_id, current.message_id)
    unrelated = RemoteMail("78", "Sent", "<related-by-recipient@example.com>", "other-thread", "owner@qq.com",
        "owner@qq.com", "teacher@example.com", "称呼样本", "2026-10-17T09:00:00Z", "您好，老师。",
        "<prior@example.com>", "<prior@example.com>")
    state.mailbox.sent = [sent_copy, unrelated]
    item, _ = state.store.upsert_item(current, account="owner@qq.com")
    state.responses[:] = [{"decision": "draft", "reason": "可以回复", "questions": [], "body": "您好。"}]

    async def process_current():
        return await state.service.process_item(item.id, adapter=state.mailbox,
            folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"})

    state.client.portal.call(process_current)

    preparation = state.store.preparation(account="owner@qq.com", mail_id=item.id)
    sample_ids = {source["messageId"] for source in preparation["sources"] if source["kind"] == "sent_sample"}
    assert current.message_id not in sample_ids
    sample = next(source for source in preparation["sources"] if source["kind"] == "sent_sample")
    assert sample["accountScope"] == state.store.account_scope("owner@qq.com")
    assert sample["sourceUid"].endswith("Sent/78")
    assert sample["inReplyTo"] == "<prior@example.com>"
    assert sample["references"] == "<prior@example.com>"


def test_reply_context_does_not_inject_the_current_mail_twice(mail_app):
    state = mail_app
    current_body = "唯一当前来信正文，不应重复注入。"
    state.mailbox.incoming = [incoming(body=current_body)]

    finish_job(state.client, state.client.post("/api/mail/sync", json={"requestId": "dedupe-current", "process": True}))

    prompt = state.prompts[0]
    assert current_body == prompt["body"]
    assert all(row["body"] != current_body for row in prompt["thread"])
    item = state.client.get("/api/mail/items").json()["items"][0]
    preparation = state.client.get(f"/api/mail/items/{item['id']}").json()["drafts"][0]["preparation"]
    assert sum(source["kind"] == "current_email" and source["id"] == item["id"] for source in preparation["sources"]) == 1
    assert all(not (source["kind"] == "thread" and source["id"] == item["id"]) for source in preparation["sources"])


def test_unknown_append_result_is_reconciled_without_a_second_append(mail_app):
    state = mail_app
    state.mailbox.incoming = [incoming()]
    state.mailbox.accept_append_then_fail = True
    item, _ = state.store.upsert_item(incoming(), account="owner@qq.com")
    folders = {"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}

    async def initial_process():
        return await state.service.process_item(item.id, adapter=state.mailbox, folders=folders)

    with pytest.raises(RuntimeError, match="response lost"):
        state.client.portal.call(initial_process)
    assert len(state.mailbox.draft_rows) == 1
    assert state.store.task_status(f"auto:{item.id}:v1", account="owner@qq.com") == "uncertain"

    first_retry = state.client.portal.call(state.service.process_pending)
    assert first_retry["errors"]
    assert len(state.mailbox.draft_rows) == 1
    uncertain = state.store.unconfirmed_append_task(item.id, account="owner@qq.com")
    assert uncertain is not None and uncertain["reconcileAttempts"] == 1 and uncertain["due"] is False

    state.mailbox.hide_draft_reads = 0

    async def scheduled_recovery():
        return await state.service.sync(trigger="scheduled", process=True)

    recovered = state.client.portal.call(scheduled_recovery)
    assert recovered.processed == 1
    assert len(recovered.errors) == 0
    assert len(state.mailbox.draft_rows) == 1
    assert state.store.get_item(item.id, account="owner@qq.com").status == "drafted"
    assert len(state.prompts) == 1


def test_role_supplied_draft_retry_only_reconciles_an_unknown_append(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(), account="owner@qq.com")
    state.mailbox.accept_append_then_fail = True

    async def create_role_draft():
        return await state.service.role_action("draft", mail_id=item.id, query="主人提供的草稿正文",
            request_id="role-draft-unknown", role_id="role-1", run_id="run-1")

    with pytest.raises(RuntimeError, match="response lost"):
        state.client.portal.call(create_role_draft)
    assert len(state.mailbox.draft_rows) == 1
    role_task = f"{state.store.account_scope('owner@qq.com')}:role-draft:{item.id}:new:role-draft-unknown"
    assert state.store.task_status(role_task, account="owner@qq.com") == "uncertain"

    state.mailbox.hide_draft_reads = 2
    with pytest.raises(MailAdapterError, match="不会再次追加"):
        state.client.portal.call(create_role_draft)
    assert len(state.mailbox.draft_rows) == 1

    recovered = state.client.portal.call(create_role_draft)
    assert recovered["status"] == "created"
    assert len(state.mailbox.draft_rows) == 1


def test_unknown_append_requires_explicit_confirmation_before_new_version(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(), account="owner@qq.com")
    state.mailbox.accept_append_then_fail = True

    async def initial_process():
        return await state.service.process_item(item.id, adapter=state.mailbox,
            folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"})

    with pytest.raises(RuntimeError, match="response lost"):
        state.client.portal.call(initial_process)
    assert len(state.mailbox.draft_rows) == 1
    detail = state.client.get(f"/api/mail/items/{item.id}").json()
    assert detail["unconfirmedAppend"] is True
    assert detail["unconfirmedAppendDetails"]["due"] is True
    assert detail["unconfirmedAppendDetails"]["reconcileAttempts"] == 0
    assert detail["drafts"] == []

    refused = state.client.post(f"/api/mail/items/{item.id}/regenerate", json={"requestId": "confirmed-v2"})
    assert refused.status_code == 409
    assert len(state.mailbox.draft_rows) == 1
    assert len(state.prompts) == 1

    state.mailbox.hide_draft_reads = 0
    state.responses.append({"decision": "draft", "reason": "主人明确要求新版本", "body": "可以参加。", "questions": []})
    response = state.client.post(f"/api/mail/items/{item.id}/regenerate", json={
        "requestId": "confirmed-v2", "confirmUncertainNewVersion": True,
    })
    result = finish_job(state.client, response)

    assert result["status"] == "drafted"
    assert len(state.mailbox.draft_rows) == 2
    detail = state.client.get(f"/api/mail/items/{item.id}").json()
    assert detail["unconfirmedAppend"] is False
    assert [draft["revision"] for draft in reversed(detail["drafts"])] == [1, 2]


def test_queued_job_is_rejected_after_switching_away_and_back_to_same_account(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(), account="owner@qq.com")
    state.client.portal.call(state.service.close)
    old_epoch = state.store.connection().account_epoch
    queued = state.service.enqueue_job("process", target_id=item.id, request_id="old-account-epoch",
                                       payload={"requestId": "old-account-epoch"})
    state.store.save_connection(MailConnection("imap.qq.com", 993, "other@qq.com", verified=True,
        auto_draft_ready=True, enabled=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts"))
    state.store.save_connection(MailConnection("imap.qq.com", 993, "owner@qq.com", verified=True,
        auto_draft_ready=True, enabled=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts"))
    assert state.store.connection().account_epoch > old_epoch
    state.client.portal.call(state.service.start)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = state.client.get(f"/api/mail/jobs/{queued['jobId']}").json()["job"]
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)

    assert job["status"] == "failed"
    assert "账号已切换" in job["error"]
    assert state.prompts == []
    assert state.mailbox.draft_rows == []


def test_quote_cleanup_requires_a_recognizable_quote_boundary():
    prose = "邮件正文提到一个配置示例：\nFrom: notices@example.com\n请将该地址加入白名单。"
    prepared = prepare_model_text(prose, 2000, strip_quotes=True)
    assert prepared.text == prose
    assert prepared.quoted_removed is False

    quoted = "我的回复\n\n-----Original Message-----\nFrom: sender@example.com\nSent: Monday\nTo: owner@qq.com\nSubject: 原主题\n旧正文"
    cleaned = prepare_model_text(quoted, 2000, strip_quotes=True)
    assert cleaned.text == "我的回复"
    assert cleaned.quoted_removed is True

    localized = "之前已确认。\n\n发件人：老师\n发件时间：周一\n收件人：主人\n主题：原主题\n旧正文"
    localized_clean = prepare_model_text(localized, 2000, strip_quotes=True)
    assert localized_clean.text == "之前已确认。"
    assert localized_clean.quoted_removed is True


def test_model_reply_normalizes_newlines_and_rejects_repetitive_output():
    parsed = parse_reply(json.dumps({
        "decision": "draft", "reason": "可回复", "questions": [],
        "body": "第一段\\n\\n第二段。",
    }, ensure_ascii=False))
    assert parsed["body"] == "第一段\n\n第二段。"

    with pytest.raises(RuntimeError, match="异常重复"):
        parse_reply(json.dumps({
            "decision": "draft", "reason": "异常", "questions": [],
            "body": "x" * 80,
        }, ensure_ascii=False))


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


def test_qq_related_sent_keeps_valid_rows_beyond_invalid_candidate_window():
    messages = {}
    valid = EmailMessage()
    valid["From"] = "owner@qq.com"
    valid["To"] = "teacher@example.com"
    valid["Subject"] = "Valid old reply"
    valid["Message-ID"] = "<valid-old@example.com>"
    valid.set_content("valid")
    messages[b"1"] = valid.as_bytes()
    for uid in range(2, 32):
        invalid = EmailMessage()
        invalid["From"] = "owner@qq.com"
        invalid["To"] = "teacher@example.com.attacker"
        invalid["Subject"] = "Invalid candidate"
        invalid["Message-ID"] = f"<invalid-{uid}@example.com>"
        invalid.set_content("invalid")
        messages[str(uid).encode()] = invalid.as_bytes()

    class Imap:
        def select(self, _folder, readonly=True):
            assert readonly
            return "OK", [b"31"]

        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b" ".join(messages)]
            assert args[-1] == "(BODY.PEEK[])"
            return "OK", [(b"BODY", messages[args[0]])]

    adapter = QQMailAdapter(MailConnection("imap.qq.com", 993, "owner@qq.com"), "synthetic")
    adapter._imap = Imap()

    rows = adapter.related_sent("Sent", "teacher@example.com", 24)

    assert [row.message_id for row in rows] == ["<valid-old@example.com>"]


def test_qq_related_sent_sorts_the_full_candidate_window_before_limiting():
    messages = {}
    for uid in range(1, 73):
        message = EmailMessage()
        message["From"] = "owner@qq.com"
        message["To"] = "teacher@example.com"
        message["Subject"] = f"Reply {uid}"
        message["Message-ID"] = f"<sorted-{uid}@example.com>"
        message["Date"] = (datetime(2026, 1, 1, 9, tzinfo=timezone.utc) + timedelta(days=uid)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        message.set_content(f"Reply {uid}")
        messages[str(uid).encode()] = message.as_bytes()

    class Imap:
        def select(self, _folder, readonly=True):
            assert readonly
            return "OK", [b"72"]

        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b" ".join(messages)]
            assert args[-1] == "(BODY.PEEK[])"
            return "OK", [(b"BODY", messages[args[0]])]

    adapter = QQMailAdapter(MailConnection("imap.qq.com", 993, "owner@qq.com"), "synthetic")
    adapter._imap = Imap()

    rows = adapter.related_sent("Sent", "teacher@example.com", 24)

    assert len(rows) == 24
    assert rows[0].message_id == "<sorted-72@example.com>"
    assert rows[-1].message_id == "<sorted-49@example.com>"


def test_html_mail_body_is_sanitized_to_readable_text():
    message = EmailMessage()
    message.set_content("<p>第一段<br>第二段</p><script>危险代码</script><p>&amp; 结束</p>", subtype="html")

    assert _body(message) == "第一段\n第二段\n& 结束"


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


def test_qq_fetch_sent_preserves_marker_headers_and_all_recipients():
    message = EmailMessage()
    message["From"] = "owner@qq.com"
    message["To"] = "teacher@example.com"
    message["Cc"] = "assistant@example.com"
    message["Subject"] = "Re: 面试安排"
    message["Date"] = "Tue, 20 Oct 2026 09:00:00 +0000"
    message["Message-ID"] = "<sent-copy@example.com>"
    message["In-Reply-To"] = "<incoming@example.com>"
    message["References"] = "<root@example.com> <incoming@example.com>"
    message["X-Meido-Draft-ID"] = "draft-123"
    message.set_content("您好，周五可以。")
    raw = message.as_bytes()
    adapter = QQMailAdapter(MailConnection(host="imap.qq.com", port=993, username="owner@qq.com"), "secret")
    seen = []

    def fetch(folder, query="ALL", limit=None):
        seen.append((folder, query, limit))
        return [("42", raw)]

    adapter._fetch = fetch

    rows = adapter.fetch_sent("Sent", after_uid=41, limit=None)

    assert seen == [("Sent", "UID 42:*", None)]
    assert len(rows) == 1
    assert rows[0].draft_marker == "draft-123"
    assert rows[0].recipients == "teacher@example.com, assistant@example.com"
    assert rows[0].in_reply_to == "<incoming@example.com>"
    assert rows[0].references.endswith("<incoming@example.com>")


def test_sync_associates_unique_sent_copy_and_does_not_call_it_a_deleted_draft(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(message_id="<incoming-send@example.com>"), account="owner@qq.com")
    drafted = asyncio.run(state.service.process_item(item.id, adapter=state.mailbox,
        folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))
    local = state.store.get_draft(drafted.draft_id, account="owner@qq.com")
    assert local is not None

    state.mailbox.sent.append(RemoteMail(
        "90", "Sent", "<sent-copy@example.com>", "sent-thread", "owner@qq.com", "owner@qq.com",
        "teacher@example.com", local.subject, "2026-10-20T09:10:00Z", local.body,
        "<incoming-send@example.com>", "<incoming-send@example.com>", "",
    ))
    state.mailbox.draft_rows.clear()

    result = asyncio.run(state.service.sync(trigger="manual", process=False))

    assert not result.errors
    association = state.store.draft_send(local.id, account="owner@qq.com")
    assert association is not None
    assert association.state == "matched"
    assert association.sent_uid == "90"
    assert association.match_basis == "recipient+in_reply_to+subject+body"
    assert state.store.get_item(item.id, account="owner@qq.com").status == "sent"
    assert state.store.get_draft(local.id, account="owner@qq.com").status == "sent"
    assert state.store.checkpoint("Sent", account="owner@qq.com") == 90
    assert state.mailbox.fetch_sent_calls[-1] == ("Sent", 0, 200)
    thread_directions = [row.direction for row in state.store.thread_items(item.thread_id, account="owner@qq.com")]
    assert thread_directions == ["inbound", "outbound"]


def test_sync_keeps_ambiguous_sent_candidates_unconfirmed(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(message_id="<incoming-ambiguous@example.com>"), account="owner@qq.com")
    drafted = asyncio.run(state.service.process_item(item.id, adapter=state.mailbox,
        folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))
    local = state.store.get_draft(drafted.draft_id, account="owner@qq.com")
    assert local is not None
    for uid in ("91", "92"):
        state.mailbox.sent.append(RemoteMail(
            uid, "Sent", f"<sent-copy-{uid}@example.com>", "sent-thread", "owner@qq.com", "owner@qq.com",
            "teacher@example.com", local.subject, "2026-10-20T09:10:00Z", local.body,
            "<incoming-ambiguous@example.com>", "<incoming-ambiguous@example.com>", "",
        ))
    state.mailbox.draft_rows.clear()

    asyncio.run(state.service.sync(trigger="manual", process=False))

    association = state.store.draft_send(local.id, account="owner@qq.com")
    assert association is not None
    assert association.state == "ambiguous"
    assert association.candidate_count == 2
    assert state.store.get_item(item.id, account="owner@qq.com").status == "uncertain"
    assert state.store.get_draft(local.id, account="owner@qq.com").status == "missing"


def test_sent_read_failure_is_unknown_and_does_not_advance_checkpoint(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(message_id="<incoming-sent-error@example.com>"), account="owner@qq.com")
    drafted = asyncio.run(state.service.process_item(item.id, adapter=state.mailbox,
        folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"}))
    local = state.store.get_draft(drafted.draft_id, account="owner@qq.com")
    assert local is not None

    def fail_fetch(*_args, **_kwargs):
        raise RuntimeError("Sent 暂不可读")

    state.mailbox.fetch_sent = fail_fetch
    result = asyncio.run(state.service.sync(trigger="manual", process=False))

    assert any("已发送邮件关联同步失败" in error for error in result.errors)
    association = state.store.draft_send(local.id, account="owner@qq.com")
    assert association is not None and association.state == "unknown"
    assert state.store.checkpoint("Sent", account="owner@qq.com") == 0


def test_followup_state_and_reminder_are_idempotent_across_syncs(mail_app):
    state = mail_app
    delivered = []
    state.service.available_roles = lambda: ["role-reminder"]
    state.service.reminder_sender = lambda role_id, reminder_id, content: delivered.append((role_id, reminder_id, content))
    state.store.set_setting("reminder_role_id", "role-reminder")
    state.service.update_followup_preferences(enabled=True, delay_hours=1)
    item, _ = state.store.upsert_item(incoming(uid="10", body="请确认合同。"), account="owner@qq.com")

    first = asyncio.run(state.service.sync(trigger="manual", process=False))
    assert not first.errors
    followup = state.service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert followup is not None
    assert followup.state == "to_reply"
    assert followup.reminder_enabled is True
    reminders = state.store.followup_reminders(account="owner@qq.com")
    assert len(reminders) == 1
    assert reminders[0].state == "delivered"
    assert len(delivered) == 1
    asyncio.run(state.service.sync(trigger="manual", process=False))
    assert len(state.store.followup_reminders(account="owner@qq.com")) == 1
    assert len(delivered) == 1


def test_explicit_followup_draft_is_idempotent_and_keeps_owner_review_boundary(mail_app):
    state = mail_app
    incoming_item, _ = state.store.upsert_item(
        incoming(uid="30", body="上次邮件请问进展如何？", message_id="<followup-incoming@example.com>"),
        account="owner@qq.com",
    )
    state.store.upsert_item(
        RemoteMail(
            "31", "Sent", "<followup-outgoing@example.com>", incoming_item.thread_id, "owner@qq.com",
            "owner@qq.com", "teacher@example.com", "Re: 面试安排", "2026-10-21T09:00:00Z", "请告诉我是否可以安排？",
            "<followup-incoming@example.com>", "<followup-incoming@example.com>",
        ),
        account="owner@qq.com",
        direction="outbound",
    )
    state.service.refresh_followup_states("owner@qq.com")
    assert state.service.thread_followup_state(incoming_item.thread_id, account="owner@qq.com").state == "awaiting_reply"

    first = asyncio.run(state.service.create_followup_draft(incoming_item.thread_id, request_id="followup-1"))
    second = asyncio.run(state.service.create_followup_draft(incoming_item.thread_id, request_id="followup-2"))

    assert first["status"] == "drafted"
    assert second["draftId"] == first["draftId"]
    assert second["recipients"] == "teacher@example.com"
    assert len(state.mailbox.draft_rows) == 1
    assert state.store.list_drafts_for_mail(incoming_item.id, account="owner@qq.com")[0].revision == 1


def test_due_waiting_thread_can_auto_create_one_reviewable_followup_draft(mail_app):
    state = mail_app
    delivered = []
    state.service.available_roles = lambda: ["role-reminder"]
    state.service.reminder_sender = lambda role_id, reminder_id, content: delivered.append((role_id, reminder_id, content))
    state.store.set_setting("reminder_role_id", "role-reminder")
    incoming_item, _ = state.store.upsert_item(
        RemoteMail(
            "40", "INBOX", "<auto-followup-in@example.com>", "auto-followup-thread", "teacher@example.com",
            "teacher@example.com", "owner@qq.com", "项目进度", "2026-10-20T09:00:00Z", "想了解项目进度。",
        ),
        account="owner@qq.com",
    )
    state.store.upsert_item(
        RemoteMail(
            "41", "Sent", "<auto-followup-out@example.com>", "auto-followup-thread", "owner@qq.com",
            "teacher@example.com", "teacher@example.com", "Re: 项目进度", "2026-10-21T09:00:00Z", "请确认预计完成时间可以吗？",
            "<auto-followup-in@example.com>", "<auto-followup-in@example.com>",
        ),
        account="owner@qq.com",
        direction="outbound",
    )
    state.responses.append({"decision": "draft", "reason": "等待对方回复", "body": "您好，想跟进一下项目进度，方便时请回复，谢谢。", "questions": []})
    state.service.update_followup_preferences(enabled=True, delay_hours=1, auto_draft=True)

    asyncio.run(state.service.deliver_followup_reminders())

    assert len(state.mailbox.draft_rows) == 1
    assert len(state.store.list_drafts_for_mail(incoming_item.id, account="owner@qq.com")) == 1
    assert state.store.followup_reminders(account="owner@qq.com")[0].state == "delivered"
    assert delivered and "自动生成催复草稿" in delivered[0][2]

    # The due reminder and stable follow-up task are both idempotent.
    asyncio.run(state.service.deliver_followup_reminders())
    assert len(state.mailbox.draft_rows) == 1
    assert len(delivered) == 1


def test_auto_followup_draft_waits_for_verified_qq_draft_capability(mail_app):
    state = mail_app
    delivered = []
    state.service.available_roles = lambda: ["role-reminder"]
    state.service.reminder_sender = lambda role_id, reminder_id, content: delivered.append((role_id, reminder_id, content))
    state.store.set_setting("reminder_role_id", "role-reminder")
    incoming_item, _ = state.store.upsert_item(
        RemoteMail(
            "42", "INBOX", "<auto-followup-unverified-in@example.com>", "auto-followup-unverified", "teacher@example.com",
            "teacher@example.com", "owner@qq.com", "项目进度", "2026-10-20T09:00:00Z", "想了解项目进度。",
        ),
        account="owner@qq.com",
    )
    state.store.upsert_item(
        RemoteMail(
            "43", "Sent", "<auto-followup-unverified-out@example.com>", "auto-followup-unverified", "owner@qq.com",
            "teacher@example.com", "teacher@example.com", "Re: 项目进度", "2026-10-21T09:00:00Z", "请确认预计完成时间可以吗？",
            "<auto-followup-unverified-in@example.com>", "<auto-followup-unverified-in@example.com>",
        ),
        account="owner@qq.com",
        direction="outbound",
    )
    state.responses[:] = [{"decision": "draft", "reason": "等待对方回复", "body": "您好，想跟进一下项目进度。", "questions": []}]
    state.service.update_followup_preferences(enabled=True, delay_hours=1, auto_draft=True)
    state.store.update_connection(auto_draft_ready=False)
    # Capability verification changes the connection epoch; refresh the
    # snapshot before delivering the newly scoped reminder.
    state.service.refresh_followup_states("owner@qq.com")

    asyncio.run(state.service.deliver_followup_reminders())

    assert state.mailbox.draft_rows == []
    assert len(state.responses) == 1
    assert state.store.followup_reminders(account="owner@qq.com")[0].state == "delivered"
    assert delivered and "自动生成催复草稿" not in delivered[0][2]
    assert incoming_item.thread_id


def test_followup_disabled_keeps_state_without_creating_reminder(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="11", body="无需马上回复。"), account="owner@qq.com")

    asyncio.run(state.service.sync(trigger="manual", process=False))

    followup = state.service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert followup is not None and followup.state == "to_reply"
    assert followup.reminder_enabled is False
    assert state.store.followup_reminders(account="owner@qq.com") == []


def test_followup_manual_close_is_bound_to_the_current_event(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="12", body="请确认版本。"), account="owner@qq.com")
    asyncio.run(state.service.sync(trigger="manual", process=False))

    closed = state.service.set_followup_state(item.thread_id, "closed")
    assert closed is not None and closed["state"] == "closed"
    asyncio.run(state.service.sync(trigger="manual", process=False))
    assert state.service.thread_followup_state(item.thread_id, account="owner@qq.com").state == "closed"

    state.store.upsert_item(incoming(uid="13", body="补充一条新消息。", message_id="<new-event@example.com>"), account="owner@qq.com")
    asyncio.run(state.service.sync(trigger="manual", process=False))
    assert state.service.thread_followup_state(item.thread_id, account="owner@qq.com").state == "to_reply"


def test_followup_preferences_and_thread_state_api_support_close_and_clear(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="121", body="请确认 API 状态。"), account="owner@qq.com")

    assert state.client.get("/api/mail/followup").json() == {"enabled": False, "delayHours": 24, "autoDraft": False}
    saved = state.client.put("/api/mail/followup", json={"enabled": True, "delayHours": 12, "autoDraft": True})
    assert saved.status_code == 200
    assert saved.json() == {"enabled": True, "delayHours": 12, "autoDraft": True}
    assert state.client.get(f"/api/mail/threads/{item.thread_id}/followup").json()["followup"]["state"] == "to_reply"

    closed = state.client.put(f"/api/mail/threads/{item.thread_id}/followup", json={"state": "closed"})
    assert closed.status_code == 200
    assert closed.json()["followup"]["state"] == "closed"
    action = state.client.get("/api/mail/threads?state=closed").json()["threads"][0]
    assert action["nextAction"] == "none"
    cleared = state.client.put(f"/api/mail/threads/{item.thread_id}/followup", json={"state": "clear"})
    assert cleared.status_code == 200
    assert cleared.json()["followup"]["state"] == "to_reply"
    assert state.client.put(f"/api/mail/threads/{item.thread_id}/followup", json={"state": "invalid"}).status_code == 422
    assert state.client.get("/api/mail/threads/missing-thread/followup").status_code == 404


def test_thread_action_api_returns_one_account_scoped_action_per_thread(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="18", body="请确认合同。"), account="owner@qq.com")
    state.service.refresh_followup_states("owner@qq.com")
    state.store.upsert_item(incoming(uid="19", body="另一个事项。", message_id="<other@example.com>"), account="other@qq.com")

    response = state.client.get("/api/mail/threads?action=review_or_reply")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["threads"][0]["threadId"] == item.thread_id
    assert payload["threads"][0]["nextAction"] == "review_or_reply"
    assert payload["threads"][0]["openQuestionCount"] >= 1
    assert state.client.get("/api/mail/threads?action=not-valid").status_code == 422


def test_thread_action_api_includes_unpersisted_threads_and_supports_state_filter(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="20", body="请确认会议时间。"), account="owner@qq.com")

    response = state.client.get("/api/mail/threads?state=to_reply")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["threads"][0]["threadId"] == item.thread_id
    assert payload["threads"][0]["nextAction"] == "review_or_reply"
    assert payload["threads"][0]["latestMailId"] == item.id


def test_thread_action_api_supports_needs_input_and_verification_filters(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="201", body="请确认会议时间。"), account="owner@qq.com")
    state.store.update_item(item.id, status="needs_input")

    needs_input = state.client.get("/api/mail/threads?action=needs_input")
    assert needs_input.status_code == 200
    assert needs_input.json()["count"] == 1
    assert needs_input.json()["threads"][0]["needsInput"] is True

    state.service.refresh_followup_states("owner@qq.com", sent_sync_ok=False, sent_sync_error="Sent 暂不可读")
    needs_verification = state.client.get("/api/mail/threads?action=needs_verification")
    assert needs_verification.status_code == 200
    assert needs_verification.json()["count"] == 1


def test_thread_action_api_marks_expired_commitment_as_overdue(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(
        RemoteMail(
            "204", "Sent", "<overdue@example.com>", "overdue-thread", "owner@qq.com",
            "owner@qq.com", "teacher@example.com", "材料", "2026-10-20T09:00:00Z",
            "我会在 2020-01-05 前提交材料。",
        ),
        account="owner@qq.com",
        direction="outbound",
    )
    overdue = state.client.get("/api/mail/threads?action=overdue")

    assert overdue.status_code == 200
    assert overdue.json()["count"] == 1
    assert overdue.json()["threads"][0]["threadId"] == item.thread_id
    assert overdue.json()["threads"][0]["overdue"] is True


def test_thread_details_aggregate_drafts_across_the_selected_thread(mail_app):
    state = mail_app
    first, _ = state.store.upsert_item(incoming(uid="202", body="请确认合同。"), account="owner@qq.com")
    second_remote = RemoteMail(
        "203", "INBOX", "<thread-second@example.com>", first.thread_id, "teacher@example.com",
        "teacher@example.com", "owner@qq.com", "Re: 面试安排", "2026-10-23T09:00:00Z", "请再确认时间。",
        first.message_id, first.message_id,
    )
    second, _ = state.store.upsert_item(second_remote, account="owner@qq.com")
    state.store.save_draft(
        first.id, task_key="thread-dossier-draft", remote_uid="1", folder="Drafts",
        body="您好，合同已确认。", subject="Re: 面试安排", recipients="teacher@example.com",
        message_id="<thread-dossier-draft>", generated_hash="g", remote_hash="r", draft_id="thread-dossier-draft",
    )
    state.store.save_preparation(
        first.id,
        task_key="thread-dossier-preparation",
        payload={
            "decision": "needs_input", "confidence": "low", "reason": "需要主人确认",
            "questions": ["是否确认合同？"], "sources": [], "warnings": [],
            "threadFingerprint": state.store.thread_fingerprint(first, account="owner@qq.com"),
        },
    )
    response = state.client.get(f"/api/mail/items/{second.id}")

    assert response.status_code == 200
    dossier = response.json()["threadDossier"]
    assert dossier["mailIds"] == [first.id, second.id]
    assert dossier["drafts"][0]["mailId"] == first.id
    assert dossier["preparations"][0]["mailId"] == first.id


def test_thread_action_api_reclassifies_when_new_event_invalidates_persisted_snapshot(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="21", body="请确认会议时间。"), account="owner@qq.com")
    state.service.refresh_followup_states("owner@qq.com")
    assert state.client.get("/api/mail/threads?action=review_or_reply").json()["count"] == 1

    state.store.upsert_item(
        RemoteMail(
                "22", "Sent", "<closed-event@example.com>", item.thread_id, "owner@qq.com",
                "owner@qq.com", "teacher@example.com", "Re: 面试安排", "2026-11-01T09:00:00Z",
            "谢谢，已经解决，无需回复。", item.message_id, item.message_id,
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    response = state.client.get("/api/mail/threads?action=none")
    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_item_and_thread_followup_details_reclassify_before_background_refresh(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="23", body="请确认会议时间。"), account="owner@qq.com")
    state.service.refresh_followup_states("owner@qq.com")
    assert state.client.get(f"/api/mail/items/{item.id}").json()["followup"]["state"] == "to_reply"

    state.store.upsert_item(
        RemoteMail(
            "24", "Sent", "<closed-detail-event@example.com>", item.thread_id, "owner@qq.com",
            "teacher@example.com", "teacher@example.com", "Re: 面试安排", "2026-11-02T09:00:00Z",
            "谢谢，已经解决，无需回复。", item.message_id, item.message_id,
        ),
        account="owner@qq.com",
        direction="outbound",
    )

    assert state.client.get(f"/api/mail/threads/{item.thread_id}/followup").json()["followup"]["state"] == "closed"
    assert state.client.get(f"/api/mail/items/{item.id}").json()["followup"]["state"] == "closed"


def test_disabling_followup_cancels_a_future_pending_reminder(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="17", body="请稍后提醒。"), account="owner@qq.com")
    state.service.update_followup_preferences(enabled=True, delay_hours=24)
    pending = state.store.followup_reminders(account="owner@qq.com")
    assert pending and pending[0].state == "pending"

    state.service.update_followup_preferences(enabled=False, delay_hours=24)

    cancelled = state.store.followup_reminders(account="owner@qq.com")
    assert cancelled and cancelled[0].state == "cancelled"
    assert state.service.thread_followup_state(item.thread_id, account="owner@qq.com").reminder_enabled is False


def test_sent_rescan_recovers_a_copy_older_than_the_incremental_checkpoint(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(message_id="<incoming-rescan@example.com>"), account="owner@qq.com")
    drafted = asyncio.run(state.service.process_item(
        item.id, adapter=state.mailbox, folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"},
    ))
    local = state.store.get_draft(drafted.draft_id, account="owner@qq.com")
    assert local is not None
    state.store.set_checkpoint("Sent", 100, account="owner@qq.com")
    state.mailbox.sent.append(RemoteMail(
        "5", "Sent", "<sent-rescan@example.com>", "sent-thread", "owner@qq.com", "owner@qq.com",
        "teacher@example.com", local.subject, "2026-10-20T09:10:00Z", local.body,
        "<incoming-rescan@example.com>", "<incoming-rescan@example.com>", "",
    ))
    state.mailbox.draft_rows.clear()

    result = asyncio.run(state.service.sync(trigger="manual", process=False))

    assert not result.errors
    association = state.store.draft_send(local.id, account="owner@qq.com")
    assert association is not None and association.state == "matched"
    assert state.store.get_item(item.id, account="owner@qq.com").status == "sent"
    assert state.mailbox.fetch_sent_calls[-2:] == [("Sent", 100, 200), ("Sent", 0, 200)]


def test_followup_sent_read_failure_suppresses_reminders_and_marks_direction_unknown(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="14", body="请确认最终版本。"), account="owner@qq.com")
    state.service.update_followup_preferences(enabled=True, delay_hours=24)
    state.service.refresh_followup_states("owner@qq.com", sent_sync_ok=True)
    assert state.service.thread_followup_state(item.thread_id, account="owner@qq.com").state == "to_reply"

    state.service.refresh_followup_states(
        "owner@qq.com", sent_sync_ok=False, sent_sync_error="Sent 暂不可读",
    )

    followup = state.service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert followup is not None and followup.state == "unknown"
    assert followup.last_error == "Sent 暂不可读"
    assert all(reminder.state == "cancelled" for reminder in state.store.followup_reminders(account="owner@qq.com"))


def test_followup_delay_change_cancels_old_schedule_and_creates_new_generation(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="15", body="请确认提醒时间。"), account="owner@qq.com")
    state.service.update_followup_preferences(enabled=True, delay_hours=24)
    first = state.service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert first is not None and first.next_reminder_at is not None
    state.service.update_followup_preferences(enabled=True, delay_hours=1)
    second = state.service.thread_followup_state(item.thread_id, account="owner@qq.com")
    reminders = state.store.followup_reminders(account="owner@qq.com")

    assert second is not None and second.next_reminder_at is not None
    assert second.next_reminder_at != first.next_reminder_at
    assert len(reminders) == 2
    assert {reminder.state for reminder in reminders} == {"cancelled", "pending"}
    assert len({reminder.due_key for reminder in reminders}) == 2


def test_inbound_status_sent_does_not_turn_followup_into_awaiting_reply(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="16", body="来信仍待处理。"), account="owner@qq.com")
    state.store.update_item(item.id, status="sent")

    state.service.refresh_followup_states("owner@qq.com", sent_sync_ok=True)

    followup = state.service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert followup is not None and followup.state == "to_reply"


def test_mail_audit_is_scoped_to_the_active_account(tmp_path):
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection("imap.qq.com", 993, "owner@qq.com", account_epoch=1))
    store.audit("read", "role-owner", "run-owner", None, "completed")
    store.save_connection(MailConnection("imap.qq.com", 993, "other@qq.com", account_epoch=1))
    store.audit("read", "role-other", "run-other", None, "completed")

    assert [row["source_role_id"] for row in store.list_audits(account="owner@qq.com")] == ["role-owner"]
    assert [row["source_role_id"] for row in store.list_audits(account="other@qq.com")] == ["role-other"]


def test_legacy_thread_state_without_manual_columns_is_migrated(tmp_path):
    path = tmp_path / "mail.db"
    store = MailStore(path)
    scope = store.account_scope("owner@qq.com")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE mail_thread_states")
        db.execute(
            """CREATE TABLE mail_thread_states (
                account_scope TEXT NOT NULL, thread_id TEXT NOT NULL, state TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '', latest_event_at TEXT, next_reminder_at TEXT,
                reminder_enabled INTEGER NOT NULL DEFAULT 0, event_fingerprint TEXT NOT NULL DEFAULT '',
                last_evaluated_at TEXT, last_error TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(account_scope, thread_id)
            )""",
        )
        db.execute(
            """INSERT INTO mail_thread_states
            (account_scope,thread_id,state,reason,event_fingerprint,updated_at)
            VALUES (?,?,?,?,?,?)""",
            (scope, "legacy-thread", "closed", "旧状态", "legacy-fingerprint", "2026-10-24T00:00:00Z"),
        )

    migrated = MailStore(path)
    state = migrated.thread_state("legacy-thread", account="owner@qq.com")
    assert state is not None
    assert state.state == "closed"
    assert state.manual_state is None
    assert state.manual_event_fingerprint is None


def test_account_switch_hides_thread_followup_state_and_reminders(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="18", body="旧账号线程。"), account="owner@qq.com")
    state.service.update_followup_preferences(enabled=True, delay_hours=24)
    state.service.refresh_followup_states("owner@qq.com")
    assert state.service.thread_followup_state(item.thread_id, account="owner@qq.com") is not None
    assert state.store.followup_reminders(account="owner@qq.com")

    state.store.save_connection(MailConnection(
        "imap.qq.com", 993, "other@qq.com", verified=True, auto_draft_ready=True,
        enabled=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))

    assert state.client.get(f"/api/mail/threads/{item.thread_id}/followup").status_code == 404
    assert state.client.get("/api/mail/followup-reminders").json()["reminders"] == []


def test_same_account_reconnect_invalidates_followup_snapshot_and_due_reminder(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="19", body="旧连接下的待处理来信。"), account="owner@qq.com")
    state.service.update_followup_preferences(enabled=True, delay_hours=24, auto_draft=True)
    state.service.refresh_followup_states("owner@qq.com")
    old_epoch = state.store.connection().account_epoch
    old_state = state.service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert old_state is not None and old_state.account_epoch == old_epoch
    assert state.store.followup_reminders(account="owner@qq.com", account_epoch=old_epoch)

    # Re-saving the same username with a changed verified draft capability is
    # a new connection epoch, even though the account scope is unchanged.
    state.store.update_connection(auto_draft_ready=False)
    new_epoch = state.store.connection().account_epoch
    assert new_epoch > old_epoch

    followup = state.client.get(f"/api/mail/threads/{item.thread_id}/followup").json()["followup"]
    assert followup["state"] == "unknown"
    assert followup["nextAction"] == "verify_thread"
    assert state.client.get("/api/mail/followup-reminders").json()["reminders"] == []

    asyncio.run(state.service.deliver_followup_reminders())
    assert state.mailbox.draft_rows == []

    state.service.refresh_followup_states("owner@qq.com")
    refreshed = state.service.thread_followup_state(item.thread_id, account="owner@qq.com")
    assert refreshed is not None and refreshed.account_epoch == new_epoch
    assert refreshed.state == "to_reply"


def test_followup_snapshot_reclassifies_when_provider_metadata_changes(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="20", body="请确认安排。"), account="owner@qq.com")
    state.service.refresh_followup_states("owner@qq.com")
    before = state.client.get(f"/api/mail/threads/{item.thread_id}/followup").json()["followup"]

    changed = incoming(uid="20", body="请确认安排。", message_id=item.message_id)
    changed = RemoteMail(
        changed.uid, changed.folder, changed.message_id, changed.thread_id,
        "different-sender@example.com", changed.reply_to, changed.recipients,
        "更正后的主题", changed.received_at, changed.body_text,
        changed.in_reply_to, changed.references,
    )
    state.store.upsert_item(changed, account="owner@qq.com")

    after = state.client.get(f"/api/mail/threads/{item.thread_id}/followup").json()["followup"]
    assert after["eventFingerprint"] != before["eventFingerprint"]


def test_role_followup_action_returns_scoped_state_and_audits(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="124", body="角色查询线程。"), account="owner@qq.com")
    state.service.refresh_followup_states("owner@qq.com")

    result = asyncio.run(state.service.role_action(
        "followup", thread_id=item.thread_id, role_id="role-followup", run_id="run-followup",
    ))

    assert result["followup"]["state"] == "to_reply"
    audits = state.store.list_audits(account="owner@qq.com")
    assert any(row["operation"] == "followup" and row["source_role_id"] == "role-followup" for row in audits)


def test_role_followup_list_includes_new_unpersisted_threads(mail_app):
    state = mail_app
    item, _ = state.store.upsert_item(incoming(uid="125", body="角色查询但尚未刷新。"), account="owner@qq.com")

    result = asyncio.run(state.service.role_action(
        "followup", role_id="role-followup", run_id="run-followup-list",
    ))

    assert [entry["threadId"] for entry in result["followups"]] == [item.thread_id]
    assert result["followups"][0]["state"] == "to_reply"


def test_mail_manage_stats_forwards_query_filter(mail_app):
    state = mail_app
    state.service.update_preferences(writing_style="", role_access=True)
    state.store.upsert_item(incoming(uid="126", body="请确认排期。", message_id="<role-stats-match@example.com>"), account="owner@qq.com")
    state.store.upsert_item(incoming(uid="127", body="仅供参考。", message_id="<role-stats-other@example.com>"), account="owner@qq.com")

    async def call_tool():
        tool = MailManageTool(state.service)
        context = ToolContext("role-stats", "role:role-stats", "run-stats", CancellationToken(), "stats-call")
        return await tool.execute({"action": "stats", "query": "排期"}, context)

    result = state.client.portal.call(call_tool)
    payload = json.loads(result.content)
    assert result.is_error is False
    assert payload["query"] == "排期"
    assert payload["total"] == 1
    assert payload["byStatus"]["new"] == 1


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
    prompt_metadata = preparation["contextMetadata"]["prompt"]
    assert prompt_metadata["inputChars"] <= service_module.MAIL_PROMPT_MAX_CHARS
    assert prompt_metadata["outputChars"] > 0
    assert prompt_metadata["modelElapsedMs"] >= 0
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


def test_prompt_budget_compresses_context_and_records_final_input_size(mail_app):
    state = mail_app
    state.responses[:] = [{"decision": "draft", "reason": "上下文已压缩", "body": "收到。", "questions": []}]
    captured: list[list[dict[str, object]]] = []

    async def complete(messages, _configuration):
        captured.append(messages)
        return json.dumps(state.responses.pop(0), ensure_ascii=False)

    state.service.complete_model = complete

    for uid in range(1, 9):
        state.store.upsert_item(
            incoming(uid=str(uid), body="旧上下文" * 2000, message_id=f"<thread-{uid}@example.com>"),
            account="owner@qq.com",
        )
    target, _ = state.store.upsert_item(
        incoming(uid="9", body="当前正文" * 7000, message_id="<thread-target@example.com>"),
        account="owner@qq.com",
    )

    async def knowledge(_query):
        return [SimpleNamespace(text="主人资料" * 1000, document_id=f"note-{index}", title=f"资料 {index}") for index in range(5)]

    state.service.owner_search = knowledge
    result = asyncio.run(state.service._draft_content(
        target, state.mailbox, connection=state.store.connection(),
    ))

    assert captured
    actual_input_chars = sum(len(str(message.get("content") or "")) for message in captured[0])
    assert actual_input_chars <= service_module.MAIL_PROMPT_MAX_CHARS
    assert result["contextMetadata"]["prompt"]["inputChars"] == actual_input_chars
    assert result["contextMetadata"]["prompt"]["inputChars"] <= service_module.MAIL_PROMPT_MAX_CHARS
    assert any("模型输入超过" in warning for warning in result["warnings"])
    current_source = next(source for source in result["sources"] if source["kind"] == "current_email")
    assert current_source["providedChars"] <= 12000
    thread_source = next(source for source in result["sources"] if source["kind"] == "thread")
    assert any("当前往来正文已截断至" in warning and str(thread_source["providedChars"]) in warning for warning in result["warnings"])


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
