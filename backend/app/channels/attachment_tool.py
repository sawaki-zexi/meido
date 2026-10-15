"""QQ text attachment reader with a host-owned current-turn allowlist."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition


class ReadChannelAttachmentTool:
    definition = ToolDefinition(
        "read_attachment", "分段读取当前 QQ 消息及引用消息的 TXT/MD 附件。只能使用本回合列出的附件 ID。",
        {"type": "object", "properties": {
            "attachment_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 400},
            "character_offset": {"type": "integer", "minimum": 0},
        }, "required": ["attachment_id"], "additionalProperties": False},
        external_allowed=True,
    )

    def __init__(self, role_id: str, session_key: str, attachments: list[dict[str, object]]) -> None:
        self.role_id, self.session_key = role_id, session_key
        self.attachments = {str(item["id"]): dict(item) for item in attachments if item.get("id")}

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        if context.role_id != self.role_id or context.session_key != self.session_key:
            return AgentToolResult("附件不属于当前 QQ 回合。", is_error=True)
        item = self.attachments.get(str(arguments.get("attachment_id") or ""))
        if item is None:
            return AgentToolResult("附件不属于当前 QQ 回合。", is_error=True)
        offset, limit, character_offset = arguments.get("offset", 0), arguments.get("limit", 50), arguments.get("character_offset")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (offset, limit)) or offset < 0 or not 1 <= limit <= 400:
            return AgentToolResult("offset 必须非负，limit 必须在 1 到 400 之间。", is_error=True)
        if character_offset is not None and (isinstance(character_offset, bool) or not isinstance(character_offset, int) or character_offset < 0 or offset or limit != 50):
            return AgentToolResult("character_offset 必须非负，且不能与 offset/limit 同用。", is_error=True)
        if Path(str(item.get("name") or "")).suffix.lower() not in {".txt", ".md"}:
            return AgentToolResult("附件读取仅支持 TXT/MD。", is_error=True)
        try:
            with Path(str(item["path"])).open("rb") as stream:
                data = stream.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024 or b"\0" in data:
                raise ValueError("附件超过 2 MiB 或不是文本")
            text = data.decode("utf-8-sig")
        except (OSError, ValueError, KeyError) as error:
            return AgentToolResult(f"附件无法读取：{error}", is_error=True)
        if character_offset is not None or any(len(line) > 8000 for line in text.splitlines()):
            if offset:
                return AgentToolResult("此附件含超长行，请使用 character_offset。", is_error=True)
            start = character_offset or 0
            end = min(start + 2000, len(text))
            return AgentToolResult(text[start:end] + f"\n[字符 {start}–{end}/{len(text)}；character_offset={end}]")
        lines = text.splitlines()
        selected = "\n".join(lines[offset:offset + limit])
        if len(selected.encode("utf-8")) > 10_000:
            return AgentToolResult("此页超过 10 KB，请减少 limit 或使用 character_offset。", is_error=True)
        return AgentToolResult(selected + f"\n[行 {offset}–{min(offset + limit, len(lines))}/{len(lines)}]")
