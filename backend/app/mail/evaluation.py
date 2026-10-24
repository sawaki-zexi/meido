"""Replayable and real-model quality evaluation for mail preparation.

The evaluator deliberately sits outside the draft-writing path. It consumes a
validated preparation result, checks only the expectations supplied by the
reviewer, and emits an auditable record. A real-model run may call the existing
model/context pipeline, but it never appends to the remote Drafts folder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Callable, Mapping
from math import isfinite
from typing import Any


QualityJudge = Callable[["MailQualityCase", Mapping[str, Any]], Mapping[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object) -> str:
    return str(value or "")


@dataclass(frozen=True, slots=True)
class MailQualityCase:
    case_id: str
    expected_decision: str
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    expected_questions: tuple[str, ...] = ()
    reviewer_status: str = "pending"
    reviewer_notes: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MailQualityCase":
        expected = value.get("expected") if isinstance(value.get("expected"), Mapping) else value
        return cls(
            case_id=_text(value.get("id") or value.get("caseId")),
            expected_decision=_text(expected.get("decision") or value.get("expectedDecision")),
            required=tuple(_text(item) for item in expected.get("required", ()) if _text(item)),
            forbidden=tuple(_text(item) for item in expected.get("forbidden", ()) if _text(item)),
            expected_questions=tuple(
                _text(item)
                for item in (
                    expected.get("questions")
                    or ([expected["question"]] if expected.get("question") else [])
                )
                if _text(item)
            ),
            reviewer_status=_text(expected.get("reviewerStatus") or "pending") or "pending",
            reviewer_notes=_text(expected.get("reviewerNotes")),
        )


@dataclass(frozen=True, slots=True)
class MailQualityResult:
    case_id: str
    expected_decision: str
    actual_decision: str
    passed: bool
    checks: tuple[dict[str, object], ...]
    provider: str = ""
    model: str = ""
    model_configuration_id: str = ""
    pipeline_version: int | None = None
    prompt_hash: str = ""
    input_chars: int = 0
    output_chars: int = 0
    model_elapsed_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_total: float | None = None
    reviewer_status: str = "pending"
    reviewer_notes: str = ""
    judge_status: str = "not_run"
    judge_score: float | None = None
    judge_checks: tuple[dict[str, object], ...] = ()
    judge_notes: str = ""
    evaluated_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, object]:
        return {
            "caseId": self.case_id,
            "expectedDecision": self.expected_decision,
            "actualDecision": self.actual_decision,
            "passed": self.passed,
            "checks": [dict(check) for check in self.checks],
            "provider": self.provider,
            "model": self.model,
            "modelConfigurationId": self.model_configuration_id,
            "pipelineVersion": self.pipeline_version,
            "promptHash": self.prompt_hash,
            "inputChars": self.input_chars,
            "outputChars": self.output_chars,
            "modelElapsedMs": self.model_elapsed_ms,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "cacheWriteTokens": self.cache_write_tokens,
            "reasoningTokens": self.reasoning_tokens,
            "costTotal": self.cost_total,
            "reviewerStatus": self.reviewer_status,
            "reviewerNotes": self.reviewer_notes,
            "judgeStatus": self.judge_status,
            "judgeScore": self.judge_score,
            "judgeChecks": [dict(check) for check in self.judge_checks],
            "judgeNotes": self.judge_notes,
            "evaluatedAt": self.evaluated_at,
        }


def _judge_result(raw: Mapping[str, Any]) -> tuple[str, float | None, tuple[dict[str, object], ...], str]:
    """Validate the small adapter contract used by an external semantic judge.

    The evaluator does not call a provider itself. A caller may inject a judge
    backed by an LLM or a human review service, but its output is normalized so
    malformed or out-of-range scores cannot silently turn a failed case into a
    pass.
    """
    status = _text(raw.get("status") or raw.get("decision")).strip().lower()
    if status not in {"pass", "fail", "error"}:
        raise ValueError("judge status must be pass, fail, or error")
    score_value = raw.get("score")
    score: float | None = None
    if score_value is not None and score_value != "":
        score = float(score_value)
        if not isfinite(score) or not 0 <= score <= 1:
            raise ValueError("judge score must be between 0 and 1")
    checks: list[dict[str, object]] = []
    raw_checks = raw.get("checks") or ()
    if not isinstance(raw_checks, (list, tuple)):
        raise ValueError("judge checks must be a list")
    for index, value in enumerate(raw_checks):
        if not isinstance(value, Mapping):
            raise ValueError(f"judge check {index} must be an object")
        passed_value = value.get("passed")
        if not isinstance(passed_value, bool):
            raise ValueError(f"judge check {index} passed must be boolean")
        name = _text(value.get("name") or f"judge_check_{index}")[:120]
        detail = _text(value.get("detail"))[:1000]
        checks.append({"name": name, "passed": passed_value, "detail": detail})
    notes = _text(raw.get("notes") or raw.get("summary"))[:2000]
    return status, score, tuple(checks), notes


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _optional_nonnegative_float(value: object) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) and number >= 0 else None


def evaluate_preparation(
    case: MailQualityCase,
    preparation: Mapping[str, Any],
    *,
    judge: QualityJudge | None = None,
) -> MailQualityResult:
    """Evaluate one parsed preparation result without treating metadata as proof.

    Source lists, confidence, and prompt hashes are recorded for attribution;
    only the explicit case expectations determine the automated checks.
    """
    actual_decision = _text(preparation.get("decision"))
    body = _text(preparation.get("body"))
    questions = tuple(_text(value) for value in (preparation.get("questions") or ()) if _text(value))
    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    check("decision", actual_decision == case.expected_decision,
          f"expected={case.expected_decision!r}; actual={actual_decision!r}")
    for required in case.required:
        check("required_text", required in body, f"required={required!r}")
    for forbidden in case.forbidden:
        check("forbidden_text", forbidden not in body, f"forbidden={forbidden!r}")
    for expected_question in case.expected_questions:
        check("question", any(expected_question in question for question in questions),
              f"expected={expected_question!r}")
    if actual_decision == "draft":
        check("draft_contract", bool(body) and not questions, "draft requires body and no questions")
    elif actual_decision == "needs_input":
        check("needs_input_contract", bool(questions) and not body, "needs_input requires questions and no body")
    elif actual_decision == "no_reply":
        check("no_reply_contract", not body and not questions, "no_reply requires empty body and questions")

    judge_status = "not_run"
    judge_score: float | None = None
    judge_checks: tuple[dict[str, object], ...] = ()
    judge_notes = ""
    if judge is not None:
        try:
            raw_judgement = judge(case, preparation)
            if not isinstance(raw_judgement, Mapping):
                raise ValueError("judge result must be an object")
            judge_status, judge_score, judge_checks, judge_notes = _judge_result(raw_judgement)
            check("semantic_judge", judge_status == "pass" and all(bool(value["passed"]) for value in judge_checks),
                  f"status={judge_status}; checks={len(judge_checks)}")
        except Exception as error:
            judge_status = "error"
            judge_notes = _text(error)[:2000]
            check("semantic_judge", False, judge_notes)

    metadata = preparation.get("contextMetadata")
    prompt = metadata.get("prompt") if isinstance(metadata, Mapping) else {}
    if not isinstance(prompt, Mapping):
        prompt = {}
    usage = preparation.get("modelUsage")
    if not isinstance(usage, Mapping):
        usage = prompt.get("usage") if isinstance(prompt.get("usage"), Mapping) else {}
    cost = usage.get("cost") if isinstance(usage.get("cost"), Mapping) else {}
    return MailQualityResult(
        case_id=case.case_id,
        expected_decision=case.expected_decision,
        actual_decision=actual_decision,
        passed=all(bool(check["passed"]) for check in checks),
        checks=tuple(checks),
        provider=_text(preparation.get("provider")),
        model=_text(preparation.get("model")),
        model_configuration_id=_text(preparation.get("modelConfigurationId")),
        pipeline_version=(int(preparation["pipelineVersion"]) if preparation.get("pipelineVersion") is not None else None),
        prompt_hash=_text(preparation.get("promptHash") or prompt.get("hash")),
        input_chars=int(prompt.get("inputChars") or 0),
        output_chars=int(prompt.get("outputChars") or 0),
        model_elapsed_ms=int(prompt.get("modelElapsedMs") or 0),
        input_tokens=_optional_nonnegative_int(usage.get("inputTokens", usage.get("input_tokens"))),
        output_tokens=_optional_nonnegative_int(usage.get("outputTokens", usage.get("output_tokens"))),
        cache_read_tokens=_optional_nonnegative_int(usage.get("cacheReadTokens", usage.get("cache_read_tokens"))),
        cache_write_tokens=_optional_nonnegative_int(usage.get("cacheWriteTokens", usage.get("cache_write_tokens"))),
        reasoning_tokens=_optional_nonnegative_int(usage.get("reasoningTokens", usage.get("reasoning_tokens"))),
        cost_total=_optional_nonnegative_float(cost.get("total", cost.get("total_cost"))),
        reviewer_status=case.reviewer_status,
        reviewer_notes=case.reviewer_notes,
        judge_status=judge_status,
        judge_score=judge_score,
        judge_checks=judge_checks,
        judge_notes=judge_notes,
    )


def summarize_quality_results(results: list[MailQualityResult]) -> dict[str, object]:
    """Build a compact report summary suitable for JSON or Markdown output."""
    passed = sum(1 for result in results if result.passed)
    usage_by_model: dict[tuple[str, str], dict[str, object]] = {}
    for result in results:
        key = (result.provider, result.model)
        bucket = usage_by_model.setdefault(key, {"provider": result.provider, "model": result.model, "calls": 0})
        bucket["calls"] = int(bucket["calls"]) + 1
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "reviewPending": sum(1 for result in results if result.reviewer_status == "pending"),
        "judgePending": sum(1 for result in results if result.judge_status == "not_run"),
        "judgeFailed": sum(1 for result in results if result.judge_status in {"fail", "error"}),
        # Token/cost accounting is provider-specific and is not inferred from
        # characters.  Keep only measurements the pipeline actually records;
        # callers may add provider usage when their model client exposes it.
        "usage": {
            "calls": len(results),
            "inputChars": sum(result.input_chars for result in results),
            "outputChars": sum(result.output_chars for result in results),
            "modelElapsedMs": sum(result.model_elapsed_ms for result in results),
            "inputTokens": sum(result.input_tokens or 0 for result in results) if any(result.input_tokens is not None for result in results) else None,
            "outputTokens": sum(result.output_tokens or 0 for result in results) if any(result.output_tokens is not None for result in results) else None,
            "cacheReadTokens": sum(result.cache_read_tokens or 0 for result in results) if any(result.cache_read_tokens is not None for result in results) else None,
            "cacheWriteTokens": sum(result.cache_write_tokens or 0 for result in results) if any(result.cache_write_tokens is not None for result in results) else None,
            "reasoningTokens": sum(result.reasoning_tokens or 0 for result in results) if any(result.reasoning_tokens is not None for result in results) else None,
            "costTotal": sum(result.cost_total or 0 for result in results) if any(result.cost_total is not None for result in results) else None,
            "reportedTokenCalls": sum(1 for result in results if any(value is not None for value in (result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_write_tokens, result.reasoning_tokens))),
            "reportedCostCalls": sum(1 for result in results if result.cost_total is not None),
            "models": list(usage_by_model.values()),
        },
        "results": [result.as_dict() for result in results],
    }
