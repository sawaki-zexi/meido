from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from collections.abc import Callable

from .memory_events import ConsolidationCandidate, ConsolidationCommitted
from .models import Message
from .session_store import SessionStore
from .memory_documents import MemoryDocuments
from .todo_intent import is_explicit_todo_request


class ConsolidationDraftError(ValueError):
    """The configured consolidation provider returned no valid draft."""


class MemoryMaintenance:
    """Maintain each role's Markdown memory views from committed messages."""

    RECENT_MESSAGE_LIMIT = 12
    RECENT_CHAR_LIMIT = 4000
    CONSOLIDATION_MIN_READY_MESSAGES = max(5, RECENT_MESSAGE_LIMIT // 2)

    def __init__(
        self,
        roles_root: str | Path,
        sessions: SessionStore,
        on_consolidation_committed: Callable[[ConsolidationCommitted], None] | None = None,
        consolidation_provider: Callable[[str, list[Message]], object] | None = None,
        role_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self.roles_root = Path(roles_root).resolve()
        self.sessions = sessions
        self.on_consolidation_committed = on_consolidation_committed
        self.consolidation_provider = consolidation_provider
        self.role_exists = role_exists or (lambda role_id: True)
        self.errors: list[str] = []
        self._draft_overrides: dict[str, dict[str, str]] = {}

    def resume_pending(self) -> int:
        """Retry durable consolidation events left by a previous process."""
        if not self.roles_root.exists():
            return 0
        resumed = 0
        for role_root in self.roles_root.iterdir():
            if not role_root.is_dir():
                continue
            if role_root.name.startswith(".") or not self.role_exists(role_root.name):
                continue
            cursor_path = role_root / "memory" / ".maintenance.json"
            cursor = self._read_cursor(cursor_path)
            if "pendingEvent" not in cursor:
                continue
            if self.on_consolidation_committed is None:
                continue
            try:
                self.maintain(role_root.name, f"role:{role_root.name}")
                resumed += 1
            except Exception as error:
                self.errors.append(f"{role_root.name}: pending consolidation retry failed: {error}")
        return resumed

    def maintain(self, role_id: str, session_key: str, *, force: bool = False) -> None:
        if session_key != f"role:{role_id}":
            raise ValueError("会话与角色不匹配")
        role_root = (self.roles_root / role_id).resolve()
        if role_root.parent != self.roles_root:
            raise ValueError("角色路径无效")
        memory_dir = role_root / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "journal").mkdir(parents=True, exist_ok=True)

        messages = self._committed_messages(self.sessions.context_messages(session_key))
        cursor_path = memory_dir / ".maintenance.json"
        cursor = self._read_cursor(cursor_path)
        cursor = self._retry_pending_event(cursor_path, cursor, role_id, session_key)
        if "pendingEvent" in cursor and self.on_consolidation_committed is None:
            return
        cursor_snapshot = dict(cursor)
        last_sequence = cursor["lastSequence"]
        recent = self._recent_context(messages)
        writes: dict[Path, str] = {memory_dir / "RECENT_CONTEXT.md": recent}
        document_snapshot = {path: self._file_signature(path) for path in writes}

        new_messages = [message for message in messages if message.sequence > last_sequence]
        eligible = [message for message in messages if message.sequence > last_sequence]
        ready = len(eligible) - self.RECENT_MESSAGE_LIMIT
        event: ConsolidationCommitted | None = None
        if force or ready >= self.CONSOLIDATION_MIN_READY_MESSAGES:
            window = self._window(messages, last_sequence)
            if window:
                source_key = self._source_key(role_id, window)
                history_path = memory_dir / "HISTORY.md"
                pending_path = memory_dir / "PENDING.md"
                history = self._read_text(history_path)
                pending = self._read_text(pending_path)
                journal_path = MemoryDocuments(self.roles_root).journal_path(role_id, window[0].createdAt.date().isoformat())
                journal = self._read_text(journal_path)
                try:
                    candidates = self._candidates(source_key, window)
                except (ConsolidationDraftError, TimeoutError, ValueError):
                    # Keep RECENT_CONTEXT, but leave the consolidation cursor
                    # unchanged so the same input can be retried later.
                    self._before_commit()
                    latest = self._committed_messages(self.sessions.context_messages(session_key))
                    if self._message_signature(latest) != self._message_signature(messages):
                        return
                    if self._read_cursor(cursor_path) != cursor_snapshot:
                        return
                    if any(self._file_signature(path) != signature for path, signature in document_snapshot.items()):
                        return
                    self._commit(writes, cursor_path, cursor_snapshot)
                    return
                draft = self._draft_overrides.get(source_key, {})
                if draft.get("recent"):
                    writes[memory_dir / "RECENT_CONTEXT.md"] = draft["recent"]
                if source_key not in history:
                    history += draft.get("history", "") or self._history_entry(source_key, window)
                if source_key not in pending:
                    pending += self._pending_entries(source_key, window, candidates)
                if source_key not in journal:
                    journal += MemoryDocuments.render_journal_entry(source_key, window[0].sequence, window[-1].sequence)
                cursor = {"lastSequence": window[-1].sequence, "lastSourceKey": source_key}
                event = ConsolidationCommitted(
                    role_id=role_id,
                    session_key=session_key,
                    source_key=source_key,
                    message_ids=tuple(message.id for message in window),
                    message_range=(window[0].sequence, window[-1].sequence),
                    candidates=tuple(candidates),
                )
                self._draft_overrides.pop(source_key, None)
                # Persist the event even when this process has no consumer;
                # another process can resume it after startup.
                cursor["pendingEvent"] = self._event_payload(event)
                writes[history_path] = history
                writes[pending_path] = pending
                writes[journal_path] = journal
                document_snapshot.update({
                    history_path: self._file_signature(history_path),
                    pending_path: self._file_signature(pending_path),
                    journal_path: self._file_signature(journal_path),
                })
        self._before_commit()
        latest = self._committed_messages(self.sessions.context_messages(session_key))
        if self._message_signature(latest) != self._message_signature(messages):
            return
        if self._read_cursor(cursor_path) != cursor_snapshot:
            return
        if any(self._file_signature(path) != signature for path, signature in document_snapshot.items()):
            return
        if not new_messages and cursor_path.exists() and (memory_dir / "RECENT_CONTEXT.md").exists():
            return
        self._commit(writes, cursor_path, cursor)
        if event is not None and self.on_consolidation_committed is not None:
            self._publish_event(event, cursor_path, cursor)

    def ensure_memory_for_window(
        self,
        role_id: str,
        session_key: str,
        *,
        max_messages: int | None = None,
    ) -> bool:
        """Ensure older committed turns are consolidated before input eviction.

        The normal maintenance threshold remains unchanged.  This entry point is
        used by hosts that are about to trim model input history and forces one
        maintenance pass when the committed context exceeds the requested window.
        """
        if max_messages is None:
            max_messages = self.RECENT_MESSAGE_LIMIT
        if max_messages < self.RECENT_MESSAGE_LIMIT:
            raise ValueError("max_messages must include the recent context window")
        messages = self._committed_messages(self.sessions.context_messages(session_key))
        cursor_path = (self.roles_root / role_id / "memory" / ".maintenance.json").resolve()
        cursor = self._read_cursor(cursor_path)
        eligible = [message for message in messages if message.sequence > int(cursor["lastSequence"])]
        if len(eligible) <= max_messages:
            return False
        self.maintain(role_id, session_key, force=True)
        return True

    def sync_structured_memory(self, role_id: str, memories: list[object]) -> None:
        MemoryDocuments(self.roles_root).sync_structured_memory(role_id, memories)  # type: ignore[arg-type]

    def _before_commit(self) -> None:
        """Hook for deterministic stale-snapshot verification in tests."""

    @staticmethod
    def _read_cursor(path: Path) -> dict[str, object]:
        if not path.exists():
            return {"lastSequence": 0}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"lastSequence": 0}
        if not isinstance(value, dict) or not isinstance(value.get("lastSequence"), int):
            return {"lastSequence": 0}
        return value

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _recent_context(self, messages: list[Message]) -> str:
        heading = "# 近期对话\n\n"
        selected: list[str] = []
        length = 0
        for message in reversed(messages[-self.RECENT_MESSAGE_LIMIT :]):
            line = f"- [{message.role}] {message.content.strip()}"
            if not message.content.strip():
                continue
            candidate_count = len(selected) + 1
            candidate_length = length + len(line) + (1 if selected else 0)
            if len(heading) + candidate_length + max(0, candidate_count - 1) + 1 > self.RECENT_CHAR_LIMIT:
                break
            selected.append(line)
            length = candidate_length
        selected.reverse()
        return heading + "\n".join(selected) + ("\n" if selected else "")

    def _window(self, messages: list[Message], after_sequence: int) -> list[Message]:
        eligible = [message for message in messages if message.sequence > after_sequence]
        if len(eligible) <= self.RECENT_MESSAGE_LIMIT:
            return []
        return eligible[:-self.RECENT_MESSAGE_LIMIT]

    @staticmethod
    def _committed_messages(messages: list[Message]) -> list[Message]:
        committed: list[Message] = []
        for index, message in enumerate(messages[:-1]):
            following = messages[index + 1]
            if message.role == "user" and following.role == "assistant" and following.status == "completed":
                if not is_explicit_todo_request(message.content):
                    committed.extend((message, following))
        return committed

    @staticmethod
    def _message_signature(messages: list[Message]) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (message.id, message.sequence, message.role, message.status, message.content, message.createdAt.isoformat())
            for message in messages
        )

    @staticmethod
    def _source_key(role_id: str, window: list[Message]) -> str:
        material = "\n".join(message.id for message in window)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
        return f"consolidation:{role_id}:{window[0].sequence}-{window[-1].sequence}:{digest}"

    @staticmethod
    def _history_entry(source_key: str, window: list[Message]) -> str:
        lines = [f"\n## 消息 {window[0].sequence}–{window[-1].sequence}\n", f"<!-- source: {source_key} -->\n"]
        for message in window:
            content = re.sub(r"\s+", " ", message.content).strip()
            if content:
                lines.append(f"- [{message.role}] {content}\n")
        return "".join(lines)

    def _candidates(self, source_key: str, window: list[Message]) -> list[ConsolidationCandidate]:
        if self.consolidation_provider is not None:
            raw = self.consolidation_provider(source_key, window)
            draft: dict[str, str] = {}
            if isinstance(raw, dict):
                history = raw.get("history", raw.get("historyText", ""))
                recent = raw.get("recentContext", raw.get("recent", ""))
                if isinstance(history, str):
                    draft["history"] = history[:12000]
                if isinstance(recent, str):
                    draft["recent"] = recent[: self.RECENT_CHAR_LIMIT]
                raw = raw.get("pendingItems", raw.get("memories", []))
            if not isinstance(raw, list) or (not raw and not draft):
                raise ConsolidationDraftError("整理模型未返回有效候选")
            if draft:
                self._draft_overrides[source_key] = draft
            candidates: list[ConsolidationCandidate] = []
            for value in raw:
                if isinstance(value, ConsolidationCandidate):
                    candidate = value
                elif isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key].strip() for key in ("memoryType", "summary", "sourceKey")):
                    candidate = ConsolidationCandidate(value["memoryType"], value["summary"], value["sourceKey"])
                else:
                    raise ConsolidationDraftError("整理模型返回结构无效")
                if not candidate.summary.strip():
                    raise ConsolidationDraftError("整理模型返回空摘要")
                candidates.append(candidate)
            return candidates
        from .memory_service import MemoryService

        candidates: list[ConsolidationCandidate] = []
        for message in window:
            if message.role != "user":
                continue
            for memory_type, summary, _ in MemoryService.extract_candidates(message.content):
                candidates.append(ConsolidationCandidate(
                    memory_type=memory_type,
                    summary=summary,
                    source_key=f"{source_key}:{message.id}:{memory_type}",
                ))
        return candidates

    def _pending_entries(
        self,
        source_key: str,
        window: list[Message],
        candidates: list[ConsolidationCandidate] | None = None,
    ) -> str:
        if candidates is None:
            return "".join(
                f"\n- [{candidate.memory_type}] {candidate.summary} (来源：消息 {message.sequence})\n"
                f"  <!-- source: {candidate.source_key} -->\n"
                for message in window
                for candidate in self._candidates(source_key, [message])
            )
        return "".join(
            f"\n- [{candidate.memory_type}] {candidate.summary} (来源：整理 {source_key})\n"
            f"  <!-- source: {candidate.source_key} -->\n"
            for candidate in candidates
        )

    @staticmethod
    def _event_payload(event: ConsolidationCommitted) -> dict[str, object]:
        return {
            "roleId": event.role_id,
            "sessionKey": event.session_key,
            "sourceKey": event.source_key,
            "messageIds": list(event.message_ids),
            "messageRange": list(event.message_range) if event.message_range else None,
            "candidates": [
                {
                    "memoryType": candidate.memory_type,
                    "summary": candidate.summary,
                    "sourceKey": candidate.source_key,
                }
                for candidate in event.candidates
            ],
            "error": event.error,
        }

    @staticmethod
    def _event_from_payload(payload: object) -> ConsolidationCommitted:
        if not isinstance(payload, dict):
            raise ValueError("整理事件格式无效")
        role_id = payload.get("roleId")
        session_key = payload.get("sessionKey")
        source_key = payload.get("sourceKey")
        message_ids = payload.get("messageIds")
        message_range = payload.get("messageRange")
        raw_candidates = payload.get("candidates")
        if not all(isinstance(value, str) for value in (role_id, session_key, source_key)):
            raise ValueError("整理事件来源无效")
        if not isinstance(message_ids, list) or any(not isinstance(value, str) for value in message_ids):
            raise ValueError("整理事件消息引用无效")
        parsed_range = None
        if message_range is not None:
            if not isinstance(message_range, list) or len(message_range) != 2 or any(not isinstance(value, int) for value in message_range):
                raise ValueError("整理事件消息范围无效")
            parsed_range = (message_range[0], message_range[1])
        if not isinstance(raw_candidates, list):
            raise ValueError("整理事件候选无效")
        candidates: list[ConsolidationCandidate] = []
        for value in raw_candidates:
            if not isinstance(value, dict) or not all(isinstance(value.get(key), str) for key in ("memoryType", "summary", "sourceKey")):
                raise ValueError("整理事件候选格式无效")
            candidates.append(ConsolidationCandidate(value["memoryType"], value["summary"], value["sourceKey"]))
        return ConsolidationCommitted(
            role_id=role_id,
            session_key=session_key,
            source_key=source_key,
            message_ids=tuple(message_ids),
            message_range=parsed_range,
            candidates=tuple(candidates),
            error=payload.get("error") if isinstance(payload.get("error"), str) else None,
        )

    def _publish_event(
        self,
        event: ConsolidationCommitted,
        cursor_path: Path,
        cursor: dict[str, object],
    ) -> None:
        assert self.on_consolidation_committed is not None
        try:
            self.on_consolidation_committed(event)
        except Exception:
            # The committed event remains in the cursor and is retried before
            # the next maintenance snapshot. Structured consumers are idempotent.
            raise
        acknowledged = dict(cursor)
        acknowledged.pop("pendingEvent", None)
        self._commit({}, cursor_path, acknowledged)

    @staticmethod
    def _file_signature(path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def _retry_pending_event(
        self,
        cursor_path: Path,
        cursor: dict[str, object],
        role_id: str,
        session_key: str,
    ) -> dict[str, object]:
        payload = cursor.get("pendingEvent")
        if payload is None:
            return cursor
        event = self._event_from_payload(payload)
        if event.role_id != role_id or event.session_key != session_key:
            raise ValueError("整理事件作用域与当前角色不匹配")
        # A process started without its structured consumer must retain the
        # durable event for the next process that can consume it.
        if self.on_consolidation_committed is None:
            return cursor
        self.on_consolidation_committed(event)
        updated = dict(cursor)
        updated.pop("pendingEvent", None)
        self._commit({}, cursor_path, updated)
        return updated

    @staticmethod
    def _commit(writes: dict[Path, str], cursor_path: Path, cursor: dict[str, object]) -> None:
        payloads = {**writes, cursor_path: json.dumps(cursor, ensure_ascii=False, indent=2) + "\n"}
        previous: dict[Path, str | None] = {}
        staged: dict[Path, Path] = {}
        try:
            for target, content in payloads.items():
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
