from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_QUERY_KEYS = frozenset({
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "code",
    "cookie",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
})
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|password|passwd|secret|authorization|cookie|set-cookie)\b\s*[:=]\s*)([\"']?)([^\s,;\"']+)"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)([^\s,;]+)")
_COOKIE_HEADER = re.compile(r"(?i)(\b(?:cookie|set-cookie)\s*:\s*)([^\r\n]+)")

def is_browser_tool(name: str) -> bool:
    return name.startswith("agent_browser_")


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.query:
        return value
    query = [
        (key, "[redacted]") if key.casefold().replace("-", "_") in _SENSITIVE_QUERY_KEYS else (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def sanitize_browser_arguments(name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    """Return a diagnostic/provider-safe copy of browser tool arguments."""

    safe = deepcopy(dict(arguments))
    if not is_browser_tool(name):
        return safe
    url = safe.get("url")
    if isinstance(url, str):
        safe["url"] = _redact_url(url)
    if name in {"agent_browser_fill", "agent_browser_type", "agent_browser_select"}:
        for key in ("text", "value"):
            if key in safe:
                safe[key] = "[redacted browser input]"
    if name == "agent_browser_eval" and "script" in safe:
        safe["script"] = "[redacted browser script]"
    return safe


def sanitize_browser_text(value: str) -> tuple[str, int]:
    """Remove common credential and cookie representations from browser output."""

    if not value:
        return value, 0
    redactions = 0

    def replace_assignment(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}{match.group(2)}[redacted]"

    def replace_bearer(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}[redacted]"

    def replace_cookie_header(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}[redacted]"

    safe = _COOKIE_HEADER.sub(replace_cookie_header, value)
    safe = _BEARER.sub(replace_bearer, safe)
    safe = _SENSITIVE_ASSIGNMENT.sub(replace_assignment, safe)
    return safe, redactions
