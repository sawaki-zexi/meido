from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .memory_optimizer import MemoryOptimizer, MemoryRecord
from .models import MemoryItem


class MemoryDocuments:
    """Safe role-scoped access to the Markdown memory views and journal."""

    DOCUMENTS = frozenset({"SELF.md", "MEMORY.md", "HISTORY.md", "PENDING.md", "RECENT_CONTEXT.md"})

    def __init__(self, roles_root: str | Path) -> None:
        self.roles_root = Path(roles_root).resolve()

    def memory_dir(self, role_id: str) -> Path:
        if not role_id or role_id in {".", ".."} or re.search(r"[/\\]", role_id):
            raise ValueError("角色路径无效")
        role_root = (self.roles_root / role_id).resolve()
        if role_root.parent != self.roles_root:
            raise ValueError("角色路径无效")
        memory_dir = (role_root / "memory").resolve()
        if memory_dir.parent != role_root:
            raise ValueError("记忆文档路径无效")
        return memory_dir

    def read_document(self, role_id: str, name: str) -> str:
        if name not in self.DOCUMENTS:
            raise ValueError("不支持的记忆文档")
        memory_dir = self.memory_dir(role_id)
        path = memory_dir / name
        if path.exists() and path.resolve().parent != memory_dir:
            raise ValueError("记忆文档路径无效")
        try:
            return path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as error:
            raise RuntimeError(f"读取 {name} 失败：{error}") from error

    def read_journal(self, role_id: str) -> list[dict[str, str]]:
        journal = self.memory_dir(role_id) / "journal"
        if not journal.exists():
            return []
        try:
            entries = []
            for path in sorted(journal.glob("*.md")):
                if path.is_file() and path.resolve().parent == journal.resolve():
                    entries.append({"date": path.stem, "content": path.read_text(encoding="utf-8")})
            return entries
        except OSError as error:
            raise RuntimeError(f"读取整理日志失败：{error}") from error

    def journal_path(self, role_id: str, date: str | None = None) -> Path:
        day = date or datetime.now(timezone.utc).date().isoformat()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("整理日志日期无效")
        return self.memory_dir(role_id) / "journal" / f"{day}.md"

    @staticmethod
    def render_journal_entry(source_key: str, start: int, end: int) -> str:
        return f"\n## 消息 {start}–{end}\n\n<!-- source: {source_key} -->\n整理了消息 {start}–{end}。\n"

    def sync_structured_memory(self, role_id: str, memories: list[MemoryItem]) -> Path:
        memory_dir = self.memory_dir(role_id)
        memory_dir.mkdir(parents=True, exist_ok=True)
        path = memory_dir / "MEMORY.md"
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        pending_path = memory_dir / "PENDING.md"
        pending_original = pending_path.read_text(encoding="utf-8") if pending_path.exists() else ""
        records = [
            MemoryRecord(
                memory_type=item.memoryType,
                summary=item.summary,
                sources=list(dict.fromkeys(item.sourceRef.sourceKeys or [item.sourceRef.stableSourceKey])),
            )
            for item in memories if item.status == "active"
        ]
        updated = MemoryOptimizer.render_managed_memory(original, records)
        excluded = [item for item in memories if item.status in {"forgotten", "rejected"}]
        pending_updated = self._remove_rejected_candidates(pending_original, excluded)
        writes = {path: updated}
        if pending_updated != pending_original:
            writes[pending_path] = pending_updated
        MemoryOptimizer.commit_documents(writes)
        return path

    @staticmethod
    def _remove_rejected_candidates(text: str, excluded: list[MemoryItem]) -> str:
        if not text or not excluded:
            return text
        sources = {
            source
            for item in excluded
            for source in (item.sourceRef.sourceKeys or [item.sourceRef.stableSourceKey])
        }
        summaries = {" ".join(item.summary.casefold().split()) for item in excluded}
        lines = text.splitlines(keepends=True)
        kept: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.lstrip().startswith("- [") and index + 1 < len(lines):
                source_line = lines[index + 1].strip()
                match = re.fullmatch(r"<!-- source: ([^>]+) -->", source_line)
                if match:
                    candidate_sources = match.group(1).split(",")
                    candidate = re.match(r"- \[[^\]]+\] (.+?)(?:[（(]来源：.*[）)])?\s*$", line.strip())
                    candidate_summary = " ".join(candidate.group(1).casefold().split()) if candidate else ""
                    source_match = any(source == key or source.startswith(key + ":") for source in candidate_sources for key in sources)
                    summary_match = candidate_summary in summaries
                    if source_match or summary_match:
                        index += 2
                        continue
            kept.append(line)
            index += 1
        return "".join(kept)
