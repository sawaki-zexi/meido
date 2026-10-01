from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from .models import Message
from .session_store import SessionStore


class MemoryMaintenance:
    """Maintain each role's Markdown memory views from committed messages."""

    CONSOLIDATION_MIN_MESSAGES = 20
    RECENT_MESSAGE_LIMIT = 12
    RECENT_CHAR_LIMIT = 4000

    def __init__(self, roles_root: str | Path, sessions: SessionStore) -> None:
        self.roles_root = Path(roles_root).resolve()
        self.sessions = sessions

    def maintain(self, role_id: str, session_key: str) -> None:
        if session_key != f"role:{role_id}":
            raise ValueError("会话与角色不匹配")
        role_root = (self.roles_root / role_id).resolve()
        if role_root.parent != self.roles_root:
            raise ValueError("角色路径无效")
        memory_dir = role_root / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)

        messages = self._committed_messages(self.sessions.context_messages(session_key))
        cursor_path = memory_dir / ".maintenance.json"
        cursor = self._read_cursor(cursor_path)
        last_sequence = cursor["lastSequence"]
        recent = self._recent_context(messages)
        writes: dict[Path, str] = {memory_dir / "RECENT_CONTEXT.md": recent}

        new_messages = [message for message in messages if message.sequence > last_sequence]
        if len(new_messages) >= self.CONSOLIDATION_MIN_MESSAGES:
            window = self._window(messages, last_sequence)
            if window:
                source_key = self._source_key(role_id, window)
                history_path = memory_dir / "HISTORY.md"
                pending_path = memory_dir / "PENDING.md"
                history = self._read_text(history_path)
                pending = self._read_text(pending_path)
                if source_key not in history:
                    history += self._history_entry(source_key, window)
                if source_key not in pending:
                    pending += self._pending_entries(source_key, window)
                cursor = {"lastSequence": window[-1].sequence, "lastSourceKey": source_key}
                writes[history_path] = history
                writes[pending_path] = pending
        self._before_commit()
        latest = self._committed_messages(self.sessions.context_messages(session_key))
        if [message.id for message in latest] != [message.id for message in messages]:
            return
        self._commit(writes, cursor_path, cursor)

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
        selected: list[str] = []
        length = 0
        for message in reversed(messages[-self.RECENT_MESSAGE_LIMIT :]):
            line = f"- [{message.role}] {message.content.strip()}"
            if not message.content.strip():
                continue
            if length + len(line) + 1 > self.RECENT_CHAR_LIMIT:
                break
            selected.append(line)
            length += len(line) + 1
        selected.reverse()
        return "# 近期对话\n\n" + "\n".join(selected) + ("\n" if selected else "")

    @staticmethod
    def _window(messages: list[Message], after_sequence: int) -> list[Message]:
        return [message for message in messages if message.sequence > after_sequence]

    @staticmethod
    def _committed_messages(messages: list[Message]) -> list[Message]:
        committed: list[Message] = []
        for index, message in enumerate(messages[:-1]):
            following = messages[index + 1]
            if message.role == "user" and following.role == "assistant" and following.status == "completed":
                committed.extend((message, following))
        return committed

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

    def _pending_entries(self, source_key: str, window: list[Message]) -> str:
        from .memory_service import MemoryService

        entries: list[str] = []
        for message in window:
            if message.role != "user":
                continue
            for memory_type, summary, _ in MemoryService.extract_candidates(message.content):
                entries.append(
                    f"\n- [{memory_type}] {summary} (来源：消息 {message.sequence})\n"
                    f"  <!-- source: {source_key}:{message.id}:{memory_type} -->\n"
                )
        return "".join(entries)

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
