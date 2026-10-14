"""Host-owned control commands shared by every chat transport."""
from __future__ import annotations

import re


def pairing_code(content: str) -> str | None:
    match = re.fullmatch(r"\s*/pair(?:@[A-Za-z0-9_]+)?(?:\s+|=)([A-Za-z0-9-]{6,32})\s*", content, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    if re.fullmatch(r"MEIDO-[A-F0-9]{8}", content.strip(), re.IGNORECASE):
        return content.strip().upper()
    return None


def control_command(content: str) -> str | None:
    match = re.fullmatch(r"\s*/(stop|chatid|myid)(?:@[A-Za-z0-9_]+)?\s*", content, re.IGNORECASE)
    return match.group(1).lower() if match else None


def identifier_reply(command: str, *, channel: str, account_id: str, chat_id: str, sender_id: str) -> str:
    if command == "myid":
        return f"用户 ID：{sender_id}"
    return f"渠道：{channel}\n账号：{account_id}\n会话 ID：{chat_id}\n用户 ID：{sender_id}"
