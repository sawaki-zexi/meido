"""Evaluate mail preparation results without writing remote drafts.

Replay mode consumes a JSON file containing ``preparation`` objects. Live mode
uses the configured Meido mail service and evaluates synchronized mail IDs by
calling the existing preparation/model pipeline only; it never calls
``append_draft`` or sends mail.

Examples:

    python scripts/evaluate_mail_quality.py --input report-cases.json
    python scripts/evaluate_mail_quality.py --live --input live-cases.json --output report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.mail.evaluation import MailQualityCase, MailQualityResult, evaluate_preparation, summarize_quality_results


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_head() -> str | None:
    """Return the evaluated checkout identity without making evaluation fail."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _read_cases(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("评估输入必须是 JSON 数组")
    cases = [item for item in value if isinstance(item, dict)]
    if len(cases) != len(value):
        raise ValueError("评估输入包含非对象条目")
    return cases


def _read_judgements(path: Path | None) -> dict[str, Mapping[str, Any]] | None:
    """Read independent semantic-review results keyed by case id.

    The judge may be a human review export or a separately run model. Keeping
    it as an input file means the mail evaluator never silently calls a second
    provider or mixes review credentials with the mailbox account.
    """
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, Mapping) and isinstance(value.get("judgements"), Mapping):
        value = value["judgements"]
    if not isinstance(value, Mapping):
        raise ValueError("语义评审文件必须是以用例 ID 为键的 JSON 对象")
    result: dict[str, Mapping[str, Any]] = {}
    for case_id, judgement in value.items():
        normalized_id = str(case_id).strip()
        if not normalized_id:
            raise ValueError("语义评审文件包含空用例 ID")
        if not isinstance(judgement, Mapping):
            raise ValueError(f"语义评审 {normalized_id!r} 必须是对象")
        result[normalized_id] = judgement
    return result


def _judge_for(case: MailQualityCase, judgements: dict[str, Mapping[str, Any]] | None):
    if judgements is None:
        return None

    def judge(_case: MailQualityCase, _preparation: Mapping[str, Any]) -> Mapping[str, Any]:
        value = judgements.get(case.case_id)
        if value is None:
            return {"status": "error", "notes": f"语义评审文件缺少用例：{case.case_id}"}
        return value

    return judge


async def _run_live(
    cases: list[dict[str, Any]],
    judgements: dict[str, Mapping[str, Any]] | None = None,
) -> list[MailQualityResult]:
    # Importing main is intentionally restricted to --live. Replay mode has no
    # credentials, model calls, or mailbox side effects.
    from backend.app.main import mail_service

    connection = mail_service.store.connection()
    if not connection.verified or not connection.username:
        raise RuntimeError("真实评估需要已验证的邮件账号")
    adapter = mail_service._adapter(connection)
    results = []
    try:
        for raw_case in cases:
            mail_id = str(raw_case.get("mailId") or "").strip()
            if not mail_id:
                raise ValueError(f"评估用例 {raw_case.get('id', '')!r} 缺少 mailId")
            item = mail_service.store.get_item(mail_id, account=connection.username)
            if item is None:
                raise ValueError(f"邮件不存在或不属于当前账号：{mail_id}")
            preparation = await mail_service._draft_content(item, adapter, connection=connection)
            case = MailQualityCase.from_mapping(raw_case)
            results.append(evaluate_preparation(case, preparation, judge=_judge_for(case, judgements)))
    finally:
        adapter.close()
    return results


def _run_replay(
    cases: list[dict[str, Any]],
    judgements: dict[str, Mapping[str, Any]] | None = None,
) -> list[MailQualityResult]:
    results: list[MailQualityResult] = []
    for raw_case in cases:
        preparation = raw_case.get("preparation")
        if not isinstance(preparation, dict):
            # The repository's deterministic regression fixture stores the
            # model response under ``model`` because the real service pipeline
            # is exercised by pytest.  Treat that response as a recorded
            # preparation here too, so the standalone evaluator can replay the
            # same cases without inventing a second fixture format.
            model = raw_case.get("model")
            if not isinstance(model, dict):
                raise ValueError(f"回放用例 {raw_case.get('id', '')!r} 缺少 preparation 或 model")
            preparation = dict(model)
            preparation.setdefault("provider", "fixture")
            preparation.setdefault("model", "fixture-replay")
        case = MailQualityCase.from_mapping(raw_case)
        results.append(evaluate_preparation(case, preparation, judge=_judge_for(case, judgements)))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="评估邮件判断/起草质量；不会写入 QQ 草稿箱")
    parser.add_argument("--input", required=True, type=Path, help="JSON 用例文件")
    parser.add_argument("--output", type=Path, help="JSON 报告输出路径；省略时输出到 stdout")
    parser.add_argument("--judge-report", type=Path, help="独立语义评审 JSON；键为用例 ID，值为 judge 结果")
    parser.add_argument("--live", action="store_true", help="调用当前配置的模型，仅准备结果，不写远端草稿")
    args = parser.parse_args()

    try:
        started_at = _now()
        cases = _read_cases(args.input)
        judgements = _read_judgements(args.judge_report)
        results = asyncio.run(_run_live(cases, judgements)) if args.live else _run_replay(cases, judgements)
        finished_at = _now()
        report = {
            "schemaVersion": 1,
            "evalName": args.input.stem,
            "mode": "live" if args.live else "replay",
            "startedAt": started_at,
            "finishedAt": finished_at,
            "gitHead": _git_head(),
            **summarize_quality_results(results),
        }
        content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        else:
            sys.stdout.write(content)
        return 0 if report["failed"] == 0 else 2
    except Exception as error:
        print(f"mail quality evaluation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
