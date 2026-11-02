from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import threading
import inspect
from pathlib import Path
from collections.abc import Callable

from .memory_events import ConsolidationCandidate, ConsolidationCommitted
from .models import Message
from .session_store import SessionStore
from .memory_documents import MemoryDocuments
from .memory_document_lock import document_lock, MarkdownCommitGuard
from .todo_intent import is_explicit_todo_request


class ConsolidationDraftError(ValueError):
    """The configured consolidation provider returned no valid draft."""


class MemoryMaintenance:
    """Maintain each role's Markdown memory views from committed messages."""

    RECENT_MESSAGE_LIMIT = 20
    RECENT_CHAR_LIMIT = 4000
    CONSOLIDATION_MIN_READY_MESSAGES = 10

    def __init__(
        self,
        roles_root: str | Path,
        sessions: SessionStore,
        on_consolidation_committed: Callable[[ConsolidationCommitted], None] | None = None,
        consolidation_provider: Callable[[str, list[Message]], object] | None = None,
        role_exists: Callable[[str], bool] | None = None,
        messages_provider: Callable[[str, str], list[Message]] | None = None,
        recent_limit_provider: Callable[[], int] | None = None,
        relationship_consumer: Callable[[ConsolidationCommitted], None] | None = None,
        ownership_provider: Callable[[str], dict[str, object]] | None = None,
        defer_consumers: bool = False,
        configuration_generation: Callable[[str], str] | None = None,
    ) -> None:
        self.roles_root = Path(roles_root).resolve()
        self.sessions = sessions
        self.on_consolidation_committed = on_consolidation_committed
        self.consolidation_provider = consolidation_provider
        self.role_exists = role_exists or (lambda role_id: True)
        self.messages_provider = messages_provider
        self.recent_limit_provider = recent_limit_provider
        self.relationship_consumer = relationship_consumer
        self.ownership_provider = ownership_provider
        self.defer_consumers = defer_consumers
        self.configuration_generation = configuration_generation
        self._configuration_generations: dict[str, str] = {}
        self._configuration_lock = threading.RLock()
        self.failures: dict[str, str] = {}
        self.errors: list[str] = []
        self._draft_local = threading.local()

    def resume_pending(self, *, schedule: Callable[[str], None] | None = None) -> int:
        """Import legacy cursors once, then resume each acknowledged consumer."""
        if not self.roles_root.exists():
            return 0
        resumed = 0
        for root in self.roles_root.iterdir():
            if root.is_dir() and not root.name.startswith(".") and self.role_exists(root.name):
                path = root / "memory" / ".maintenance.json"
                progress = self._read_cursor(path)
                if progress.get("pendingEvents") or progress.get("relationshipPayload"):
                    if schedule is not None:
                        schedule(root.name)
                    else:
                        self._retry_pending_event(path, progress, root.name, f"role:{root.name}")
                    resumed += 1
        return resumed

    @property
    def recent_limit(self) -> int:
        return self.recent_limit_provider() if self.recent_limit_provider else self.RECENT_MESSAGE_LIMIT

    @property
    def minimum_new_messages(self) -> int:
        return max(5, self.recent_limit // 2)

    def refresh_configuration(self, role_id: str) -> str:
        """Known failures belong to the configuration that attempted the draft."""
        with self._configuration_lock:
            generation = self.configuration_generation(role_id) if self.configuration_generation else ''
            if role_id in self._configuration_generations and self._configuration_generations[role_id] != generation:
                self.failures.pop(role_id, None)
            self._configuration_generations[role_id] = generation
            return generation

    def maintain(self, role_id: str, session_key: str, *, force: bool = False, through_sequence: int | None = None, commit_guard=None) -> None:
        guard = commit_guard or MarkdownCommitGuard()
        if guard.cancelled:
            return
        if session_key != f"role:{role_id}" or not self.role_exists(role_id):
            raise ValueError("会话与角色不匹配")
        generation = self.refresh_configuration(role_id)
        memory_dir = MemoryDocuments(self.roles_root).memory_dir(role_id)
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "journal").mkdir(parents=True, exist_ok=True)
        cursor_path = memory_dir / ".maintenance.json"
        messages = self._messages(role_id, session_key)
        cursor = self._read_cursor(cursor_path)
        valid = self._context_cursor(cursor, messages, session_key)
        if valid != cursor:
            self._save(cursor_path, valid, expected=cursor)
        cursor = valid if self.defer_consumers else self._retry_pending_event(cursor_path, valid, role_id, session_key)
        cursor_snapshot = dict(cursor)
        eligible = [message for message in messages if message.sequence > int(cursor["lastSequence"])]
        recent_path = memory_dir / "RECENT_CONTEXT.md"
        ownership = self.ownership_provider(role_id) if self.ownership_provider else {}
        previous = self._read_text(recent_path)
        abstract = ''
        match = re.match(r'<!-- recent-context-sources: (.*?) -->\s*', previous)
        if match:
            try:
                previous_owners = json.loads(match.group(1))
            except ValueError:
                previous_owners = None
            if isinstance(previous_owners, dict) and self._ownership_matches(previous_owners, ownership):
                abstract = previous[match.end():].split('<!-- recent-raw -->', 1)[0].strip()
        raw = self._recent_context(messages, ownership=ownership).split('<!-- recent-raw -->', 1)[-1]
        writes = {recent_path: self._recent_document(abstract, ownership, raw=raw)}
        snapshots = {recent_path: self._file_signature(recent_path)}
        additions = {}
        window = []
        if force or len(eligible) - self.recent_limit >= self.minimum_new_messages:
            window = (self._window(messages, int(cursor["lastSequence"])) if through_sequence is None else
                      [message for message in eligible if message.sequence <= through_sequence])
        if window:
            source_key = self._source_key(role_id, window)
            history_path, pending_path = memory_dir / "HISTORY.md", memory_dir / "PENDING.md"
            journal_path = MemoryDocuments(self.roles_root).journal_path(role_id, window[0].createdAt.date().isoformat())
            try:
                candidates = self._candidates(source_key, window, commit_guard=guard)
            except Exception as error:
                with self._configuration_lock:
                    if not guard.cancelled and generation == self.refresh_configuration(role_id):
                        self.failures[role_id] = str(error) or type(error).__name__
                return
            draft = getattr(self._draft_local, 'draft', {})
            self._draft_local.draft = {}
            if draft.get("recent"):
                writes[recent_path] = self._recent_document(draft["recent"], ownership)
            additions = {
                history_path: draft['history'] + f"\n<!-- source: {source_key} -->\n" if 'history' in draft else self._history_entry(source_key, window),
                pending_path: draft['pending'] + f'\n<!-- source: {source_key} -->\n' if 'pending' in draft else self._pending_entries(source_key, window, candidates),
                journal_path: MemoryDocuments.render_journal_entry(source_key, window[0].sequence, window[-1].sequence)}
            event = ConsolidationCommitted(role_id=role_id, session_key=session_key, source_key=source_key,
                message_ids=tuple(message.id for message in window), message_range=(window[0].sequence, window[-1].sequence),
                candidates=tuple(candidates), history_entries=tuple(draft.get('history_entries', ())),
                conversation=str(draft.get('conversation', '')))
            version = int(cursor.get("memoryVersion", 0)) + 1
            payload = self._event_payload(event)
            cursor = {**cursor, "lastSequence": window[-1].sequence, "lastSourceKey": source_key,
                "memoryVersion": version,
                "sources": self._prefix_stamp([m for m in messages if m.sequence <= window[-1].sequence]),
                "ownership": ownership,
                "pendingEvents": [*cursor.get("pendingEvents", []), {"version": version, "payload": payload}],
                "relationshipPayload": {"version": version, "payload": payload}}
            if window[-1].metadata.get("contextSequenceSource"):
                cursor["sequenceSource"] = window[-1].metadata["contextSequenceSource"]
        self._before_commit()
        latest = self._messages(role_id, session_key)
        # Only this frozen prefix belongs to the preparation. A legal tail append
        # is independent; changed/deleted/re-authorized source messages are not.
        if not self.role_exists(role_id) or not self._prefix_matches(messages, latest):
            return
        if self.ownership_provider and not self._ownership_matches(ownership, self.ownership_provider(role_id)):
            return
        with guard.lock, document_lock(memory_dir):
            if guard.cancelled:
                return
            if any(self._file_signature(path) != signature for path, signature in snapshots.items()):
                return
            for path, addition in additions.items():
                latest_text = self._read_text(path)
                if source_key not in latest_text:
                    writes[path] = latest_text.rstrip() + '\n\n' + addition
            # Independent consumer acknowledgements can advance while drafting.
            current = self._read_cursor(cursor_path)
            if current.get('memoryVersion', 0) != cursor_snapshot.get('memoryVersion', 0) or current.get('sources') != cursor_snapshot.get('sources'):
                return
            acknowledged = int(current.get('publishedVersion', 0))
            cursor.update(publishedVersion=acknowledged,
                pendingEvents=[entry for entry in cursor.get('pendingEvents', []) if entry['version'] > acknowledged],
                relationshipVersion=current.get('relationshipVersion', 0))
            if (cursor.get('relationshipPayload') or {}).get('version', 0) <= int(current.get('relationshipVersion', 0)):
                cursor['relationshipPayload'] = None
            self._save(cursor_path, cursor, expected=current, writes=writes)
        with self._configuration_lock:
            if generation == self.refresh_configuration(role_id):
                self.failures.pop(role_id, None)
        if not self.defer_consumers:
            self._retry_pending_event(cursor_path, cursor, role_id, session_key)

    def retry_consumers(self, role_id):
        path = self.roles_root / role_id / 'memory' / '.maintenance.json'
        return self._retry_pending_event(path, self._read_cursor(path), role_id, f'role:{role_id}')

    def pending_publications(self, role_id):
        path = self.roles_root / role_id / 'memory' / '.maintenance.json'
        current = self._validated_pending_cursor(path, self._read_cursor(path), role_id, f'role:{role_id}')
        return tuple(current.get('pendingEvents', []))

    def publish_pending_event(self, role_id, entry):
        path = self.roles_root / role_id / 'memory' / '.maintenance.json'
        return self._publish_pending_entry(path, entry, role_id, f'role:{role_id}')

    def retry_relationship(self, role_id):
        path = self.roles_root / role_id / 'memory' / '.maintenance.json'
        current = self._validated_pending_cursor(path, self._read_cursor(path), role_id, f'role:{role_id}')
        return self._refresh_relationship(path, current, role_id)

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
            max_messages = self.recent_limit
        if max_messages < self.recent_limit:
            raise ValueError("max_messages must include the recent context window")
        messages = self._messages(role_id, session_key)
        cursor_path = (self.roles_root / role_id / "memory" / ".maintenance.json").resolve()
        cursor = self._context_cursor(self._read_cursor(cursor_path), messages, session_key)
        eligible = [message for message in messages if message.sequence > int(cursor["lastSequence"])]
        if len(eligible) <= max_messages:
            return False
        self.maintain(role_id, session_key, force=True)
        return True

    def ensure_coverage(self, role_id: str, source_session: str, source_ids: list[str], *, commit_guard=None) -> bool:
        """Coverage is durable Markdown progress, independent of consumer ACKs."""
        all_messages = (self.messages_provider(role_id, f'role:{role_id}') if self.messages_provider
            else self.sessions.context_messages(f'role:{role_id}'))
        if set(source_ids) - {m.id for m in all_messages}:
            return False
        source = {m.id: m for m in self._committed_messages(all_messages) if m.id in source_ids}
        messages = self._messages(role_id, f"role:{role_id}")
        eligible = {m.id: m for m in messages}
        if set(source) - set(eligible):
            return False
        if not source:
            return True
        through = max(eligible[identifier].sequence for identifier in source)
        path = self.roles_root / role_id / "memory" / ".maintenance.json"
        cursor = self._context_cursor(self._read_cursor(path), messages, f"role:{role_id}")
        if int(cursor["lastSequence"]) < through:
            self.maintain(role_id, f"role:{role_id}", force=True, through_sequence=through, commit_guard=commit_guard)
        latest = self._messages(role_id, f"role:{role_id}")
        if not self._prefix_matches([eligible[key] for key in source], latest, ordered=False):
            return False
        cursor = self._context_cursor(self._read_cursor(path), latest, f"role:{role_id}")
        return int(cursor["lastSequence"]) >= through

    @classmethod
    def _prefix_matches(cls, frozen, latest, *, ordered=True):
        if ordered:
            return cls._message_signature(frozen) == cls._message_signature(latest[:len(frozen)])
        found = {message.id: message for message in latest}
        return all(message.id in found and cls._message_signature([message]) == cls._message_signature([found[message.id]]) for message in frozen)

    @staticmethod
    def _ownership_matches(saved, current):
        return all(current.get(key) == value for key, value in saved.items())

    def _messages(self, role_id: str, session_key: str) -> list[Message]:
        messages = self.messages_provider(role_id, session_key) if self.messages_provider else self.sessions.context_messages(session_key)
        return self._committed_messages(messages)

    def refresh_recent_context(self, role_id: str, session_key: str) -> bool:
        """Refresh the current authorised raw-message view without a model call.

        This does not advance a consolidation cursor or change long-term
        facts. Readers use it after binding changes to avoid stale raw history.
        """

        if session_key != f"role:{role_id}" or not self.role_exists(role_id):
            return False
        memory_dir = MemoryDocuments(self.roles_root).memory_dir(role_id)
        path = memory_dir / "RECENT_CONTEXT.md"
        messages = self._messages(role_id, session_key)
        if not messages and not path.exists():
            return True
        recent = self._recent_context(messages)
        if self._read_text(path) == recent:
            return True
        cursor_path = memory_dir / ".maintenance.json"
        cursor = self._read_cursor(cursor_path)
        document_signature = self._file_signature(path)
        self._before_commit()
        if not self.role_exists(role_id) or self._message_signature(self._messages(role_id, session_key)) != self._message_signature(messages):
            return False
        if self._read_cursor(cursor_path) != cursor or self._file_signature(path) != document_signature:
            return False
        memory_dir.mkdir(parents=True, exist_ok=True)
        self._save(cursor_path, cursor, expected=cursor, writes={path: recent})
        return True

    @classmethod
    def _context_cursor(cls, cursor: dict[str, object], messages: list[Message], session_key: str) -> dict[str, object]:
        sources = cursor.get("sources")
        if sources:
            actual = messages[:int(sources.get('count', 0))]
            if sources != cls._prefix_stamp(actual):
                # Keep the old payload as evidence, but it no longer proves coverage.
                return {**cursor, "lastSequence": 0, "sources": [],
                        "pendingEvents": [], "relationshipPayload": None,
                        "invalidatedSources": sources}
        if not messages or messages[-1].metadata.get("contextSequenceSource") != "global-rowid" or cursor.get("sequenceSource") == "global-rowid":
            return cursor
        previous = int(cursor.get("lastSequence", 0))
        mapped = max((message.sequence for message in messages if message.sessionKey == session_key and int(message.metadata.get("sourceSequence", 0)) <= previous), default=0)
        return {**cursor, "lastSequence": mapped, "sequenceSource": "global-rowid"}

    def sync_structured_memory(self, role_id: str, memories: list[object]) -> None:
        MemoryDocuments(self.roles_root).sync_structured_memory(role_id, memories)  # type: ignore[arg-type]

    def _before_commit(self) -> None:
        """Hook for deterministic stale-snapshot verification in tests."""

    def _read_cursor(self, path: Path) -> dict[str, object]:
        role_id = path.parent.parent.name
        session_key = f"role:{role_id}"
        current = self.sessions.maintenance_progress(role_id, session_key)
        if path.exists() and not current.get("legacyImported"):
            try:
                legacy = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                legacy = {}
            # No old content fingerprint: retain the cursor for inspection, never
            # certify an unverifiable prefix merely from a sequence number.
            imported = {**current, "legacyImported": True, "legacyProgress": legacy}
            payload = legacy.get("pendingEvent") if isinstance(legacy, dict) else None
            if payload:
                # Legacy payloads did not bind their content to ownership or a
                # prefix hash. Preserve them for review/rollback, never replay
                # an unverifiable event under today's owner grant.
                imported['unverifiedLegacyEvent'] = payload
            self.sessions.save_maintenance_progress(role_id, session_key, imported, expected=current)
            current = imported
        if path.exists() and current.get("legacyImported"):
            archive = path.with_name(path.name + ".migrated")
            if not archive.exists():
                os.replace(path, archive)
            elif archive.read_bytes() == path.read_bytes():
                # Recovery after the database commit retains the original backup.
                path.unlink()
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                restored = path.with_name(path.name + '.' + digest + '.migrated')
                if restored.exists() and restored.read_bytes() == path.read_bytes():
                    path.unlink()
                else:
                    os.replace(path, restored)
        return current

    def _save(self, path, cursor, *, expected=None, writes=None):
        from .memory_optimizer import MemoryOptimizer
        role_id = path.parent.parent.name
        self.sessions.save_maintenance_progress(role_id, f"role:{role_id}", cursor, expected=expected,
            write_documents=(lambda: MemoryOptimizer.commit_documents(writes)) if writes else None)

    @staticmethod
    def _read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _recent_context(self, messages: list[Message], *, ownership=None) -> str:
        heading = "# 近期对话\n\n"
        selected: list[str] = []
        length = 0
        for message in reversed(messages[-self.recent_limit :]):
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
        return self._recent_document("", ownership or {}, raw=heading + "\n".join(selected) + ("\n" if selected else ""))

    def _recent_document(self, abstract, ownership, *, raw=None):
        if '## 最近的对话' in abstract:
            abstract, generated_raw = abstract.split('## 最近的对话', 1)
            raw = raw or '## 最近的对话' + generated_raw
        marker = "<!-- recent-context-sources: " + json.dumps(ownership, ensure_ascii=False, sort_keys=True) + " -->\n"
        return marker + abstract.strip() + "\n\n<!-- recent-raw -->\n" + (raw or "")

    def _window(self, messages: list[Message], after_sequence: int) -> list[Message]:
        eligible = [message for message in messages if message.sequence > after_sequence]
        if len(eligible) <= self.recent_limit:
            return []
        return eligible[:-self.recent_limit]

    @staticmethod
    def _committed_messages(messages: list[Message]) -> list[Message]:
        committed: list[Message] = []
        sessions: dict[str, list[Message]] = {}
        for message in messages:
            sessions.setdefault(message.sessionKey, []).append(message)
        for transcript in sessions.values():
            replies = {
                message.runId: (index, message)
                for index, message in enumerate(transcript)
                if message.runId and message.role == "assistant" and message.status == "completed"
            }
            for index, message in enumerate(transcript):
                if message.role != "user":
                    continue
                if message.runId:
                    matched = replies.get(message.runId)
                    following = matched[1] if matched is not None and matched[0] > index else None
                else:
                    # Legacy transcripts have no run association; their turns
                    # remain replayable only at the original adjacent boundary.
                    following = transcript[index + 1] if index + 1 < len(transcript) else None
                    if following is not None and following.runId is not None:
                        following = None
                if following is not None and following.role == "assistant" and following.status == "completed":
                    committed.extend((message, following))
        return sorted(committed, key=lambda message: message.sequence)

    @staticmethod
    def _message_signature(messages: list[Message]) -> list[list[object]]:
        return [[message.id, message.sessionKey, message.role, message.status,
                 message.content, message.createdAt.isoformat(),
                 json.dumps(message.metadata, ensure_ascii=False, sort_keys=True)]
                for message in messages]

    @classmethod
    def _prefix_stamp(cls, messages):
        return {'count': len(messages), 'sha256': hashlib.sha256(json.dumps(
            cls._message_signature(messages), ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}

    @staticmethod
    def _source_key(role_id: str, window: list[Message]) -> str:
        material = json.dumps(MemoryMaintenance._message_signature(window), ensure_ascii=False, sort_keys=True)
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

    def _candidates(self, source_key: str, window: list[Message], *, commit_guard=None) -> list[ConsolidationCandidate]:
        self._draft_local.draft = {}
        if self.consolidation_provider is not None:
            kwargs = {'commit_guard': commit_guard} if 'commit_guard' in inspect.signature(self.consolidation_provider).parameters else {}
            raw = self.consolidation_provider(source_key, window, **kwargs)
            draft: dict[str, str] = {}
            if isinstance(raw, dict):
                history = raw.get("history", raw.get("historyText", ""))
                recent = raw.get("recentContext", raw.get("recent", ""))
                if isinstance(history, str):
                    draft["history"] = history[:12000]
                if isinstance(recent, str):
                    draft["recent"] = recent[: self.RECENT_CHAR_LIMIT]
                if isinstance(raw.get('pending'), str):
                    draft['pending'] = raw['pending']
                draft['history_entries'] = raw.get('history_entries', [])
                draft['conversation'] = raw.get('conversation', '')
                raw = raw.get("pendingItems", raw.get("memories", []))
            if not isinstance(raw, list) or (not raw and not draft):
                raise ConsolidationDraftError("整理模型未返回有效候选")
            if draft:
                self._draft_local.draft = draft
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
        raise ConsolidationDraftError('宿主记忆整理模型未配置')

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
            'historyEntries': list(event.history_entries),
            'conversation': event.conversation,
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
            history_entries=tuple((str(item[0]), int(item[1])) for item in payload.get('historyEntries', [])),
            conversation=str(payload.get('conversation', '')),
        )

    @staticmethod
    def _file_signature(path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def _retry_pending_event(self, cursor_path, cursor, role_id, session_key):
        # Each acknowledged publication is saved before the next. Relationship
        # refresh has its own latest payload and cannot undo memory coverage.
        current = self._validated_pending_cursor(cursor_path, cursor, role_id, session_key)
        for entry in tuple(current.get('pendingEvents', [])):
            if not self._publish_pending_entry(cursor_path, entry, role_id, session_key):
                break
        return self._refresh_relationship(cursor_path, self._read_cursor(cursor_path), role_id)

    def _validated_pending_cursor(self, cursor_path, cursor, role_id, session_key):
        messages = self._messages(role_id, session_key)
        current = self._context_cursor(cursor, messages, session_key)
        ownership = self.ownership_provider(role_id) if self.ownership_provider else {}
        if (not self.role_exists(role_id) or any(ownership.get(key) != value for key, value in cursor.get('ownership', {}).items())):
            current = {**current, 'lastSequence': 0, 'sources': {}, 'pendingEvents': [], 'relationshipPayload': None,
                       'invalidatedProgress': cursor}
        if current != cursor:
            self._save(cursor_path, current, expected=cursor)
        return current

    def _publish_pending_entry(self, cursor_path, entry, role_id, session_key):
        current = self._validated_pending_cursor(cursor_path, self._read_cursor(cursor_path), role_id, session_key)
        if entry not in current.get('pendingEvents', []):
            return True
        if self.on_consolidation_committed is None:
            return False
        try:
            event = self._event_from_payload(entry['payload'])
            if event.role_id != role_id or event.session_key != session_key:
                raise ValueError('整理事件作用域与当前角色不匹配')
            self.on_consolidation_committed(event)
        except Exception as error:
            self.errors.append(f'{role_id}: publication pending: {error}')
            return False
        with document_lock(cursor_path.parent):
            current = self._read_cursor(cursor_path)
            if entry in current.get('pendingEvents', []):
                updated = {**current, 'publishedVersion': max(int(current.get('publishedVersion', 0)), entry['version']),
                           'pendingEvents': [item for item in current.get('pendingEvents', []) if item['version'] > entry['version']]}
                self._save(cursor_path, updated, expected=current)
        return True

    def _refresh_relationship(self, cursor_path, current, role_id):
        entry = current.get("relationshipPayload")
        if entry and int(current.get("publishedVersion", 0)) >= entry["version"]:
            try:
                if self.relationship_consumer is not None:
                    self.relationship_consumer(self._event_from_payload(entry["payload"]))
                with document_lock(cursor_path.parent):
                    current = self._read_cursor(cursor_path)
                    updated = {**current, "relationshipVersion": max(int(current.get('relationshipVersion', 0)), entry["version"])}
                    if current.get('relationshipPayload') == entry:
                        updated['relationshipPayload'] = None
                    self._save(cursor_path, updated, expected=current)
                    current = updated
            except Exception as error:
                self.errors.append(f"{role_id}: relationship pending: {error}")
        return current

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
