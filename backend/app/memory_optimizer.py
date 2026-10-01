from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable


_CANDIDATE_LINE = re.compile(r"^- \[(?P<memory_type>[^\]]+)\] (?P<summary>.+?) \(来源：.*\)\s*$")
_MEMORY_LINE = re.compile(r"^- \[(?P<memory_type>[^\]]+)\] (?P<summary>.+?)\s*$")
_SOURCE_LINE = re.compile(r"^\s*<!-- source: (?P<source>[^>]+?) -->\s*$")
_MANAGED_START = "<!-- meido-optimizer:start -->"
_MANAGED_END = "<!-- meido-optimizer:end -->"


@dataclass
class MemoryRecord:
    memory_type: str
    summary: str
    sources: list[str]


@dataclass
class MemoryOptimizationResult:
    records: list[MemoryRecord]
    self_understanding: str


# Backwards-compatible aliases for callers that used the original internal
# names before these result types became part of the optimizer's public API.
_Record = MemoryRecord
_Optimization = MemoryOptimizationResult


class MemoryOptimizer:
    """Atomically fold pending Markdown candidates into role memory documents."""

    MAX_SNAPSHOT_RETRIES = 3

    def __init__(
        self,
        roles_root: str | Path,
        consolidate: Callable[[str, list[MemoryRecord], list[MemoryRecord], str, str], MemoryOptimizationResult] | None = None,
        persist_structured: Callable[[str, list[MemoryRecord]], None] | None = None,
    ) -> None:
        self.roles_root = Path(roles_root).resolve()
        self.consolidate = consolidate
        self.persist_structured = persist_structured

    def optimize(self, role_id: str) -> bool:
        role_root = (self.roles_root / role_id).resolve()
        if role_root.parent != self.roles_root:
            raise ValueError("角色路径无效")
        memory_dir = role_root / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        pending_path = memory_dir / "PENDING.md"
        memory_path = memory_dir / "MEMORY.md"
        self_path = memory_dir / "SELF.md"
        history_path = memory_dir / "HISTORY.md"
        state_path = memory_dir / ".optimizer.json"

        for _ in range(self.MAX_SNAPSHOT_RETRIES):
            pending_snapshot = self._read_text(pending_path)
            history_snapshot = self._read_text(history_path)
            state = self._read_state(state_path)
            history_hash = hashlib.sha256(history_snapshot.encode("utf-8")).hexdigest()
            candidates, ranges = self._parse_pending(pending_snapshot)
            if not candidates and state.get("historyHash") == history_hash:
                return False
            existing_records = self._parse_memory(self._read_text(memory_path))
            existing_self = self._read_text(self_path)
            optimized = (
                self.consolidate(role_id, existing_records, candidates, history_snapshot, existing_self)
                if self.consolidate is not None
                else MemoryOptimizationResult(self._merge_records(existing_records, candidates), self._fallback_self(candidates))
            )
            required_sources = {source for record in [*existing_records, *candidates] for source in record.sources}
            returned_sources = {source for record in optimized.records for source in record.sources}
            if not required_sources.issubset(returned_sources):
                raise ValueError("Optimizer 遗漏了已有记忆来源，已保留待处理内容")
            documents = self._build_documents(
                pending_snapshot,
                self._read_text(memory_path),
                existing_self,
                history_snapshot,
                optimized,
                ranges,
            )
            self._before_commit()
            if self._read_text(pending_path) != pending_snapshot or self._read_text(history_path) != history_snapshot:
                continue
            if self.persist_structured is not None:
                self.persist_structured(role_id, optimized.records)
                if self._read_text(pending_path) != pending_snapshot or self._read_text(history_path) != history_snapshot:
                    continue
            self._commit(
                {
                    memory_path: documents["memory"],
                    self_path: documents["self"],
                    pending_path: documents["pending"],
                    state_path: json.dumps({"historyHash": history_hash}, ensure_ascii=False, indent=2) + "\n",
                }
            )
            return True
        raise RuntimeError("记忆候选在归并期间持续变化，已保留待处理内容")

    def _before_commit(self) -> None:
        """Hook for deterministic snapshot-race tests."""

    def _build_documents(
        self,
        pending_text: str,
        memory_text: str,
        self_text: str,
        history_text: str,
        optimized: MemoryOptimizationResult,
        ranges: list[tuple[int, int]],
    ) -> dict[str, str]:
        return {
            "memory": self._render_memory(memory_text, optimized.records),
            "self": self._render_self(self_text, optimized.records, history_text, optimized.self_understanding),
            "pending": self._remove_ranges(pending_text, ranges),
        }

    @staticmethod
    def _fallback_self(candidates: list[MemoryRecord]) -> str:
        return "；".join(record.summary for record in candidates[:8])

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @staticmethod
    def _read_state(path: Path) -> dict[str, str]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) and isinstance(value.get("historyHash"), str) else {}

    @classmethod
    def _parse_pending(cls, text: str) -> tuple[list[MemoryRecord], list[tuple[int, int]]]:
        lines = text.splitlines(keepends=True)
        records: list[MemoryRecord] = []
        ranges: list[tuple[int, int]] = []
        index = 0
        while index < len(lines):
            match = _CANDIDATE_LINE.match(lines[index].rstrip("\r\n"))
            if match is None:
                index += 1
                continue
            end = index + 1
            source = ""
            if end < len(lines):
                source_match = _SOURCE_LINE.match(lines[end].rstrip("\r\n"))
                if source_match is not None:
                    source = source_match.group("source").strip()
                    end += 1
            records.append(MemoryRecord(match.group("memory_type").strip(), match.group("summary").strip(), [source] if source else []))
            ranges.append((index, end))
            index = end
        return records, ranges

    @staticmethod
    def _parse_memory(text: str) -> list[MemoryRecord]:
        lines = text.splitlines()
        records: list[MemoryRecord] = []
        for index, line in enumerate(lines):
            match = _MEMORY_LINE.match(line)
            if match is None:
                continue
            sources: list[str] = []
            if index + 1 < len(lines):
                source_match = _SOURCE_LINE.match(lines[index + 1])
                if source_match is not None:
                    sources.extend(source.strip() for source in source_match.group("source").split(",") if source.strip())
            records.append(MemoryRecord(match.group("memory_type").strip(), match.group("summary").strip(), sources))
        return records

    @staticmethod
    def _canonical(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold())

    @classmethod
    def _same_record(cls, left: MemoryRecord, right: MemoryRecord) -> bool:
        if left.memory_type != right.memory_type:
            return False
        left_key = cls._canonical(left.summary)
        right_key = cls._canonical(right.summary)
        if left_key == right_key:
            return True
        return difflib.SequenceMatcher(None, left_key, right_key).ratio() >= 0.88

    @classmethod
    def _merge_records(cls, existing: list[MemoryRecord], candidates: list[MemoryRecord]) -> list[MemoryRecord]:
        merged = [MemoryRecord(item.memory_type, item.summary, list(item.sources)) for item in existing]
        for candidate in candidates:
            match = next((item for item in merged if cls._same_record(item, candidate)), None)
            if match is None:
                merged.append(MemoryRecord(candidate.memory_type, candidate.summary, list(candidate.sources)))
                continue
            for source in candidate.sources:
                if source and source not in match.sources:
                    match.sources.append(source)
            if len(candidate.summary) > len(match.summary) and cls._canonical(candidate.summary) != cls._canonical(match.summary):
                match.summary = candidate.summary
        return merged

    @classmethod
    def _render_memory(cls, original: str, records: list[MemoryRecord]) -> str:
        prefix, suffix = cls._managed_parts(original)
        if not prefix.strip():
            prefix = "# 长期记忆"
        lines = [_MANAGED_START, "## 自动归并的记忆"]
        for record in records:
            lines.append(f"- [{record.memory_type}] {record.summary}")
            if record.sources:
                lines.append(f"  <!-- source: {','.join(record.sources)} -->")
        lines.append(_MANAGED_END)
        result = prefix.rstrip() + "\n\n" + "\n".join(lines) + "\n"
        if suffix.strip():
            result += "\n" + suffix.strip() + "\n"
        return result

    @classmethod
    def _render_self(cls, original: str, records: list[MemoryRecord], history: str, self_understanding: str) -> str:
        prefix, suffix = cls._managed_parts(original)
        if not prefix.strip():
            prefix = "# 角色自我认识"
        lines = [_MANAGED_START, "## 自动整理的认识"]
        if self_understanding.strip():
            lines.append("- " + self_understanding.strip())
        if history.strip():
            event_count = len(re.findall(r"^## 消息 ", history, flags=re.MULTILINE))
            lines.append(f"- 已整理共同经历：{event_count} 个消息窗口。")
        lines.append(_MANAGED_END)
        result = prefix.rstrip() + "\n\n" + "\n".join(lines) + "\n"
        if suffix.strip():
            result += "\n" + suffix.strip() + "\n"
        return result

    @classmethod
    def _managed_parts(cls, text: str) -> tuple[str, str]:
        if _MANAGED_START not in text:
            prefix_lines: list[str] = []
            lines = text.splitlines(keepends=True)
            index = 0
            while index < len(lines):
                if _MEMORY_LINE.match(lines[index].rstrip("\r\n")):
                    index += 1
                    if index < len(lines) and _SOURCE_LINE.match(lines[index].rstrip("\r\n")):
                        index += 1
                    continue
                prefix_lines.append(lines[index])
                index += 1
            return "".join(prefix_lines).rstrip(), ""
        prefix, remainder = text.split(_MANAGED_START, 1)
        if _MANAGED_END in remainder:
            _, suffix = remainder.split(_MANAGED_END, 1)
            return prefix.rstrip(), suffix.lstrip()
        return prefix.rstrip(), ""

    @staticmethod
    def _remove_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
        lines = text.splitlines(keepends=True)
        removed = {index for start, end in ranges for index in range(start, end)}
        result = "".join(line for index, line in enumerate(lines) if index not in removed)
        return result if result.strip() else ""

    @staticmethod
    def _commit(writes: dict[Path, str]) -> None:
        previous: dict[Path, str | None] = {}
        staged: dict[Path, Path] = {}
        try:
            for target, content in writes.items():
                previous[target] = target.read_text(encoding="utf-8") if target.exists() else None
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_text(content, encoding="utf-8")
                staged[target] = temporary
            for target, temporary in staged.items():
                os.replace(temporary, target)
        except OSError:
            for target, content in previous.items():
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    restore = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
                    restore.write_text(content, encoding="utf-8")
                    os.replace(restore, target)
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)


class MemoryOptimizerWorker:
    """Run optimizer jobs off the event loop with one serial lane per role."""

    def __init__(self, optimizer: MemoryOptimizer) -> None:
        self.optimizer = optimizer
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[object]] = set()
        self.errors: list[str] = []

    def lock_for(self, role_id: str) -> asyncio.Lock:
        """Return the lock shared with the post-response worker for this role."""
        return self._locks.setdefault(role_id, asyncio.Lock())

    def submit(self, role_id: str) -> None:
        task = asyncio.create_task(self._run(role_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, role_id: str) -> None:
        """Run inside MemoryWorker's per-role lane after Markdown maintenance."""
        await asyncio.to_thread(self.optimizer.optimize, role_id)

    async def _run(self, role_id: str) -> None:
        try:
            async with self.lock_for(role_id):
                await self.run(role_id)
        except Exception as error:
            self.errors.append(f"{role_id}: {error}")

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
