from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterable
from datetime import datetime

from .memory_store import MemoryStore
from .models import MemoryItem, MemorySourceRef, Message
from .embeddings import EmbeddingProvider
from .memory_maintenance import MemoryMaintenance
from .memory_optimizer import MemoryOptimizerWorker
from .memory_events import TurnCommitted


class MemoryService:
    """Role-scoped memory ingestion and retrieval facade."""

    POST_RESPONSE_MAX_CANDIDATES = 8

    def __init__(self, store: MemoryStore, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.store = store
        self.embedding_provider = embedding_provider
        self.embedding_errors: list[str] = []

    def index_embedding(self, role_id: str, item: MemoryItem) -> None:
        if self.embedding_provider is None:
            return
        try:
            vector = self.embedding_provider.embed(role_id, item.summary)
            if vector is not None:
                self.store.set_embedding(role_id, item.id, vector)
        except Exception:
            self.embedding_errors.append(f"{role_id}: embedding 写入失败")

    def remember(
        self,
        role_id: str,
        summary: str,
        memory_type: str,
        *,
        session_key: str | None = None,
        message_ids: Iterable[str] = (),
        happened_at: datetime | None = None,
        stable_source_key: str | None = None,
        supersede_key: str | None = None,
    ) -> MemoryItem:
        source = MemorySourceRef(
            kind="message" if message_ids else "manual",
            sessionKey=session_key or f"role:{role_id}",
            messageIds=list(message_ids),
            stableSourceKey=stable_source_key or f"manual:{uuid.uuid4().hex}",
        )
        extra = {"supersedeKey": supersede_key} if supersede_key else {}
        item = self.store.add_or_reinforce(
            role_id,
            memory_type,
            summary,
            source,
            extra=extra,
            happened_at=happened_at,
            supersede_key=supersede_key,
        )
        self.index_embedding(role_id, item)
        return item

    def process_turn(
        self,
        role_id: str,
        session_key: str,
        user_message: Message,
        assistant_message: Message,
    ) -> list[MemoryItem]:
        if assistant_message.role != "assistant" or assistant_message.status != "completed":
            return []
        if user_message.role != "user":
            return []
        if user_message.sessionKey != session_key or assistant_message.sessionKey != session_key:
            raise ValueError("回合消息与会话不匹配")
        source_key = f"turn:{session_key}:{user_message.id}:{assistant_message.id}"
        source = MemorySourceRef(
            kind="turn",
            sessionKey=session_key,
            messageIds=[user_message.id, assistant_message.id],
            messageRange=(user_message.sequence, assistant_message.sequence),
            stableSourceKey=source_key,
        )
        forget_target = self._forget_target(user_message.content)
        if forget_target is not None:
            forgotten = self.forget_matching(role_id, forget_target)
            return [forgotten] if forgotten is not None else []
        reject_target = self._reject_target(user_message.content)
        if reject_target is not None:
            return [self.reject_matching(role_id, reject_target, source)]
        explicit = self._extract_explicit(user_message.content)
        extracted = self._extract(user_message.content)
        saved: list[MemoryItem] = []
        correction_target = self._correction_target(user_message.content)
        if correction_target is not None:
            previous = self.store.find_status_candidate(role_id, correction_target, ("active",))
            if previous is not None and previous.memoryType == "preference":
                saved.append(self.store.set_status(role_id, previous.id, "superseded"))
        protected: set[tuple[str, str]] = set()
        for memory_type, summary, supersede_key in explicit:
            extra = {"explicit": True, "protectedTurn": source_key}
            if supersede_key:
                extra["supersedeKey"] = supersede_key
            item = self.store.add_or_reinforce(
                role_id,
                memory_type,
                summary,
                source,
                extra=extra,
                happened_at=user_message.createdAt,
                supersede_key=supersede_key,
            )
            self.index_embedding(role_id, item)
            saved.append(item)
            protected.add((memory_type, summary.casefold()))
        for memory_type, summary, supersede_key in extracted:
            if (memory_type, summary.casefold()) in protected:
                continue
            extra = {"supersedeKey": supersede_key} if supersede_key else {}
            item = self.store.add_or_reinforce(
                role_id,
                memory_type,
                summary,
                source,
                extra=extra,
                happened_at=user_message.createdAt,
                supersede_key=supersede_key,
            )
            self.index_embedding(role_id, item)
            saved.append(item)
        return saved

    def forget_matching(self, role_id: str, target: str) -> MemoryItem | None:
        item = self.store.find_status_candidate(role_id, target, ("active", "rejected"))
        if item is None:
            return None
        return self.store.set_status(role_id, item.id, "forgotten")

    def reject_matching(self, role_id: str, target: str, source: MemorySourceRef) -> MemoryItem:
        item = self.store.find_status_candidate(role_id, target, ("active", "rejected", "forgotten"))
        if item is not None:
            return self.store.set_status(role_id, item.id, "rejected")
        created = self.store.add_or_reinforce(
            role_id,
            "fact",
            target,
            source,
            status="rejected",
        )
        return created

    def recall(self, role_id: str, query: str, limit: int = 8) -> list[MemoryItem]:
        if self.embedding_provider is None:
            return self.store.query(role_id, query, limit=limit)
        try:
            vector = self.embedding_provider.embed(role_id, query)
        except Exception:
            self.embedding_errors.append(f"{role_id}: embedding 查询失败")
            vector = None
        dimension_changed = False
        if vector is not None:
            try:
                dimension_changed = self.store.observe_query_embedding(role_id, vector)
            except Exception:
                self.embedding_errors.append(f"{role_id}: embedding 空间更新失败")
                vector = None
        if not dimension_changed:
            for item in self.store.list_without_embeddings(role_id):
                self.index_embedding(role_id, item)
        return self.store.query_hybrid(role_id, query, vector, limit=limit) if vector is not None else self.store.query(role_id, query, limit=limit)

    async def recall_async(self, role_id: str, query: str, limit: int = 8) -> list[MemoryItem]:
        if self.embedding_provider is None:
            return await asyncio.to_thread(self.store.query, role_id, query, limit)
        lexical_task = asyncio.to_thread(self.store.query, role_id, query, limit)
        vector_task = asyncio.to_thread(self.embedding_provider.embed, role_id, query)
        lexical_result, vector_result = await asyncio.gather(lexical_task, vector_task, return_exceptions=True)
        lexical = lexical_result if isinstance(lexical_result, list) else []
        vector = vector_result if isinstance(vector_result, list) else None
        if isinstance(vector_result, BaseException):
            self.embedding_errors.append(f"{role_id}: embedding 查询失败")
        if vector is None:
            return lexical
        try:
            dimension_changed = await asyncio.to_thread(self.store.observe_query_embedding, role_id, vector)
        except Exception:
            self.embedding_errors.append(f"{role_id}: embedding 空间更新失败")
            return lexical
        if dimension_changed:
            return await asyncio.to_thread(self.store.query_hybrid, role_id, query, vector, limit)
        missing = await asyncio.to_thread(self.store.list_without_embeddings, role_id, 8)
        async def index(item: MemoryItem) -> None:
            try:
                item_vector = await asyncio.to_thread(self.embedding_provider.embed, role_id, item.summary)
                if item_vector is not None:
                    await asyncio.to_thread(self.store.set_embedding, role_id, item.id, item_vector)
            except Exception:
                self.embedding_errors.append(f"{role_id}: embedding 回填失败")
        await asyncio.gather(*(index(item) for item in missing))
        return await asyncio.to_thread(self.store.query_hybrid, role_id, query, vector, limit)

    @staticmethod
    def context_block(memories: list[MemoryItem], fixed_context: str = "", max_chars: int = 4000) -> str:
        if max_chars <= 0:
            return ""

        lines: list[str] = []
        length = 0

        def append_line(line: str) -> bool:
            nonlocal length
            line = line.strip()
            if not line:
                return True
            added = len(line) if not lines else len(line) + 1
            if length + added > max_chars:
                return False
            lines.append(line)
            length += added
            return True

        for line in fixed_context.strip().splitlines():
            if not append_line(line):
                break

        memory_lines: list[str] = []
        type_counts: dict[str, int] = {}
        for memory in memories:
            if type_counts.get(memory.memoryType, 0) >= 3:
                continue
            line = f"- [{memory.memoryType}] {memory.summary}"
            memory_lines.append(line)
            type_counts[memory.memoryType] = type_counts.get(memory.memoryType, 0) + 1
        if memory_lines:
            heading = "以下是当前角色已保存、仅供本轮理解使用的记忆："
            before_heading = len(lines)
            before_length = length
            if append_line(heading):
                added_memory = 0
                for line in memory_lines:
                    if append_line(line):
                        added_memory += 1
                if added_memory == 0:
                    del lines[before_heading:]
                    length = before_length
        return "\n".join(lines)

    @staticmethod
    def _extract(content: str) -> list[tuple[str, str, str | None]]:
        text = re.sub(r"\s+", " ", content).strip()
        if not text:
            return []
        if MemoryService._forget_target(text) or MemoryService._reject_target(text):
            return []
        results: list[tuple[str, str, str | None]] = []
        seen: set[tuple[str, str]] = set()

        def add(memory_type: str, summary: str, supersede_key: str | None = None) -> None:
            summary = summary.strip(" \t\r\n，。！？!?,.:：；;")
            if not summary or len(summary) > 500:
                return
            key = (memory_type, summary.casefold())
            if key not in seen:
                seen.add(key)
                results.append((memory_type, summary, supersede_key))

        explicit = re.search(r"(?:请|请你)?记住(?:我|我的)?[：:，, ]*(.+)$", text, re.IGNORECASE)
        if explicit:
            explicit_summary = explicit.group(1)
            explicit_type = "preference" if re.match(r"(?:喜欢|不喜欢|偏好)", explicit_summary.strip()) else "fact"
            add(explicit_type, explicit_summary)

        changed = re.search(r"(?:以前|原来)(?:喜欢|偏好)[：:，, ]*(.+?)[，, ]*(?:现在|改为|改成|换成)(?:喜欢|偏好)?[：:，, ]*(.+)$", text)
        if changed:
            add("preference", f"喜欢{changed.group(2)}", "preference")
        elif not explicit:
            preference = re.search(r"(?:我|本人)(?:一直|平时|更|最)?喜欢(?:吃|喝|看|用)?[：:，, ]*(.+)$", text)
            if preference:
                add("preference", f"喜欢{preference.group(1)}")
            dislike = re.search(r"(?:我|本人)(?:一直)?不喜欢[：:，, ]*(.+)$", text)
            if dislike:
                add("preference", f"不喜欢{dislike.group(1)}")

        identity = re.search(r"我(?:叫|的名字是)[：:，, ]*(.+)$", text)
        if identity:
            add("profile", f"名字是{identity.group(1)}")

        procedure = re.search(r"(?:以后|今后|请始终|请都)[：:，, ]*(.+)$", text)
        if procedure:
            add("procedure", procedure.group(1))

        temporal_event = re.search(r"(?:今天|昨天|前天|刚才|上周|上个月|去年|曾经|那天)(?:我|我们)?(.+)$", text)
        if temporal_event and not explicit:
            add("event", temporal_event.group(0))
        elif not explicit and not results and re.search(r"(?:我|我们)(?:在|去了|参加了|完成了|遇到|经历了)(.+)$", text):
            add("fact", text)
        return results[: MemoryService.POST_RESPONSE_MAX_CANDIDATES]

    @staticmethod
    def _extract_explicit(content: str) -> list[tuple[str, str, str | None]]:
        """Return only user-directed remember candidates for turn protection."""
        text = re.sub(r"\s+", " ", content).strip()
        if not text:
            return []
        match = re.search(r"(?:请|请你)?记住(?:我|我的)?[：:，, ]*(.+)$", text, re.IGNORECASE)
        if not match:
            return []
        summary = match.group(1).strip(" \t\r\n，。！？!?,.:：；;")
        memory_type = "preference" if re.match(r"(?:喜欢|不喜欢|偏好)", summary) else "fact"
        return [(memory_type, summary, None)] if summary and len(summary) <= 500 else []

    @staticmethod
    def _forget_target(content: str) -> str | None:
        match = re.search(r"^(?:请|请你)?(?:忘记|忘了|不要再记得|删除记忆)[：:，,\s]*(.+)$", content.strip(), re.IGNORECASE)
        target = match.group(1).strip(" \t\r\n，。！？!?,.:：；;") if match else ""
        return re.sub(r"^(?:我|我的)", "", target).strip() or None

    @staticmethod
    def _reject_target(content: str) -> str | None:
        match = re.search(r"^(?:请|请你)?(?:拒绝记忆|不要记住|别记住|不需要记住)[：:，,\s]*(.+)$", content.strip(), re.IGNORECASE)
        target = match.group(1).strip(" \t\r\n，。！？!?,.:：；;") if match else ""
        return re.sub(r"^(?:我|我的)", "", target).strip() or None

    @staticmethod
    def _correction_target(content: str) -> str | None:
        match = re.search(
            r"(?:以前|原来)(?:喜欢|偏好)[：:，, ]*(.+?)[，, ]*(?:现在|改为|改成|换成)",
            re.sub(r"\s+", " ", content).strip(),
        )
        if not match:
            return None
        old = match.group(1).strip(" \t\r\n，。！？!?,.:：；;")
        return f"喜欢{old}" if old else None

    @staticmethod
    def extract_candidates(content: str) -> list[tuple[str, str, str | None]]:
        """Compatibility entry point used by Markdown memory maintenance."""
        return MemoryService._extract(content)


class MemoryWorker:
    """In-process queue with one serial lane per role."""

    def __init__(
        self,
        service: MemoryService,
        maintenance: MemoryMaintenance | None = None,
        optimizer: MemoryOptimizerWorker | None = None,
    ) -> None:
        self.service = service
        self.maintenance = maintenance
        self.optimizer = optimizer
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[asyncio.Task[object], str] = {}
        self._deleting: set[str] = set()
        self.errors: list[str] = []
        self.closed = False

    def start(self) -> None:
        self.closed = False
        if self.optimizer is not None:
            self.optimizer.start()

    def submit(self, role_id: str, session_key: str, user_message: Message, assistant_message: Message) -> bool:
        return self.publish(TurnCommitted(role_id, session_key, user_message, assistant_message))

    def publish(self, event: TurnCommitted) -> bool:
        role_id = event.role_id
        session_key = event.session_key
        user_message = event.user_message
        assistant_message = event.assistant_message
        if (
            self.closed
            or role_id in self._deleting
            or user_message.role != "user"
            or assistant_message.role != "assistant"
            or assistant_message.status != "completed"
            or user_message.sessionKey != session_key
            or assistant_message.sessionKey != session_key
            or session_key != f"role:{role_id}"
        ):
            return False
        task = asyncio.create_task(self._run(role_id, session_key, user_message, assistant_message))
        self._tasks[task] = role_id
        task.add_done_callback(self._forget_task)
        return True

    def _forget_task(self, task: asyncio.Task[object]) -> None:
        self._tasks.pop(task, None)

    async def begin_role_deletion(self, role_id: str) -> None:
        """Stop accepting maintenance and wait for in-flight work for a role."""
        self._deleting.add(role_id)
        tasks = tuple(task for task, task_role in self._tasks.items() if task_role == role_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def end_role_deletion(self, role_id: str) -> None:
        self._deleting.discard(role_id)

    async def _run(self, role_id: str, session_key: str, user_message: Message, assistant_message: Message) -> None:
        lock = self.optimizer.lock_for(role_id) if self.optimizer is not None else self._locks.setdefault(role_id, asyncio.Lock())
        async with lock:
            semantic_task = asyncio.create_task(asyncio.to_thread(
                self._process_semantic,
                role_id,
                session_key,
                user_message,
                assistant_message,
            ))
            maintenance_task = asyncio.create_task(asyncio.to_thread(
                self._process_maintenance,
                role_id,
                session_key,
            ))
            semantic_result, maintenance_result = await asyncio.gather(
                semantic_task, maintenance_task, return_exceptions=True
            )
            if isinstance(semantic_result, BaseException):
                self.errors.append(f"{role_id}: semantic post-response failed: {semantic_result}")
            if isinstance(maintenance_result, BaseException):
                self.errors.append(f"{role_id}: markdown maintenance failed: {maintenance_result}")
            if (
                not isinstance(semantic_result, BaseException)
                and self.maintenance is not None
                and any(item.status in {"forgotten", "rejected", "superseded"} for item in semantic_result)
            ):
                try:
                    self.maintenance.sync_structured_memory(role_id, self.service.store.list_all(role_id))
                except Exception as error:
                    self.errors.append(f"{role_id}: structured Markdown sync failed: {error}")

    def _process_semantic(self, role_id: str, session_key: str, user_message: Message, assistant_message: Message) -> list[MemoryItem]:
        return self.service.process_turn(role_id, session_key, user_message, assistant_message)

    def _process_maintenance(self, role_id: str, session_key: str) -> None:
        if self.maintenance is not None:
            self.maintenance.maintain(role_id, session_key)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self.optimizer is not None:
            await self.optimizer.drain()

    async def close(self) -> None:
        self.closed = True
        if self.optimizer is not None:
            await self.optimizer.close()
        await self.drain()
