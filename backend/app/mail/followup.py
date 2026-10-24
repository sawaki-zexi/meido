"""Thread-level follow-up classification.

The classifier deliberately keeps the durable state small and explainable. It
uses message direction, processing status, and explicit language signals; a
future model-assisted classifier can replace the signal extraction without
changing the stored follow-up contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .context import prepare_model_text
from .date import mail_date_key
from .models import MailItem, ThreadFollowupState


FOLLOWUP_CLASSIFIER_VERSION = "signals-v3"

_QUESTION_RE = re.compile(
    r"(?:\?|？|吗[？?]?$|么[？?]?$|能否|是否|请确认|请问|可否|需要你|请你|"
    r"请(?:回复|告知|确认|反馈|查看|处理|提供|发送|提交|安排)|"
    r"please (?:reply|respond|get back|confirm|review|check|provide|send|submit|let me know)|"
    r"looking forward to (?:hearing|your reply)|let me know|keep me posted|"
    r"could you|would you|can you|do you)",
    re.IGNORECASE,
)
_COMMITMENT_RE = re.compile(
    r"(?:我(?:会|将|要|能|可以)|我们(?:会|将)|稍后|之后|尽快|明天|后天|下周|本周|周[一二三四五六日天]|\b(?:i(?:'ll| will)|we(?:'ll| will)|i can|i plan to|will)\b|\bby (?:tomorrow|friday|next week)\b)",
    re.IGNORECASE,
)
_CLOSURE_RE = re.compile(
    r"(?:谢谢|感谢|收到|(?:已|已经)解决|解决了|事情(?:已经)?办完|不用回复|无需回复|无须回复|没有其他问题|到此为止|辛苦了|祝好|thanks|thank you|all set|resolved|no further action|no need to reply|best regards)",
    re.IGNORECASE,
)
_FULFILLED_RE = re.compile(
    r"(?:已提交|已经提交|已发送|已经发送|已完成|已经完成|已办妥|已经办妥|已处理|已经处理|已解决|已经解决|done|submitted|sent|completed|resolved)",
    re.IGNORECASE,
)

_OWNER_PENDING_STATUSES = {"new", "queued", "processing", "failed", "uncertain", "needs_input", "drafted"}
_QUESTION_SPLIT_RE = re.compile(r"(?:\r?\n+|[。！？!?；;]+|，\s*(?:并|以及|同时|另外)|,\s*(?:and|also|plus)\b)")
_TERM_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")
_TOPIC_STOPWORDS = {
    "请", "请你", "请问", "是否", "能否", "可以", "能够", "需要", "需要你", "告诉", "确认", "一下",
    "我", "你", "他", "她", "我们", "他们", "您好", "你好", "谢谢", "收到", "please", "could", "would",
    "you", "can", "the", "and", "for", "with", "about", "that", "this", "是否能", "吗", "呢",
}


@dataclass(frozen=True, slots=True)
class FollowupAnalysis:
    state: ThreadFollowupState
    reason: str
    next_action: str
    open_questions: tuple[dict[str, object], ...]
    commitments: tuple[dict[str, object], ...]
    evidence_event_ids: tuple[str, ...]
    classifier: str = FOLLOWUP_CLASSIFIER_VERSION

    def as_dict(self) -> dict[str, object]:
        active_commitments = [item for item in self.commitments if item.get("status") == "open"]
        due_values = [str(item["dueAt"]) for item in active_commitments if item.get("dueAt")]
        next_due_at = min(due_values) if due_values else None
        return {
            "classifier": self.classifier,
            "nextAction": self.next_action,
            "nextDueAt": next_due_at,
            "openQuestions": [dict(item) for item in self.open_questions],
            "commitments": [dict(item) for item in self.commitments],
            "evidenceEventIds": list(self.evidence_event_ids),
        }


def _text(item: MailItem) -> str:
    subject = str(item.subject or "").strip()
    body = prepare_model_text(item.body_text, 12000, strip_quotes=True).text
    return "\n".join(value for value in (subject, body) if value).strip()


def _snippet(item: MailItem) -> str:
    compact = re.sub(r"\s+", " ", _text(item)).strip()
    return compact[:240]


def _has_question(item: MailItem) -> bool:
    return bool(_QUESTION_RE.search(_text(item)))


def _semantic_text(item: MailItem) -> str:
    body = prepare_model_text(item.body_text, 12000, strip_quotes=True).text.strip()
    return body or str(item.subject or "").strip()


def _question_units(item: MailItem) -> list[str]:
    """Split a message into independently answerable request fragments."""
    text = _semantic_text(item)
    if not text:
        return []
    units = [re.sub(r"\s+", " ", value).strip(" \t,，") for value in _QUESTION_SPLIT_RE.split(text)]
    units = [value for value in units if value]
    if len(units) <= 1:
        if _has_question(item):
            return units
        if item.status in _OWNER_PENDING_STATUSES and not _is_closing(item) and not _FULFILLED_RE.search(text):
            return units
        return []
    selected = [
        value for value in units
        if _QUESTION_RE.search(value)
        or re.search(r"(?:请|請|需要你|需要您|请你|請你|please|could you|would you|can you)", value, re.IGNORECASE)
    ]
    return selected or (
        units
        if item.status in _OWNER_PENDING_STATUSES and not _is_closing(item) and not _FULFILLED_RE.search(text)
        else []
    )


def _topic_terms(value: str) -> set[str]:
    terms: set[str] = set()
    for token in _TERM_RE.findall(value.casefold()):
        if token in _TOPIC_STOPWORDS or len(token) < 2:
            continue
        terms.add(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 2:
            terms.update(
                pair for index in range(len(token) - 1)
                for pair in (token[index:index + 2],)
                if pair not in _TOPIC_STOPWORDS
            )
    return terms


def _matches_topic(topic: str, item: MailItem) -> bool:
    topic_terms = _topic_terms(topic)
    return bool(topic_terms and topic_terms & _topic_terms(_semantic_text(item)))


def _has_commitment(item: MailItem) -> bool:
    text = _text(item)
    if re.search(r"(?:不会|不會|不能|无法|無法|won['’]?t|will not|cannot|can['’]?t)", text, re.IGNORECASE):
        return False
    # Requests containing a date are not commitments unless the sender also
    # takes ownership of the action in the same message.
    if re.search(r"(?:请|請|please|could you|would you|can you|需要你)", text, re.IGNORECASE) and not re.search(
        r"(?:我|我们|我們|i\b|we\b)", text, re.IGNORECASE,
    ):
        return False
    return bool(_COMMITMENT_RE.search(text))


def _is_closing(item: MailItem) -> bool:
    return bool(_CLOSURE_RE.search(_text(item)))


def _is_global_closing(item: MailItem) -> bool:
    """Return true only for a message that closes the whole thread."""
    if not _is_closing(item) or _has_question(item) or _has_commitment(item):
        return False
    value = _semantic_text(item)
    residual = _CLOSURE_RE.sub("", value)
    residual = re.sub(r"[\s,，。.!！?？;；:：、]+", "", residual)
    return not residual


def _base_datetime(value: str) -> datetime | None:
    parsed = _base_datetime_with_zone(value)
    return parsed.astimezone(timezone.utc) if parsed is not None else None


def _base_datetime_with_zone(value: str) -> datetime | None:
    """Parse a mail timestamp while preserving the provider's offset."""
    try:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, OverflowError):
        return None


