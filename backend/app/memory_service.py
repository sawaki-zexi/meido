from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime

from .memory_store import MemoryStore
from .models import MemoryItem, MemorySourceRef, Message
from .embeddings import EmbeddingProvider
from .memory_maintenance import MemoryMaintenance
from .memory_optimizer import MemoryOptimizerWorker
from .memory_events import TurnCommitted, TurnIngested, MemoryWritten


class MemoryService:
    """Role-scoped memory ingestion and retrieval facade."""

    def __init__(
        self,
        store: MemoryStore,
        embedding_provider: EmbeddingProvider | None = None,
        post_response_provider: Callable[..., object] | None = None,
        event_bus: object | None = None,
        *,
        implicit_extraction_enabled: bool = True,
        memory_enabled: Callable[[], bool] | None = None,
        auxiliary_provider: Callable[..., str] | None = None,
    ) -> None:
        self.store = store
        self.embedding_provider = embedding_provider
        self.post_response_provider = post_response_provider
        self.event_bus = event_bus
        self.implicit_extraction_enabled = implicit_extraction_enabled
        self.memory_enabled = memory_enabled or (lambda: True)
        self.auxiliary_provider = auxiliary_provider
        self.embedding_errors: list[str] = []
        self.post_response_errors: list[str] = []

    def _embedding_session(self):
        provider = self.embedding_provider
        if provider is None:
            return None, None
        snapshot = getattr(provider, "snapshot", None)
        active_provider = snapshot() if callable(snapshot) else provider
        return active_provider, getattr(active_provider, "model_id", None)

    def index_embedding(self, role_id: str, item: MemoryItem) -> None:
        if self.embedding_provider is None:
            return
        if not getattr(self.embedding_provider, "is_enabled", True):
            return
        try:
            provider, model_id = self._embedding_session()
            vector = provider.embed(role_id, item.summary) if provider is not None else None
            if vector is not None:
                self.store.set_embedding(role_id, item.id, vector, model_id)
        except Exception:
            self.embedding_errors.append(f"{role_id}: embedding 写入失败")
            self._publish_written(
                role_id,
                item.sourceRef.sessionKey,
                item.sourceRef.stableSourceKey,
                [item.id],
                "embedding",
                error="embedding 写入失败",
            )

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
        vector, model_id = self.embed_for_write(role_id, summary)
        item = self.store.add_or_reinforce(
            role_id,
            memory_type,
            summary,
            source,
            extra=extra,
            happened_at=happened_at,
            supersede_key=supersede_key,
        )
        self.store.set_embedding(role_id, item.id, vector, model_id)
        return self.store.get(role_id, item.id)

    def process_turn(
        self,
        role_id: str,
        session_key: str,
        user_message: Message,
        assistant_message: Message,
        *,
        protected_item_ids: Iterable[str] = (),
        tool_metadata: dict[str, object] | None = None,
    ) -> list[MemoryItem]:
        if not self.availability(role_id)[0]:
            return []
        if assistant_message.role != "assistant" or assistant_message.status != "completed" or user_message.role != "user":
            return []
        if user_message.sessionKey != session_key or assistant_message.sessionKey != session_key:
            raise ValueError("回合消息与会话不匹配")
        from .memory_writes import post_response
        return asyncio.run(post_response(self, role_id, session_key, user_message, assistant_message,
                                         set(protected_item_ids), tool_metadata))

    def _publish_written(self, role_id: str, session_key: str, source_key: str, ids: list[str], operation: str, error: str | None = None) -> None:
        if self.event_bus is None:
            return
        try:
            from .memory_events import MemoryWritten
            self.event_bus.publish(MemoryWritten(role_id, session_key, source_key, tuple(ids), operation, error))
        except Exception:
            return

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

    def availability(self, role_id=None):
        if not self.memory_enabled():
            return False, "语义记忆引擎已关闭"
        provider, model_id = self._embedding_session()
        if provider is None or not getattr(provider, "is_enabled", True) or not getattr(provider, "ready", True):
            return False, "语义记忆引擎缺少独立向量配置"
        try:
            if role_id is not None:
                self.store.validate_embedding_space(role_id, getattr(provider, "dimensions", None), model_id)
            return True, ""
        except ValueError as error:
            return False, str(error)

    def embed_for_write(self, role_id, text):
        return asyncio.run(self.embed_for_write_async(role_id, text))

    async def embed_for_write_async(self, role_id, text):
        ready, reason = self.availability(role_id)
        if not ready:
            raise ValueError(reason)
        provider, model_id = self._embedding_session()
        embed_async = getattr(provider, 'embed_async', None)
        vector = (await embed_async(role_id, text) if embed_async is not None
            else await asyncio.to_thread(provider.embed, role_id, text))
        if not vector:
            raise ValueError("记忆向量写入失败")
        self.store.validate_embedding_space(role_id, len(vector), model_id)
        return vector, model_id

    def recall(self, role_id: str, query: str, limit: int = 8) -> list[MemoryItem]:
        if not self.availability(role_id)[0]:
            return []
        provider, model_id = self._embedding_session()
        try:
            vector = provider.embed(role_id, query)
            if vector is not None:
                self.store.validate_embedding_space(role_id, len(vector), model_id)
        except Exception:
            vector = None
        return self.store.query_hybrid(role_id, query, vector, limit)

    async def recall_async(self, role_id: str, query: str, limit: int = 8) -> list[MemoryItem]:
        if not await asyncio.to_thread(lambda: self.availability(role_id)[0]):
            return []
        provider, model_id = self._embedding_session()
        try:
            vector = await asyncio.wait_for(asyncio.to_thread(provider.embed, role_id, query), timeout=8)
            if vector:
                await asyncio.to_thread(self.store.validate_embedding_space, role_id, len(vector), model_id)
        except Exception:
            self.embedding_errors.append(f"{role_id}: embedding 查询失败")
            vector = None
        return await asyncio.to_thread(self.store.query_hybrid, role_id, query, vector, limit)

    async def recall_vector_async(self, role_id: str, query: str, limit: int = 8) -> list[MemoryItem]:
        if not await asyncio.to_thread(lambda: self.availability(role_id)[0]):
            return []
        provider, model_id = self._embedding_session()
        try:
            vector = await asyncio.wait_for(asyncio.to_thread(provider.embed, role_id, query), timeout=8)
            if not vector:
                return []
            await asyncio.to_thread(self.store.validate_embedding_space, role_id, len(vector), model_id)
            return await asyncio.to_thread(self.store.query_hybrid, role_id, "", vector, limit)
        except Exception:
            return []

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
        self._semantic_locks: dict[str, asyncio.Lock] = {}
        self._maintenance_roles: set[str] = set()
        self._consumer_roles: set[str] = set()
        self._deleting: set[str] = set()
        self._deleted: set[str] = set()
        self.errors: list[str] = []
        self.closed = False

    def start(self) -> None:
        self.closed = False
        if self.optimizer is not None:
            self.optimizer.start()

    def submit(
        self,
        role_id: str,
        session_key: str,
        user_message: Message,
        assistant_message: Message,
        *,
        explicit_memory_ids: tuple[str, ...] = (),
        tool_metadata: dict[str, object] | None = None,
    ) -> bool:
        return self.publish(TurnCommitted(
            role_id, session_key, user_message, assistant_message,
            tuple(dict.fromkeys(explicit_memory_ids)), tool_metadata,
        ))

    def role_lock_for(self, role_id: str) -> asyncio.Lock:
        """The session Markdown lane; semantic work and optimizer own separate locks."""
        return self._locks.setdefault(role_id, asyncio.Lock())

    def role_deletion_blocked(self, role_id: str) -> bool:
        return role_id in self._deleting or role_id in self._deleted

    def publish(self, event: TurnCommitted) -> bool:
        role_id = event.role_id
        session_key = event.session_key
        user_message = event.user_message
        assistant_message = event.assistant_message
        if (
            self.closed
            or role_id in self._deleting
            or role_id in self._deleted
            or user_message.role != "user"
            or assistant_message.role != "assistant"
            or assistant_message.status != "completed"
            or user_message.sessionKey != session_key
            or assistant_message.sessionKey != session_key
            or session_key != f"role:{role_id}"
        ):
            return False
        if self.service.event_bus is not None:
            try:
                self.service.event_bus.publish(event)
            except Exception as error:
                self.errors.append(f"{role_id}: TurnCommitted observation failed: {error}")
        task = asyncio.create_task(self._run(
            role_id,
            session_key,
            user_message,
            assistant_message,
            event.explicit_memory_ids,
            event.tool_metadata,
        ))
        self._tasks[task] = role_id
        task.add_done_callback(self._forget_task)
        return True

    def _forget_task(self, task: asyncio.Task[object]) -> None:
        self._tasks.pop(task, None)

    def submit_owner_channel(self, role_id, session_key, user, assistant, *, private):
        """Called only after the channel host verifies the owner and delivery."""
        if self.closed or self.role_deletion_blocked(role_id):
            return False
        async def run():
            work = [self._semantic(self._process_semantic, role_id, session_key, user, assistant)]
            if private:
                work.append(self._maintain(self._process_maintenance, role_id, f'role:{role_id}'))
            results = await asyncio.gather(*work, return_exceptions=True)
            self.errors.extend(f'{role_id}: channel memory failed: {item}' for item in results if isinstance(item, BaseException))
        task = asyncio.create_task(run())
        self._tasks[task] = role_id
        task.add_done_callback(self._forget_task)
        return True

    async def begin_role_deletion(self, role_id: str) -> None:
        """Stop accepting maintenance and wait for in-flight work for a role."""
        self._deleting.add(role_id)
        if self.optimizer is not None:
            await self.optimizer.begin_role_deletion(role_id)
        tasks = tuple(task for task, task_role in self._tasks.items() if task_role == role_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def end_role_deletion(self, role_id: str, *, deleted: bool = False) -> None:
        self._deleting.discard(role_id)
        if deleted:
            self._deleted.add(role_id)
        if self.optimizer is not None:
            self.optimizer.end_role_deletion(role_id, deleted=deleted)

    async def _run(
        self,
        role_id: str,
        session_key: str,
        user_message: Message,
        assistant_message: Message,
        explicit_memory_ids: tuple[str, ...] = (),
        tool_metadata: dict[str, object] | None = None,
    ) -> None:
        semantic_task = asyncio.create_task(self._semantic(
            self._process_semantic,
            role_id,
            session_key,
            user_message,
            assistant_message,
            explicit_memory_ids,
            tool_metadata,
        ))
        maintenance_task = asyncio.create_task(self._maintain(
            self._process_maintenance,
            role_id,
            session_key,
        ))
        semantic_result, maintenance_result = await asyncio.gather(
            semantic_task, maintenance_task, return_exceptions=True
        )
        if isinstance(semantic_result, BaseException):
            self.errors.append(f"{role_id}: semantic post-response failed: {semantic_result}")
            if self.service.event_bus is not None:
                self.service.event_bus.publish(MemoryWritten(
                    role_id,
                    session_key,
                    f"turn:{session_key}:{user_message.id}:{assistant_message.id}",
                    operation="post_response",
                    error=str(semantic_result),
                ))
                self.service.event_bus.publish(TurnIngested(
                    role_id,
                    session_key,
                    f"turn:{session_key}:{user_message.id}:{assistant_message.id}",
                    implicit=self.service.implicit_extraction_enabled,
                    error=str(semantic_result),
                ))
        elif self.service.event_bus is not None:
            try:
                self.service.event_bus.publish(TurnIngested(
                    role_id,
                    session_key,
                    f"turn:{session_key}:{user_message.id}:{assistant_message.id}",
                    tuple(item.id for item in semantic_result),
                    implicit=self.service.implicit_extraction_enabled,
                    tool_metadata=tool_metadata,
                ))
            except Exception as error:
                self.errors.append(f"{role_id}: TurnIngested observation failed: {error}")
        if isinstance(maintenance_result, BaseException):
            self.errors.append(f"{role_id}: markdown maintenance failed: {maintenance_result}")

    async def _semantic(self, operation, role_id, *args):
        async with self._semantic_locks.setdefault(role_id, asyncio.Lock()):
            return await asyncio.to_thread(operation, role_id, *args)

    async def _maintain(self, operation, role_id, *args):
        from .channels.consolidation import finish_io
        from .memory_document_lock import MarkdownCommitGuard
        guard = MarkdownCommitGuard()
        async with self.role_lock_for(role_id):
            try:
                result = await asyncio.to_thread(operation, role_id, *args, commit_guard=guard)
            except asyncio.CancelledError:
                await finish_io(asyncio.to_thread(guard.cancel))
                raise
        self.retry_consumers(role_id)
        return result

    def retry_consumers(self, role_id):
        if self.maintenance is None or self.closed or self.role_deletion_blocked(role_id) or role_id in self._consumer_roles:
            return
        self._consumer_roles.add(role_id)
        async def run():
            try:
                from .channels.consolidation import finish_io
                await finish_io(asyncio.to_thread(self.maintenance.retry_consumers, role_id))
            except Exception as error:
                self.errors.append(f'{role_id}: memory consumers pending: {error}')
            finally:
                self._consumer_roles.discard(role_id)
        task = asyncio.create_task(run())
        self._tasks[task] = role_id
        task.add_done_callback(self._forget_task)

    async def before_turn(self, role_id, session_key):
        """Queue normal backlog; only a known failed extraction blocks entry."""
        if self.maintenance is None or self.closed or self.role_deletion_blocked(role_id):
            return
        self.maintenance.refresh_configuration(role_id)
        def due():
            messages = self.maintenance._messages(role_id, session_key)
            path = self.maintenance.roles_root / role_id / "memory" / ".maintenance.json"
            cursor = self.maintenance._context_cursor(self.maintenance._read_cursor(path), messages, session_key)
            count = sum(m.sequence > int(cursor["lastSequence"]) for m in messages)
            return count >= self.maintenance.recent_limit + self.maintenance.minimum_new_messages
        if role_id in self._maintenance_roles or not await asyncio.to_thread(due):
            return
        if role_id in self.maintenance.failures:
            raise ValueError("记忆整理失败：" + (self.maintenance.failures[role_id] or '整理请求失败'))
        self._maintenance_roles.add(role_id)
        async def run():
            try:
                await self._maintain(self._process_maintenance, role_id, session_key)
            except Exception as error:
                self.errors.append(f"{role_id}: markdown maintenance failed: {error}")
            finally:
                self._maintenance_roles.discard(role_id)
        task = asyncio.create_task(run())
        self._tasks[task] = role_id
        task.add_done_callback(self._forget_task)

    def _process_semantic(
        self,
        role_id: str,
        session_key: str,
        user_message: Message,
        assistant_message: Message,
        explicit_memory_ids: tuple[str, ...] = (),
        tool_metadata: dict[str, object] | None = None,
    ) -> list[MemoryItem]:
        return self.service.process_turn(
            role_id,
            session_key,
            user_message,
            assistant_message,
            protected_item_ids=explicit_memory_ids,
            tool_metadata=tool_metadata,
        )

    def _process_maintenance(self, role_id: str, session_key: str, *, commit_guard=None) -> None:
        if self.maintenance is not None:
            self.maintenance.maintain(role_id, session_key, commit_guard=commit_guard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self.optimizer is not None:
            await self.optimizer.drain()

    async def close(self) -> None:
        self.closed = True
        if self.optimizer is not None:
            await self.optimizer.close()
        await self.drain()
