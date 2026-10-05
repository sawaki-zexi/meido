from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable


_CANDIDATE_LINE = re.compile(r"^- \[(?P<memory_type>[^\]]+)\] (?P<summary>.+?) \(来源：.*\)\s*$")
_MEMORY_LINE = re.compile(r"^- \[(?P<memory_type>[^\]]+)\] (?P<summary>.+?)\s*$")
_SOURCE_LINE = re.compile(r"^\s*<!-- source: (?P<source>[^>]+?) -->\s*$")
_MANAGED_START = "<!-- meido-optimizer:start -->"
_MANAGED_END = "<!-- meido-optimizer:end -->"


class _RetryOptimizer(RuntimeError):
    """The optimizer snapshot became stale before its conditional commit."""


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
    DEFAULT_INTERVAL_SECONDS = 18 * 60 * 60
    MIN_INTERVAL_SECONDS = 60
    SELF_RULES_VERSION = 1

    def __init__(
        self,
        roles_root: str | Path,
        consolidate: Callable[[str, list[MemoryRecord], list[MemoryRecord], str, str], MemoryOptimizationResult] | None = None,
        persist_structured: Callable[[str, list[MemoryRecord]], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.roles_root = Path(roles_root).resolve()
        self.consolidate = consolidate
        self.persist_structured = persist_structured
        self.now = now or (lambda: datetime.now(timezone.utc))

    def optimize(self, role_id: str) -> bool:
        role_root = (self.roles_root / role_id).resolve()
        if role_root.parent != self.roles_root:
            raise ValueError("角色路径无效")
        memory_dir = role_root / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        pending_path = memory_dir / "PENDING.md"
        snapshot_path = memory_dir / "PENDING.snapshot.md"
        memory_path = memory_dir / "MEMORY.md"
        self_path = memory_dir / "SELF.md"
        history_path = memory_dir / "HISTORY.md"
        state_path = memory_dir / ".optimizer.json"

        recovery_state = self._read_state(state_path)
        self._recover_pending_snapshot(
            pending_path,
            snapshot_path,
            committed=recovery_state.get("pending_snapshot_committed") is True,
        )
        if recovery_state.get("pending_snapshot_committed") is True and not snapshot_path.exists():
            recovery_state.pop("pending_snapshot_committed", None)
            self._write_state(state_path, recovery_state)
        if self.persist_structured is not None:
            recovery_state = self._read_state(state_path)
            pending_records = self._decode_structured_pending(recovery_state)
            if pending_records is not None:
                self.persist_structured(role_id, pending_records)
                recovery_state.pop("structuredPending", None)
                self._write_state(state_path, recovery_state)

        state = self._read_state(state_path)
        existing_self = self._read_text(self_path)
        explicit_self_version = "self_rules_version" in state
        self_rewrite_required = explicit_self_version and state.get("self_rules_version") != self.SELF_RULES_VERSION

        for _ in range(self.MAX_SNAPSHOT_RETRIES):
            pending_before_snapshot = self._read_text(pending_path)
            history_snapshot = self._read_text(history_path)
            state = self._read_state(state_path)
            history_hash = hashlib.sha256(history_snapshot.encode("utf-8")).hexdigest()
            candidates, _ = self._parse_pending(pending_before_snapshot)
            needs_memory_merge = bool(candidates)
            needs_self_merge = self_rewrite_required or needs_memory_merge or state.get("self_update_pending") is True
            if not needs_memory_merge and not needs_self_merge:
                if not explicit_self_version:
                    self._write_state(state_path, {**state, "self_rules_version": self.SELF_RULES_VERSION})
                return False
            snapshot_created = False
            memory_committed = False
            try:
                if needs_memory_merge:
                    self._snapshot_pending(pending_path, snapshot_path)
                    snapshot_created = True
                    pending_snapshot = self._read_text(snapshot_path)
                    candidates, ranges = self._parse_pending(pending_snapshot)
                else:
                    pending_snapshot = ""
                    ranges = []
                existing_records = self._parse_memory(self._read_text(memory_path))
                existing_self = self._read_text(self_path)
                optimized = (
                    self.consolidate(role_id, existing_records, candidates, history_snapshot, existing_self)
                    if self.consolidate is not None
                    else MemoryOptimizationResult(
                        self._merge_records(existing_records, candidates),
                        self._fallback_self(candidates or existing_records),
                    )
                )
                self._validate_result(existing_records, candidates, optimized)
                if not needs_memory_merge:
                    # A SELF-only retry/rules migration must never rewrite or
                    # truncate MEMORY. The provider may return no records for
                    # this path; use the durable records only as SELF input.
                    self_records = optimized.records or existing_records
                    self_text = self._render_self(
                        existing_self,
                        self_records,
                        history_snapshot,
                        optimized.self_understanding,
                    )
                    current_state = {
                        **state,
                        "historyHash": history_hash,
                        "last_memory_optimized_at": self.now().astimezone(timezone.utc).isoformat(),
                        "self_rules_version": self.SELF_RULES_VERSION,
                    }
                    current_state.pop("self_update_pending", None)
                    self.commit_documents({
                        self_path: self_text,
                        state_path: json.dumps(current_state, ensure_ascii=False, indent=2) + "\n",
                    })
                    return True
                documents = self._build_documents(
                    pending_snapshot,
                    self._read_text(memory_path),
                    existing_self,
                    history_snapshot,
                    optimized,
                    ranges,
                    render_self=False,
                )
                memory_text = documents["memory"]
                self._before_commit()
                if self._read_text(history_path) != history_snapshot:
                    raise _RetryOptimizer("历史文档在归并期间发生变化")
                if needs_memory_merge and not snapshot_path.exists():
                    raise _RetryOptimizer("PENDING 快照已被外部修改")
                # MEMORY is committed before SELF. A SELF failure therefore
                # cannot roll back a successful long-term memory merge.
                current_state = {
                    **state,
                    "historyHash": history_hash,
                    "last_memory_optimized_at": self.now().astimezone(timezone.utc).isoformat(),
                    "self_update_pending": True,
                }
                if snapshot_created:
                    current_state["pending_snapshot_committed"] = True
                if self.persist_structured is not None:
                    current_state["structuredPending"] = [self._record_payload(item) for item in optimized.records]
                previous_documents = {
                    memory_path: self._read_text(memory_path) if memory_path.exists() else None,
                    self_path: self._read_text(self_path) if self_path.exists() else None,
                    state_path: self._read_text(state_path) if state_path.exists() else None,
                }
                self.commit_documents({
                    memory_path: memory_text,
                    state_path: json.dumps(current_state, ensure_ascii=False, indent=2) + "\n",
                })
                memory_committed = True
                if snapshot_created:
                    snapshot_path.unlink(missing_ok=True)
                    current_state.pop("pending_snapshot_committed", None)
                try:
                    # SELF is deliberately rendered and committed after MEMORY.
                    # A provider or file failure here must leave the successful
                    # long-term memory merge intact and retain a retry marker.
                    self_text = self._render_self(
                        existing_self,
                        optimized.records,
                        history_snapshot,
                        optimized.self_understanding,
                    )
                    self.commit_documents({
                        self_path: self_text,
                    })
                except Exception:
                    # Keep MEMORY and candidates committed; the next due scan
                    # retries SELF without resurrecting the consumed snapshot.
                    raise
                current_state["self_rules_version"] = self.SELF_RULES_VERSION
                current_state.pop("self_update_pending", None)
                self._write_state(state_path, current_state)
                if self.persist_structured is not None:
                    try:
                        self.persist_structured(role_id, optimized.records)
                    except Exception:
                        self._restore_documents(previous_documents)
                        if snapshot_created:
                            current_pending = self._read_text(pending_path)
                            restored_pending = pending_before_snapshot
                            if current_pending:
                                if restored_pending and not restored_pending.endswith("\n"):
                                    restored_pending += "\n"
                                restored_pending += current_pending
                            pending_path.write_text(restored_pending, encoding="utf-8")
                        raise
                    # The structured outbox is intentionally acknowledged in
                    # a separate write. If that acknowledgement is interrupted,
                    # leave the outbox marker for the next process to replay.
                    self._write_state(state_path, {key: value for key, value in current_state.items() if key != "structuredPending"})
                return True
            except _RetryOptimizer:
                if snapshot_created:
                    self._restore_pending_snapshot(pending_path, snapshot_path)
                continue
            except Exception:
                # Once MEMORY has been committed, snapshot_path has already
                # been removed and self_update_pending remains durable. Before
                # that point, restore the consumed snapshot atomically.
                if snapshot_created and not memory_committed:
                    self._restore_pending_snapshot(pending_path, snapshot_path)
                raise
        raise RuntimeError("记忆候选在归并期间持续变化，已保留待处理内容")

    @classmethod
    def _validate_result(
        cls,
        existing: list[MemoryRecord],
        candidates: list[MemoryRecord],
        optimized: MemoryOptimizationResult,
    ) -> None:
        if not isinstance(optimized, MemoryOptimizationResult):
            raise ValueError("Optimizer 返回结果格式无效")
        if not isinstance(optimized.self_understanding, str):
            raise ValueError("Optimizer 返回的自我认识格式无效")
        if candidates and not optimized.records:
            raise ValueError("Optimizer 返回空的长期记忆结果")
        required_sources = {source for record in [*existing, *candidates] for source in record.sources if source}
        returned_sources = {source for record in optimized.records for source in record.sources if source}
        if not required_sources.issubset(returned_sources):
            raise ValueError("Optimizer 遗漏了已有记忆来源，已保留待处理内容")
        for record in optimized.records:
            if not record.memory_type.strip() or not record.summary.strip() or any(not source.strip() for source in record.sources):
                raise ValueError("Optimizer 返回的记忆条目缺少必要字段")

    @staticmethod
    def _snapshot_pending(pending_path: Path, snapshot_path: Path) -> None:
        if snapshot_path.exists():
            return
        if pending_path.exists():
            os.replace(pending_path, snapshot_path)
        else:
            snapshot_path.write_text("", encoding="utf-8")
        pending_path.write_text("", encoding="utf-8")

    @staticmethod
    def _restore_pending_snapshot(pending_path: Path, snapshot_path: Path) -> None:
        if not snapshot_path.exists():
            return
        snapshot = MemoryOptimizer._read_text(snapshot_path)
        current = MemoryOptimizer._read_text(pending_path)
        merged = snapshot
        if current:
            if merged and not merged.endswith("\n"):
                merged += "\n"
            merged += current
        pending_path.write_text(merged, encoding="utf-8")
        snapshot_path.unlink(missing_ok=True)

    @classmethod
    def _recover_pending_snapshot(cls, pending_path: Path, snapshot_path: Path, *, committed: bool = False) -> None:
        if snapshot_path.exists():
            if committed:
                snapshot_path.unlink()
            else:
                cls._restore_pending_snapshot(pending_path, snapshot_path)

    @classmethod
    def should_run(cls, roles_root: str | Path, role_id: str, *, now: datetime | None = None, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> bool:
        memory_dir = (Path(roles_root).resolve() / role_id / "memory")
        pending = memory_dir / "PENDING.md"
        snapshot = memory_dir / "PENDING.snapshot.md"
        try:
            pending_has_content = pending.exists() and bool(pending.read_text(encoding="utf-8").strip())
        except OSError:
            # A read failure must leave the role eligible for the next scan;
            # the worker will record the concrete error during optimize().
            pending_has_content = True
        if snapshot.exists() or pending_has_content:
            return True
        state = cls._read_state(memory_dir / ".optimizer.json")
        if state.get("self_update_pending") is True:
            return True
        if state.get("self_rules_version") != cls.SELF_RULES_VERSION:
            return True
        raw = state.get("last_memory_optimized_at")
        if not isinstance(raw, str):
            return True
        try:
            updated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return (current - updated.astimezone(timezone.utc)).total_seconds() >= max(cls.MIN_INTERVAL_SECONDS, interval_seconds)

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
        *,
        render_self: bool = True,
    ) -> dict[str, str]:
        return {
            "memory": self.render_managed_memory(memory_text, optimized.records),
            "self": (
                self._render_self(self_text, optimized.records, history_text, optimized.self_understanding)
                if render_self
                else self_text
            ),
            "pending": self._remove_ranges(pending_text, ranges),
        }

    @staticmethod
    def _fallback_self(candidates: list[MemoryRecord]) -> str:
        return "；".join(record.summary for record in candidates[:8])

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @staticmethod
    def _read_state(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _record_payload(record: MemoryRecord) -> dict[str, object]:
        return {"memoryType": record.memory_type, "summary": record.summary, "sources": record.sources}

    @staticmethod
    def _decode_structured_pending(state: dict[str, object]) -> list[MemoryRecord] | None:
        values = state.get("structuredPending")
        if not isinstance(values, list):
            return None
        records: list[MemoryRecord] = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("结构化记忆恢复记录格式无效")
            memory_type, summary, sources = value.get("memoryType"), value.get("summary"), value.get("sources")
            if not isinstance(memory_type, str) or not isinstance(summary, str) or not isinstance(sources, list):
                raise ValueError("结构化记忆恢复记录缺少必要字段")
            if any(not isinstance(source, str) for source in sources):
                raise ValueError("结构化记忆恢复来源格式无效")
            records.append(MemoryRecord(memory_type, summary, list(sources)))
        return records

    @staticmethod
    def _write_state(path: Path, state: dict[str, object]) -> None:
        MemoryOptimizer.commit_documents({path: json.dumps(state, ensure_ascii=False, indent=2) + "\n"})

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
    def render_managed_memory(cls, original: str, records: list[MemoryRecord]) -> str:
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
    def commit_documents(writes: dict[Path, str]) -> None:
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

    @classmethod
    def _restore_documents(cls, previous: dict[Path, str | None]) -> None:
        writes = {
            path: content
            for path, content in previous.items()
            if content is not None
        }
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
        if writes:
            cls.commit_documents(writes)


class MemoryOptimizerWorker:
    """Run optimizer jobs off the event loop with one serial lane per role."""

    def __init__(self, optimizer: MemoryOptimizer) -> None:
        self.optimizer = optimizer
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[asyncio.Task[object], str] = {}
        self._scheduled_roles: set[str] = set()
        self._deleting: set[str] = set()
        self._deleted: set[str] = set()
        self._optimizer_lock = asyncio.Lock()
        self.errors: list[str] = []
        self.closed = False

    def start(self) -> None:
        self.closed = False

    def lock_for(self, role_id: str) -> asyncio.Lock:
        """Return the lock shared with the post-response worker for this role."""
        return self._locks.setdefault(role_id, asyncio.Lock())

    def submit(self, role_id: str) -> bool:
        if self.closed or role_id in self._scheduled_roles or role_id in self._deleting or role_id in self._deleted:
            return False
        self._scheduled_roles.add(role_id)
        task = asyncio.create_task(self._run(role_id))
        self._tasks[task] = role_id
        def done(completed: asyncio.Task[object]) -> None:
            self._tasks.pop(completed, None)
            self._scheduled_roles.discard(role_id)
        task.add_done_callback(done)
        return True

    def resume_pending(self) -> int:
        """Schedule role documents with durable candidate content for startup recovery."""
        if self.closed or not self.optimizer.roles_root.exists():
            return 0
        scheduled = 0
        for role_root in self.optimizer.roles_root.iterdir():
            pending = role_root / "memory" / "PENDING.md"
            if role_root.is_dir() and not role_root.name.startswith(".") and role_root.name not in self._deleted and pending.is_file():
                try:
                    has_content = bool(pending.read_text(encoding="utf-8").strip())
                except OSError as error:
                    self.errors.append(f"{role_root.name}: 读取待整理记忆失败：{error}")
                    continue
                if has_content and self.submit(role_root.name):
                    scheduled += 1
        return scheduled

    async def begin_role_deletion(self, role_id: str) -> None:
        """Stop scheduling a role and wait for its independent optimizer task."""
        self._deleting.add(role_id)
        tasks = tuple(
            task for task, task_role in self._tasks.items()
            if task_role == role_id and not task.done()
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def end_role_deletion(self, role_id: str, *, deleted: bool = False) -> None:
        self._deleting.discard(role_id)
        if deleted:
            self._deleted.add(role_id)

    async def run(self, role_id: str) -> None:
        """Run inside MemoryWorker's per-role lane after Markdown maintenance."""
        await asyncio.to_thread(self.optimizer.optimize, role_id)

    async def _run(self, role_id: str) -> None:
        try:
            async with self._optimizer_lock:
                async with self.lock_for(role_id):
                    await self.run(role_id)
        except Exception as error:
            self.errors.append(f"{role_id}: {error}")

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        self.closed = True
        await self.drain()


class MemoryOptimizerLoop:
    """Independent low-frequency scheduler for role memory optimization."""

    DEFAULT_INTERVAL_SECONDS = MemoryOptimizer.DEFAULT_INTERVAL_SECONDS
    MIN_INTERVAL_SECONDS = MemoryOptimizer.MIN_INTERVAL_SECONDS

    def __init__(
        self,
        worker: MemoryOptimizerWorker,
        roles_root: str | Path,
        *,
        enabled: bool = True,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], object] | None = None,
        model_available: Callable[[str], bool] | None = None,
    ) -> None:
        self.worker = worker
        self.roles_root = Path(roles_root).resolve()
        self.enabled = enabled
        self.interval_seconds = max(self.MIN_INTERVAL_SECONDS, float(interval_seconds))
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep
        self.model_available = model_available or (lambda role_id: True)
        self._task: asyncio.Task[object] | None = None
        self.closed = False
        self.errors: list[str] = []

    def start(self) -> bool:
        if not self.enabled or self.closed or self._task is not None:
            return False
        self.worker.start()
        self._task = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        try:
            await self.run_once(startup=True)
            while not self.closed:
                await self._sleep(self.interval_seconds)
                if not self.closed:
                    await self.run_once(startup=False)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors.append(str(error))

    async def run_once(self, *, startup: bool = False) -> int:
        if not self.enabled or self.closed or not self.roles_root.exists():
            return 0
        scheduled = 0
        for role_root in sorted(self.roles_root.iterdir(), key=lambda path: path.name):
            if not role_root.is_dir() or role_root.name.startswith("."):
                continue
            if not self.model_available(role_root.name):
                continue
            if startup and not MemoryOptimizer.should_run(
                self.roles_root,
                role_root.name,
                now=self.now(),
                interval_seconds=self.interval_seconds,
            ):
                continue
            if self.worker.submit(role_root.name):
                scheduled += 1
        return scheduled

    async def close(self) -> None:
        self.closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
