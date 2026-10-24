from __future__ import annotations

import re
from dataclasses import dataclass


# Increment when prompt construction, retrieval or post-processing changes.
MAIL_DRAFT_PIPELINE_VERSION = 5


@dataclass(frozen=True, slots=True)
class PreparedModelText:
    text: str
    source_chars: int
    provided_chars: int
    truncated: bool
    quoted_removed: bool


_QUOTE_START = re.compile(
    r"^(?:on .+ wrote:\s*|在 .+ 写道：\s*|-{2,}\s*original message\s*-{2,}|begin forwarded message:|原始邮件(?:\s*[-:：])?)$",
    re.IGNORECASE,
)
_QUOTE_SENDER = re.compile(r"^(?:from|发件人)\s*[:：]", re.IGNORECASE)
_QUOTE_HEADER_FIELD = re.compile(r"^(?:sent|date|to|cc|subject|发件时间|日期|收件人|抄送|主题)\s*[:：]", re.IGNORECASE)


def _starts_quote(lines: list[str], index: int) -> bool:
    stripped = lines[index].strip()
    if stripped.startswith(">") or _QUOTE_START.match(stripped):
        return True
    if not _QUOTE_SENDER.match(stripped):
        return False
    fields = sum(bool(_QUOTE_HEADER_FIELD.match(line.strip())) for line in lines[index + 1:index + 7])
    return fields >= 2


def prepare_model_text(value: str, limit: int, *, strip_quotes: bool = False) -> PreparedModelText:
    original = str(value or "")
    text = original.replace("\r\n", "\n").replace("\r", "\n")
    quoted_removed = False
    if strip_quotes:
        kept: list[str] = []
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if _starts_quote(lines, index):
                quoted_removed = True
                break
            kept.append(line)
        text = "\n".join(kept)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    provided = text[:max(0, limit)].rstrip()
    return PreparedModelText(
        text=provided,
        source_chars=len(original),
        provided_chars=len(provided),
        truncated=len(text) > len(provided),
        quoted_removed=quoted_removed,
    )
