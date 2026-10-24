"""Validated reply decisions; model output never supplies source links."""
import json
import re
from urllib.parse import urlparse


def parse_reply(raw: str) -> dict[str, object]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].removesuffix("```").strip()
    try:
        value = json.loads(text)
    except ValueError as error:
        raise RuntimeError("模型未返回有效邮件判断结果") from error
    if not isinstance(value, dict):
        raise RuntimeError("模型返回的邮件判断结果格式无效")
    decision = value.get("decision")
    if decision is None and isinstance(value.get("needsReply"), bool):
        decision = "draft" if value["needsReply"] else "no_reply"
    body, reason, questions = value.get("body", ""), value.get("reason", ""), value.get("questions", [])
    confidence = value.get("confidence", "unknown")
    if (decision not in {"draft", "no_reply", "needs_input"} or not isinstance(body, str)
            or not isinstance(reason, str) or not isinstance(questions, list)
            or not isinstance(confidence, str) or confidence not in {"low", "medium", "high", "unknown"}
            or len(questions) > 5 or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in questions)):
        raise RuntimeError("模型返回的邮件判断结果格式无效")
    body = normalize_reply_text(body)
    if ((decision == "draft" and (not body or questions))
            or (decision == "needs_input" and (not questions or body.strip()))
            or (decision == "no_reply" and (questions or body.strip()))
            or ("needsReply" in value and value["needsReply"] != (decision != "no_reply"))):
        raise RuntimeError("模型返回的邮件判断与正文或问题不一致")
    return {"decision": decision, "body": body[:12000], "reason": reason.strip()[:1000],
            "questions": [item.strip() for item in questions], "confidence": confidence}


def normalize_reply_text(value: str) -> str:
    """Normalize common model newline artifacts without rewriting the reply's wording."""
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
    if re.search(r"\\(?:r\\n|n|r)", text):
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if re.search(r"([^\s\-=_*.#~])\1{49,}", text):
        raise RuntimeError("模型生成的邮件正文包含异常重复内容")
    if "\n" not in text:
        boundaries = list(re.finditer(r"([a-z0-9][.!?])(?=[A-Z])", text))
        if len(boundaries) >= 2:
            text = re.sub(r"([a-z0-9][.!?])(?=[A-Z])", r"\1\n\n", text)
    return text


def safe_source_url(value: object) -> str:
    url = str(value or "")[:2000]
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password else ""