def _deadline_end_of_day(value: datetime) -> str:
    return value.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(timezone.utc).isoformat()


def _due_at(item: MailItem) -> str | None:
    """Extract only explicit, date-like deadlines from a commitment signal.

    The result is deliberately date-only precision. A missing or ambiguous date
    remains unknown rather than being invented from the current clock.
    """
    text = _text(item)
    base = _base_datetime_with_zone(item.received_at)
    if base is None:
        return None
    explicit_datetime = re.search(
        r"(?<!\d)(?P<year>20\d{2})[-年](?P<month>\d{1,2})[-月](?P<day>\d{1,2})日?"
        r"(?:[ T](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
        r"(?P<zone>Z|[+-]\d{2}:?\d{2})?)?",
        text,
        re.IGNORECASE,
    )
    explicit = explicit_datetime
    if explicit:
        try:
            zone_text = explicit.group("zone")
            zone = None
            if zone_text:
                zone = timezone.utc if zone_text.upper() == "Z" else timezone(
                    timedelta(hours=int(zone_text[-5:-3]), minutes=int(zone_text[-2:]))
                    * (-1 if zone_text[0] == "-" else 1),
                )
            zone = zone or base.tzinfo or timezone.utc
            value = datetime(
                int(explicit.group("year")), int(explicit.group("month")), int(explicit.group("day")),
                int(explicit.group("hour") or 0), int(explicit.group("minute") or 0),
                int(explicit.group("second") or 0), tzinfo=zone,
            )
            return value.astimezone(timezone.utc).isoformat() if explicit.group("hour") else _deadline_end_of_day(value)
        except ValueError:
            return None
    relative = re.search(r"(?:明天|后天|后日|tomorrow|the day after tomorrow)", text, re.IGNORECASE)
    if relative:
        days = 2 if relative.group(0).casefold() in {"后天", "后日", "the day after tomorrow"} else 1
        return _deadline_end_of_day(base + timedelta(days=days))
    weekday = re.search(
        r"(?P<prefix>本周|这周|下周|周|星期|this\s+week|next\s+week|this|next)?\s*"
        r"(?P<day>[一二三四五六日天]|[1-7]|monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
        text,
        re.IGNORECASE,
    )
    if weekday:
        token = weekday.group("day").casefold()
        names = {"一": 0, "1": 0, "二": 1, "2": 1, "三": 2, "3": 2, "四": 3, "4": 3,
                 "五": 4, "5": 4, "六": 5, "6": 5, "日": 6, "天": 6, "7": 6,
                 "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                 "friday": 4, "saturday": 5, "sunday": 6}
        target = names.get(token)
        if target is not None:
            prefix = (weekday.group("prefix") or "").casefold()
            if prefix in {"下周", "next week", "next"}:
                delta = (target - base.weekday()) % 7 + 7
            elif prefix in {"本周", "这周", "this week", "this"}:
                delta = target - base.weekday()
                if delta < 0:
                    delta += 7
            else:
                delta = (target - base.weekday()) % 7
                if delta == 0:
                    delta = 7
            return _deadline_end_of_day(base + timedelta(days=delta))
    if re.search(r"(?:下周|next week)", text, re.IGNORECASE):
        return (base + timedelta(days=7)).replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    return None


def _event(
    item: MailItem,
    *,
    owner: str,
    kind: str,
    status: str = "open",
    fulfilled_by: str | None = None,
    summary: str | None = None,
    question_id: str | None = None,
) -> dict[str, object]:
    due_at = _due_at(item) if kind.endswith("commitment") else None
    value: dict[str, object] = {
        "eventId": item.id,
        "itemId": item.id,
        "owner": owner,
        "kind": kind,
        "status": status,
        "summary": summary or _snippet(item),
        "at": item.received_at,
        **({"dueAt": due_at} if due_at else {}),
        **({"fulfilledByEventId": fulfilled_by} if fulfilled_by else {}),
    }
    topic = summary or _snippet(item)
    if topic:
        value["topic"] = topic
    if question_id:
        value["questionId"] = question_id
    return value


def _question_event(item: MailItem, *, owner: str, kind: str, summary: str, index: int) -> dict[str, object]:
    return _event(
        item,
        owner=owner,
        kind=kind,
        summary=summary,
        question_id=f"{item.id}:q:{index + 1}",
    )


def _remove_answered_questions(
    questions: list[dict[str, object]],
    answer: MailItem,
    *,
    owner: str,
) -> list[dict[str, object]]:
    remaining: list[dict[str, object]] = []
    closing = _is_global_closing(answer)
    for question in questions:
        if question.get("owner") != owner:
            remaining.append(question)
            continue
        topic = str(question.get("topic") or question.get("summary") or "")
        if closing or _matches_topic(topic, answer):
            continue
        remaining.append(question)
    return remaining


def classify_thread(events: list[MailItem]) -> FollowupAnalysis:
    """Classify a complete thread and retain evidence for the UI.

    The newest event controls the immediate action, while explicit questions
    and commitments in the thread explain why the action remains open. A
    commitment takes precedence over a closing phrase in the same message.
    """
    ordered = sorted(events, key=lambda item: mail_date_key(item.received_at, item.message_id, item.id))
    if not ordered:
        return FollowupAnalysis("unknown", "没有可用的线程事件", "verify_thread", (), (), ())

    open_questions: list[dict[str, object]] = []
    commitments: list[dict[str, object]] = []
    evidence: list[str] = []

    # Resolve questions by topic as the thread progresses. A reply can answer
    # one item while leaving another item open; the whole responsibility side
    # must not be cleared merely because a newer message exists.
    for item in ordered:
        text = _text(item)
        if not text:
            continue
        if item.direction == "outbound":
            if _has_commitment(item):
                commitments.append(_event(item, owner="owner", kind="owner_commitment"))
                evidence.append(item.id)
            open_questions = _remove_answered_questions(open_questions, item, owner="owner")
            units = _question_units(item)
            if units and _has_question(item):
                for question_index, unit in enumerate(units):
                    open_questions.append(_question_event(item, owner="counterparty", kind="question", summary=unit, index=question_index))
                evidence.append(item.id)
            continue

        if _has_commitment(item):
            commitments.append(_event(item, owner="counterparty", kind="counterparty_commitment"))
            evidence.append(item.id)
        open_questions = _remove_answered_questions(open_questions, item, owner="counterparty")
        units = _question_units(item)
        if item.status != "no_reply" and units:
            for question_index, unit in enumerate(units):
                open_questions.append(_question_event(item, owner="owner", kind="inbound_question", summary=unit, index=question_index))
            evidence.append(item.id)

    latest = ordered[-1]
    latest_commitment = _has_commitment(latest)
    latest_question = bool(_question_units(latest)) and _has_question(latest)
    latest_closing = _is_closing(latest)
    # A later message from the same side can explicitly close a commitment.
    # Keep the original evidence, but mark it so the UI can distinguish an
    # outstanding promise from one that was subsequently fulfilled.
    for index, commitment in enumerate(commitments):
        commitment_at = _base_datetime(str(commitment.get("at") or ""))
        commitment_owner = str(commitment.get("owner") or "")
        for later in ordered:
            later_at = _base_datetime(later.received_at)
            if commitment_at is None or later_at is None or later_at <= commitment_at:
                continue
            later_owner = "owner" if later.direction == "outbound" else "counterparty"
            topic = str(commitment.get("topic") or commitment.get("summary") or "")
            if later_owner == commitment_owner and _FULFILLED_RE.search(_text(later)) and _matches_topic(topic, later):
                commitments[index] = {
                    **commitment,
                    "status": "fulfilled",
                    "fulfilledByEventId": later.id,
                }
                break

    open_commitments = [item for item in commitments if item.get("status") == "open"]
    latest_commitment_event = open_commitments[-1] if open_commitments else None
    latest_commitment_owner = latest_commitment_event.get("owner") if latest_commitment_event else None
    latest_owner_commitment = next((item for item in reversed(open_commitments) if item.get("owner") == "owner"), None)
    latest_counterparty_commitment = next((item for item in reversed(open_commitments) if item.get("owner") == "counterparty"), None)
    latest_commitment = bool(latest_commitment_event and latest_commitment_event.get("eventId") == latest.id)

    if latest.direction == "outbound":
        if latest_commitment or (latest_owner_commitment and not latest_closing):
            state: ThreadFollowupState = "to_reply"
            reason = "主人有尚未完成的承诺"
            next_action = "owner_complete_commitment"
        elif latest_question:
            state = "awaiting_reply"
            reason = "主人在最新邮件中提出问题，等待对方回复"
            next_action = "wait_counterparty"
        elif any(question.get("owner") == "owner" for question in open_questions):
            state = "to_reply"
            reason = "线程仍有主人尚未回答的问题"
            next_action = "review_or_reply"
        elif latest_counterparty_commitment:
            state = "awaiting_reply"
            reason = "对方仍有尚未兑现的承诺，等待对方完成"
            next_action = "wait_counterparty_commitment"
        elif latest_closing and not open_questions:
            state = "closed"
            reason = "主人最新邮件明确结束当前事项"
            next_action = "none"
        else:
            # Informational outbound messages are actioned by default.  Only
            # an explicit question/request or an explicit commitment creates
            # a pending counterparty action, avoiding needless follow-ups.
            state = "closed"
            reason = "主人已发送信息，未发现需要等待对方回复的事项"
            next_action = "none"
    elif (latest_commitment and latest_commitment_owner == "counterparty" or latest_counterparty_commitment) and not latest_question:
        state = "awaiting_reply"
        reason = "对方有尚未兑现的承诺，等待对方完成"
        next_action = "wait_counterparty_commitment"
    elif _FULFILLED_RE.search(_text(latest)) and not open_questions and not open_commitments:
        state = "closed"
        reason = "最新来信确认事项已完成，且线程没有未解决事项"
        next_action = "none"
    elif latest.status == "no_reply" and not open_questions and not open_commitments:
        state = "closed"
        reason = "最新来信判断为无需回复，且线程没有未解决事项"
        next_action = "none"
    elif latest.direction == "inbound":
        state = "to_reply"
        reason = "最新来信仍需要主人处理或确认"
        next_action = "review_or_reply"
    else:
        state = "unknown"
        reason = "线程事件方向无法确认"
        next_action = "verify_thread"

    # Keep only evidence that explains the final state, in stable order.
    evidence = list(dict.fromkeys(evidence + [latest.id]))
    return FollowupAnalysis(
        state,
        reason,
        next_action,
        tuple(open_questions),
        tuple(commitments),
        tuple(evidence),
    )
