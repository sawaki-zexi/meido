"""Replayable mail-draft quality cases using the real local mail pipeline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.mail.adapter import RemoteDraft, RemoteMail
from backend.app.mail.models import MailConnection
from backend.app.mail.service import MailService
from backend.app.mail.store import MailStore
from backend.app.mail.evaluation import MailQualityCase, evaluate_preparation, summarize_quality_results
from scripts.evaluate_mail_quality import _read_judgements, _run_replay


CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "mail_quality_cases.json").read_text(encoding="utf-8"))


class Credentials:
    def get(self, key: str) -> str:
        return "fixture-secret" if key == "qq-password" else ""

    def set(self, key: str, value: str) -> None:
        del key, value


class ReplayMailbox:
    def __init__(self, history: list[RemoteMail]) -> None:
        self.history = history
        self.draft_rows: list[RemoteDraft] = []

    def close(self) -> None:
        return None

    def related_sent(self, folder: str, participant: str, limit: int = 24) -> list[RemoteMail]:
        del folder, participant
        return self.history[:limit]

    def drafts(self, folder: str) -> list[RemoteDraft]:
        return [row for row in self.draft_rows if row.folder == folder]

    def append_draft(self, folder: str, *, marker: str, recipients: str, subject: str, body: str,
                     in_reply_to: str = "", references: str = "") -> RemoteDraft:
        del in_reply_to, references
        row = RemoteDraft(marker, str(len(self.draft_rows) + 1), folder, f"<{marker}@fixture>", subject,
                          recipients, body, body)
        self.draft_rows.append(row)
        return row


def _mail(case: dict[str, object]) -> RemoteMail:
    sender = str(case["sender"])
    return RemoteMail(
        "1", "INBOX", f"<{case['id']}@fixture>", f"thread-{case['id']}", sender, sender,
        "owner@qq.com", str(case["subject"]), "2026-10-20T09:00:00Z", str(case["body"]),
    )


def _history(case: dict[str, object]) -> list[RemoteMail]:
    rows: list[RemoteMail] = []
    for value in case.get("history", []):
        rows.append(RemoteMail(
            str(value["uid"]), "Sent", str(value["messageId"]), f"history-{value['uid']}",
            "owner@qq.com", "owner@qq.com", str(case["sender"]), str(value["subject"]),
            "2026-10-01T09:00:00Z", str(value["body"]),
        ))
    return rows


async def _run_case(tmp_path: Path, case: dict[str, object]) -> tuple[object, MailStore, ReplayMailbox, list[dict[str, object]]]:
    store = MailStore(tmp_path / "mail.db")
    store.save_connection(MailConnection(
        host="imap.qq.com", port=993, username="owner@qq.com", verified=True,
        auto_draft_ready=True, enabled=True, inbox_folder="INBOX", sent_folder="Sent", drafts_folder="Drafts",
    ))
    mailbox = ReplayMailbox(_history(case))
    prompts: list[dict[str, object]] = []

    async def complete(messages, _configuration):
        prompts.append(json.loads(messages[1]["content"]))
        return json.dumps(case["model"], ensure_ascii=False)

    service = MailService(
        store, credential_store=Credentials(), complete_model=complete,
        configuration_getter=lambda: SimpleNamespace(model="quality-fixture", provider="fixture"),
    )
    service.update_preferences(writing_style=str(case.get("writingStyle", "")), role_access=False)
    item, _ = store.upsert_item(_mail(case), account="owner@qq.com")
    result = await service.process_item(
        item.id, adapter=mailbox,
        folders={"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts"},
    )
    return result, store, mailbox, prompts


@pytest.mark.parametrize("case", CASES, ids=[str(case["id"]) for case in CASES])
def test_mail_quality_case_replays_real_pipeline(tmp_path, case):
    result, store, mailbox, prompts = asyncio.run(_run_case(tmp_path, case))
    expected = case["expected"]
    body = str(case["model"].get("body", ""))

    expected_status = expected.get("itemStatus", "drafted" if expected["decision"] == "draft" else expected["decision"])
    assert result.status == expected_status
    assert len(mailbox.draft_rows) == expected["drafts"]
    for text in expected.get("required", []):
        assert text in body
    for text in expected.get("forbidden", []):
        assert text not in body
    if expected["decision"] == "needs_input":
        preparation = store.preparation(account="owner@qq.com", mail_id=result.id)
        assert preparation is not None
        expected_questions = expected.get("questions") or ([expected["question"]] if expected.get("question") else [])
        assert expected_questions
        assert all(question in preparation["questions"] for question in expected_questions)
    if expected.get("promptStyle"):
        assert prompts and prompts[0]["writingStyle"] == expected["promptStyle"]
    if expected.get("promptHistorySubject"):
        assert prompts and any(row["subject"] == expected["promptHistorySubject"] for row in prompts[0]["sentSamples"])


@pytest.mark.parametrize("case", CASES, ids=[str(case["id"]) for case in CASES])
def test_quality_evaluator_records_checks_and_model_attribution(tmp_path, case):
    result, store, _mailbox, _prompts = asyncio.run(_run_case(tmp_path, case))
    preparation = store.preparation(account="owner@qq.com", mail_id=result.id)
    assert preparation is not None

    evaluated = evaluate_preparation(MailQualityCase.from_mapping(case), preparation)
    report = summarize_quality_results([evaluated])

    assert evaluated.passed is True
    assert evaluated.prompt_hash == preparation["promptHash"]
    assert evaluated.model == "quality-fixture"
    assert report["total"] == 1
    assert report["passed"] == 1
    assert report["reviewPending"] == 1
    assert report["usage"]["calls"] == 1
    assert report["usage"]["models"][0]["model"] == "quality-fixture"


def test_quality_evaluator_accepts_an_injected_semantic_judge():
    case = MailQualityCase(case_id="judge-case", expected_decision="draft", required=("已确认",))
    preparation = {"decision": "draft", "body": "已确认。", "questions": [], "provider": "fixture", "model": "test"}

    evaluated = evaluate_preparation(
        case,
        preparation,
        judge=lambda _case, _preparation: {
            "status": "pass",
            "score": 0.92,
            "checks": [{"name": "facts", "passed": True, "detail": "只使用给定事实"}],
            "notes": "通过语义抽检",
        },
    )

    assert evaluated.passed is True
    assert evaluated.judge_status == "pass"
    assert evaluated.judge_score == pytest.approx(0.92)
    assert evaluated.judge_checks[0]["name"] == "facts"
    assert evaluated.as_dict()["judgeStatus"] == "pass"


def test_quality_evaluator_fails_closed_when_semantic_judge_is_invalid():
    case = MailQualityCase(case_id="judge-invalid", expected_decision="draft")
    preparation = {"decision": "draft", "body": "已确认。", "questions": []}

    evaluated = evaluate_preparation(case, preparation, judge=lambda _case, _preparation: {"status": "pass", "score": 2})

    assert evaluated.passed is False
    assert evaluated.judge_status == "error"
    assert any(check["name"] == "semantic_judge" and check["passed"] is False for check in evaluated.checks)
    report = summarize_quality_results([evaluated])
    assert report["judgeFailed"] == 1


def test_quality_evaluator_preserves_provider_reported_usage_without_inventing_tokens():
    case = MailQualityCase(case_id="usage-case", expected_decision="draft")
    with_usage = evaluate_preparation(
        case,
        {
            "decision": "draft", "body": "已确认。", "questions": [],
            "modelUsage": {
                "inputTokens": 120, "outputTokens": 24, "cacheReadTokens": 8,
                "cacheWriteTokens": 0, "reasoningTokens": 3,
                "cost": {"total": 0.0042},
            },
        },
    )
    without_usage = evaluate_preparation(case, {"decision": "draft", "body": "已确认。", "questions": []})

    assert with_usage.input_tokens == 120
    assert with_usage.output_tokens == 24
    assert with_usage.cost_total == pytest.approx(0.0042)
    assert without_usage.input_tokens is None
    report = summarize_quality_results([with_usage, without_usage])
    assert report["usage"]["inputTokens"] == 120
    assert report["usage"]["reportedTokenCalls"] == 1
    assert report["usage"]["costTotal"] == pytest.approx(0.0042)


def test_quality_replay_cli_accepts_the_repository_model_fixture():
    results = _run_replay(CASES)

    assert len(results) == len(CASES) == 8
    assert all(result.passed for result in results)
    assert {result.model for result in results} == {"fixture-replay"}


def test_quality_replay_accepts_an_independent_judge_report(tmp_path):
    report_path = tmp_path / "judge.json"
    report_path.write_text(json.dumps({
        "judgements": {
            "owner-decision-required": {
                "status": "pass", "score": 0.97,
                "checks": [{"name": "decision-boundary", "passed": True, "detail": "没有替主人作决定"}],
            },
        },
    }, ensure_ascii=False), encoding="utf-8")

    judgements = _read_judgements(report_path)
    assert judgements is not None
    results = _run_replay(CASES[:1], judgements)

    assert results[0].judge_status == "pass"
    assert results[0].judge_score == pytest.approx(0.97)
    assert results[0].passed is True


def test_quality_replay_fails_closed_when_judge_report_omits_a_case(tmp_path):
    report_path = tmp_path / "judge.json"
    report_path.write_text(json.dumps({"owner-decision-required": {"status": "pass"}}), encoding="utf-8")

    judgements = _read_judgements(report_path)
    results = _run_replay(CASES[:2], judgements)

    assert results[0].judge_status == "pass"
    assert results[1].judge_status == "error"
    assert results[1].passed is False
