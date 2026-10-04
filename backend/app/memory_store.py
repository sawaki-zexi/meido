from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import MemoryItem, MemoryOrigin, MemorySourceRef


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_summary(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def content_hash(value: str) -> str:
    return hashlib.sha256(normalize_summary(value).casefold().encode("utf-8")).hexdigest()


class MemoryStore:
    """SQLite authority for role-scoped structured memories."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version > 1:
                raise RuntimeError(f"memory database schema {schema_version} is newer than this application")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    role_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    source_ref TEXT NOT NULL,
                    happened_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reinforcement INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL,
                    emotional_weight INTEGER NOT NULL DEFAULT 0,
                    embedding_json TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS semantic_memory (
                    id TEXT PRIMARY KEY,
                    role_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_items)")}
            if "embedding_json" not in columns:
                connection.execute("ALTER TABLE memory_items ADD COLUMN embedding_json TEXT")
            if "emotional_weight" not in columns:
                connection.execute("ALTER TABLE memory_items ADD COLUMN emotional_weight INTEGER NOT NULL DEFAULT 0")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS memory_items_role_type_hash
                ON memory_items(role_id, memory_type, content_hash)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS memory_items_role_status
                ON memory_items(role_id, status, updated_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consolidation_events (
                    role_id TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    item_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (role_id, source_ref)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_replacements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role_id TEXT NOT NULL,
                    old_item_id TEXT NOT NULL,
                    old_memory_type TEXT NOT NULL,
                    old_summary TEXT NOT NULL,
                    old_source_ref TEXT,
                    old_happened_at TEXT,
                    old_extra_json TEXT,
                    new_item_id TEXT NOT NULL,
                    new_memory_type TEXT NOT NULL,
                    new_summary TEXT NOT NULL,
                    new_source_ref TEXT,
                    new_happened_at TEXT,
                    new_extra_json TEXT,
                    relation_type TEXT NOT NULL DEFAULT 'supersede',
                    source_ref TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS memory_replacements_role_old ON memory_replacements(role_id, old_item_id, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS memory_replacements_role_new ON memory_replacements(role_id, new_item_id, created_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_embedding_spaces (
                    role_id TEXT PRIMARY KEY,
                    dimension INTEGER NOT NULL CHECK (dimension > 0)
                )
                """
            )
            if schema_version < 1:
                self._migrate_embedding_dimensions(connection)
            connection.execute("PRAGMA user_version = 1")

    @staticmethod
    def _migrate_embedding_dimensions(connection: sqlite3.Connection) -> None:
        role_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT role_id FROM memory_items WHERE embedding_json IS NOT NULL"
            )
        ]
        for role_id in role_ids:
            rows = connection.execute(
                "SELECT embedding_json FROM memory_items WHERE role_id = ? AND embedding_json IS NOT NULL",
                (role_id,),
            ).fetchall()
            dimensions: set[int] = set()
            invalid = False
            for row in rows:
                vector = MemoryStore._decode_json(row[0], None)
                if not isinstance(vector, list) or not vector:
                    invalid = True
                    break
                dimensions.add(len(vector))
            if invalid or len(dimensions) != 1:
                connection.execute("UPDATE memory_items SET embedding_json = NULL WHERE role_id = ?", (role_id,))
                connection.execute("DELETE FROM memory_embedding_spaces WHERE role_id = ?", (role_id,))
                continue
            connection.execute(
                "INSERT INTO memory_embedding_spaces(role_id, dimension) VALUES (?, ?) "
                "ON CONFLICT(role_id) DO UPDATE SET dimension = excluded.dimension",
                (role_id, dimensions.pop()),
            )

    @staticmethod
    def _require_role_id(role_id: str) -> None:
        if not isinstance(role_id, str) or not role_id.strip():
            raise ValueError("role_id is required for memory operations")

    @classmethod
    def _require_source_scope(cls, role_id: str, source_ref: MemorySourceRef) -> None:
        cls._require_role_id(role_id)
        if source_ref.sessionKey != f"role:{role_id}":
            raise ValueError("source sessionKey does not belong to role_id")

    def add_or_reinforce(
        self,
        role_id: str,
        memory_type: str,
        summary: str,
        source_ref: MemorySourceRef,
        *,
        extra: dict[str, Any] | None = None,
        happened_at: datetime | None = None,
        supersede_key: str | None = None,
        status: str = "active",
    ) -> MemoryItem:
        self._require_role_id(role_id)
        with self._connect() as connection:
            return self._add_or_reinforce_connection(
                connection,
                role_id,
                memory_type,
                summary,
                source_ref,
                extra=extra,
                happened_at=happened_at,
                supersede_key=supersede_key,
                status=status,
            )

    def add_or_reinforce_batch(
        self,
        role_id: str,
        records: list[tuple[str, str, MemorySourceRef]],
    ) -> list[MemoryItem]:
        """Persist a consolidation result in one SQLite transaction."""
        self._require_role_id(role_id)
        with self._connect() as connection:
            return [
                self._add_or_reinforce_connection(connection, role_id, memory_type, summary, source_ref)
                for memory_type, summary, source_ref in records
            ]

    def consolidate_batch(
        self,
        role_id: str,
        records: list[tuple[str, str, MemorySourceRef]],
    ) -> list[MemoryItem]:
        """Supersede extracted items covered by a consolidation and persist its result."""
        self._require_role_id(role_id)
        source_keys = {source.stableSourceKey for _, _, source in records}
        message_ids = {
            message_id
            for _, _, source in records
            for message_id in source.messageIds
        }
        with self._connect() as connection:
            if message_ids:
                rows = connection.execute(
                    "SELECT id, source_ref FROM memory_items WHERE role_id = ? AND status = 'active'",
                    (role_id,),
                ).fetchall()
                now = _now()
                for row in rows:
                    source_payload = self._decode_json(row["source_ref"], {})
                    keys = source_payload.get("sourceKeys", [])
                    if isinstance(keys, list) and source_keys.intersection(keys):
                        continue
                    origins = source_payload.get("sources", [])
                    origin_ids = {
                        message_id
                        for origin in origins if isinstance(origin, dict)
                        for message_id in origin.get("messageIds", []) if isinstance(message_id, str)
                    } if isinstance(origins, list) else set()
                    direct_ids = source_payload.get("messageIds", [])
                    if origin_ids.intersection(message_ids) or (
                        isinstance(direct_ids, list) and set(direct_ids).intersection(message_ids)
                    ):
                        connection.execute(
                            "UPDATE memory_items SET status = 'superseded', updated_at = ? WHERE id = ?",
                            (now, row["id"]),
                        )
            return [
                self._add_or_reinforce_connection(connection, role_id, memory_type, summary, source_ref)
                for memory_type, summary, source_ref in records
            ]

    def _add_or_reinforce_connection(
        self,
        connection: sqlite3.Connection,
        role_id: str,
        memory_type: str,
        summary: str,
        source_ref: MemorySourceRef,
        *,
        extra: dict[str, Any] | None = None,
        happened_at: datetime | None = None,
        supersede_key: str | None = None,
        status: str = "active",
    ) -> MemoryItem:
        self._require_source_scope(role_id, source_ref)
        summary = normalize_summary(summary)
        if not summary:
            raise ValueError("记忆内容不能为空")
        item_hash = content_hash(summary)
        now = _now()
        extra_payload = dict(extra or {})
        source_rows = connection.execute(
            "SELECT * FROM memory_items WHERE role_id = ? AND memory_type = ?",
            (role_id, memory_type),
        ).fetchall()
        for source_row in source_rows:
            source_payload = self._decode_json(source_row["source_ref"], {})
            source_keys = source_payload.get("sourceKeys", [])
            if isinstance(source_keys, list) and source_ref.stableSourceKey in source_keys:
                return self._item(source_row)
        row = connection.execute(
            "SELECT * FROM memory_items WHERE role_id = ? AND memory_type = ? AND content_hash = ?",
            (role_id, memory_type, item_hash),
        ).fetchone()
        if row is None:
            if supersede_key:
                rows = connection.execute(
                    "SELECT id, extra_json FROM memory_items WHERE role_id = ? AND memory_type = ? AND status = 'active'",
                    (role_id, memory_type),
                ).fetchall()
                for old_row in rows:
                    old_extra = self._decode_json(old_row["extra_json"], {})
                    if isinstance(old_extra, dict) and old_extra.get("supersedeKey") == supersede_key:
                        connection.execute(
                            "UPDATE memory_items SET status = 'superseded', updated_at = ? WHERE id = ?",
                            (now, old_row["id"]),
                        )
            item_id = f"memory-{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO memory_items
                  (id, role_id, memory_type, summary, extra_json, source_ref,
                   happened_at, status, created_at, updated_at, reinforcement, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    item_id,
                    role_id,
                    memory_type,
                    summary,
                    json.dumps(extra_payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        {
                            **source_ref.model_dump(mode="json"),
                            "sourceKeys": [source_ref.stableSourceKey],
                            "sources": [self._origin(source_ref)],
                        },
                        ensure_ascii=False,
                    ),
                    happened_at.isoformat() if happened_at else None,
                    status,
                    now,
                    now,
                    item_hash,
                ),
            )
            row = connection.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        else:
            source_payload = self._decode_json(row["source_ref"], {})
            source_keys = source_payload.get("sourceKeys", [])
            if not isinstance(source_keys, list):
                source_keys = []
            if source_ref.stableSourceKey not in source_keys:
                source_keys.append(source_ref.stableSourceKey)
                source_payload["sourceKeys"] = source_keys
                origins = source_payload.get("sources", [])
                if not isinstance(origins, list):
                    origins = []
                origins.append(self._origin(source_ref))
                source_payload["sources"] = origins
                connection.execute(
                    """
                    UPDATE memory_items
                    SET source_ref = ?, updated_at = ?, reinforcement = reinforcement + 1,
                        status = CASE WHEN status = 'superseded' THEN 'active' ELSE status END
                    WHERE id = ?
                    """,
                    (json.dumps(source_payload, ensure_ascii=False), now, row["id"]),
                )
                row = connection.execute("SELECT * FROM memory_items WHERE id = ?", (row["id"],)).fetchone()
        assert row is not None
        return self._item(row)

    def list_active(self, role_id: str) -> list[MemoryItem]:
        self._require_role_id(role_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items WHERE role_id = ? AND status = 'active' ORDER BY updated_at DESC, created_at DESC",
                (role_id,),
            ).fetchall()
        return [self._item(row) for row in rows]

    def list_all(self, role_id: str) -> list[MemoryItem]:
        self._require_role_id(role_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items WHERE role_id = ? ORDER BY updated_at DESC, created_at DESC",
                (role_id,),
            ).fetchall()
        return [self._item(row) for row in rows]

    def query(self, role_id: str, text: str, limit: int = 8) -> list[MemoryItem]:
        candidates = self.list_active(role_id)
        normalized_query = normalize_summary(text).casefold()
        if not normalized_query:
            return []
        terms: list[str] = []
        for token in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", normalized_query):
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                if len(token) == 1:
                    terms.append(token)
                else:
                    terms.extend(token[index : index + 2] for index in range(len(token) - 1))
            else:
                terms.append(token)

        def score(item: MemoryItem) -> tuple[int, str]:
            haystack = item.summary.casefold()
            exact = 100 if normalized_query and normalized_query in haystack else 0
            matches = sum(1 for term in terms if term in haystack)
            return exact + matches, item.updatedAt.isoformat()

        ranked = sorted(candidates, key=score, reverse=True)
        if not terms:
            return ranked[:limit]
        matching = [item for item in ranked if score(item)[0] > 0]
        return matching[:limit]

    def find_status_candidate(self, role_id: str, text: str, statuses: tuple[str, ...]) -> MemoryItem | None:
        self._require_role_id(role_id)
        if not statuses:
            return None
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_items WHERE role_id = ? AND status IN ({placeholders}) ORDER BY updated_at DESC",
                (role_id, *statuses),
            ).fetchall()
        normalized = normalize_summary(text).casefold()
        terms: list[str] = []
        for token in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", normalized):
            if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 1:
                terms.extend(token[index : index + 2] for index in range(len(token) - 1))
            else:
                terms.append(token)
        candidates = [self._item(row) for row in rows]
        matching = [item for item in candidates if normalized and normalized in item.summary.casefold()]
        if not matching:
            matching = [item for item in candidates if any(term in item.summary.casefold() for term in terms)]
        if not matching:
            return None
        return max(matching, key=lambda item: (sum(term in item.summary.casefold() for term in terms), item.updatedAt))

    def set_embedding(self, role_id: str, item_id: str, vector: list[float]) -> None:
        self._require_role_id(role_id)
        if not vector or any(not math.isfinite(float(value)) for value in vector):
            raise ValueError("embedding vector must contain finite values")
        with self._connect() as connection:
            dimension = len(vector)
            row = connection.execute(
                "SELECT dimension FROM memory_embedding_spaces WHERE role_id = ?", (role_id,)
            ).fetchone()
            if row is not None and int(row["dimension"]) != dimension:
                connection.execute("UPDATE memory_items SET embedding_json = NULL WHERE role_id = ?", (role_id,))
            connection.execute(
                "INSERT INTO memory_embedding_spaces(role_id, dimension) VALUES (?, ?) "
                "ON CONFLICT(role_id) DO UPDATE SET dimension = excluded.dimension",
                (role_id, dimension),
            )
            connection.execute(
                "UPDATE memory_items SET embedding_json = ? WHERE role_id = ? AND id = ? AND status = 'active'",
                (json.dumps(vector), role_id, item_id),
            )

    def embedding_for(self, role_id: str, item_id: str) -> list[float] | None:
        self._require_role_id(role_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT embedding_json FROM memory_items WHERE role_id = ? AND id = ?",
                (role_id, item_id),
            ).fetchone()
        if row is None:
            return None
        vector = self._decode_json(row["embedding_json"], None)
        return vector if isinstance(vector, list) else None

    def reinforce_items_batch(self, role_id: str, ids: list[str]) -> None:
        self._require_role_id(role_id)
        unique_ids = tuple(dict.fromkeys(item_id for item_id in ids if item_id))
        if not unique_ids:
            return
        placeholders = ",".join("?" for _ in unique_ids)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE memory_items SET reinforcement = reinforcement + 1, updated_at = ? "
                f"WHERE role_id = ? AND status = 'active' AND id IN ({placeholders})",
                (_now(), role_id, *unique_ids),
            )

    def update_metadata(
        self,
        role_id: str,
        item_id: str,
        *,
        status: str | None = None,
        extra_json: dict[str, object] | None = None,
        source_ref: str | None = None,
        happened_at: str | None = None,
        emotional_weight: int | None = None,
    ) -> MemoryItem | None:
        self._require_role_id(role_id)
        if emotional_weight is not None and not 0 <= emotional_weight <= 10:
            raise ValueError("emotional_weight must be between 0 and 10")
        if source_ref is not None:
            try:
                parsed_source = MemorySourceRef.model_validate_json(source_ref)
            except ValueError as error:
                raise ValueError("source_ref must be a valid memory source reference") from error
            self._require_source_scope(role_id, parsed_source)
        updates: list[str] = []
        values: list[object] = []
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if extra_json is not None:
            updates.append("extra_json = ?")
            values.append(json.dumps(extra_json, ensure_ascii=False, sort_keys=True))
        if source_ref is not None:
            updates.append("source_ref = ?")
            values.append(source_ref)
        if happened_at is not None:
            updates.append("happened_at = ?")
            values.append(happened_at)
        if emotional_weight is not None:
            updates.append("emotional_weight = ?")
            values.append(emotional_weight)
        if updates:
            updates.append("updated_at = ?")
            values.extend((_now(), role_id, item_id))
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE memory_items SET {', '.join(updates)} WHERE role_id = ? AND id = ?",
                    values,
                )
        return self.get(role_id, item_id)

    def remove_batch(self, role_id: str, ids: list[str]) -> int:
        self._require_role_id(role_id)
        unique_ids = tuple(dict.fromkeys(item_id for item_id in ids if item_id))
        if not unique_ids:
            return 0
        placeholders = ",".join("?" for _ in unique_ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM memory_items WHERE role_id = ? AND id IN ({placeholders})",
                (role_id, *unique_ids),
            )
        return cursor.rowcount

    def invalidate_role(self, role_id: str) -> int:
        self._require_role_id(role_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE memory_items SET status = 'forgotten', updated_at = ? "
                "WHERE role_id = ? AND status = 'active'",
                (_now(), role_id),
            )
        return cursor.rowcount

    def list_without_embeddings(self, role_id: str, limit: int = 32) -> list[MemoryItem]:
        self._require_role_id(role_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items WHERE role_id = ? AND status = 'active' AND embedding_json IS NULL ORDER BY updated_at DESC LIMIT ?",
                (role_id, limit),
            ).fetchall()
        return [self._item(row) for row in rows]

    def query_hybrid(self, role_id: str, text: str, vector: list[float] | None, limit: int = 8) -> list[MemoryItem]:
        lexical = self.query(role_id, text, limit=max(limit, 1))
        if vector is None:
            return lexical[:limit]
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items WHERE role_id = ? AND status = 'active' AND embedding_json IS NOT NULL",
                (role_id,),
            ).fetchall()
        semantic = sorted(
            (row for row in rows if self._cosine(vector, self._decode_json(row["embedding_json"], [])) >= 0.15),
            key=lambda row: self._cosine(vector, self._decode_json(row["embedding_json"], [])),
            reverse=True,
        )
        fused: dict[str, tuple[float, sqlite3.Row | MemoryItem]] = {}
        lexical_ids = [item.id for item in lexical]
        semantic_ids = [str(row["id"]) for row in semantic]
        by_id = {str(row["id"]): row for row in semantic}
        if len(by_id) < len(rows):
            by_id.update({item.id: item for item in lexical if item.id not in by_id})
        for result_ids in (lexical_ids, semantic_ids):
            for rank, item_id in enumerate(result_ids, 1):
                current_score, _ = fused.get(item_id, (0.0, by_id[item_id]))
                fused[item_id] = (current_score + 1 / (60 + rank), by_id[item_id])
        ordered = sorted(fused.values(), key=lambda entry: entry[0], reverse=True)
        return [self._item(row) if isinstance(row, sqlite3.Row) else row for _, row in ordered[:limit]]

    @staticmethod
    def _cosine(left: list[float], right: Any) -> float:
        if not isinstance(right, list) or len(left) != len(right) or not left:
            return -1.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(float(value) * float(value) for value in right))
        if not left_norm or not right_norm:
            return -1.0
        return sum(float(a) * float(b) for a, b in zip(left, right)) / (left_norm * right_norm)

    def get(self, role_id: str, item_id: str) -> MemoryItem | None:
        self._require_role_id(role_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_items WHERE role_id = ? AND id = ?", (role_id, item_id)
            ).fetchone()
        return self._item(row) if row is not None else None

    def update(self, role_id: str, item_id: str, summary: str, memory_type: str, happened_at: datetime | None) -> MemoryItem:
        self._require_role_id(role_id)
        summary = normalize_summary(summary)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_items WHERE role_id = ? AND id = ?", (role_id, item_id)).fetchone()
            if row is None:
                raise KeyError(item_id)
            connection.execute(
                "UPDATE memory_items SET summary = ?, memory_type = ?, happened_at = ?, content_hash = ?, embedding_json = NULL, updated_at = ? WHERE role_id = ? AND id = ?",
                (summary, memory_type, happened_at.isoformat() if happened_at else None, content_hash(summary), _now(), role_id, item_id),
            )
            row = connection.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        assert row is not None
        return self._item(row)

    def set_status(self, role_id: str, item_id: str, status: str) -> MemoryItem:
        self._require_role_id(role_id)
        with self._connect() as connection:
            connection.execute("UPDATE memory_items SET status = ?, updated_at = ? WHERE role_id = ? AND id = ?", (status, _now(), role_id, item_id))
            row = connection.execute("SELECT * FROM memory_items WHERE role_id = ? AND id = ?", (role_id, item_id)).fetchone()
        if row is None:
            raise KeyError(item_id)
        return self._item(row)

    def remove(self, role_id: str, item_id: str) -> None:
        self._require_role_id(role_id)
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM memory_items WHERE role_id = ? AND id = ?", (role_id, item_id))
        if cursor.rowcount == 0:
            raise KeyError(item_id)

    def delete_role(self, role_id: str) -> None:
        self._require_role_id(role_id)
        with self._connect() as connection:
            connection.execute("DELETE FROM memory_items WHERE role_id = ?", (role_id,))
            connection.execute("DELETE FROM semantic_memory WHERE role_id = ?", (role_id,))
            connection.execute("DELETE FROM memory_embedding_spaces WHERE role_id = ?", (role_id,))
            connection.execute("DELETE FROM consolidation_events WHERE role_id = ?", (role_id,))
            connection.execute("DELETE FROM memory_replacements WHERE role_id = ?", (role_id,))

    def snapshot_role(self, role_id: str) -> dict[str, list[dict[str, object]]]:
        self._require_role_id(role_id)
        table_names = (
            "memory_items",
            "semantic_memory",
            "memory_embedding_spaces",
            "consolidation_events",
            "memory_replacements",
        )
        with self._connect() as connection:
            return {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE role_id = ?", (role_id,))]
                for table in table_names
            }

    def restore_role(self, role_id: str, snapshot: dict[str, list[dict[str, object]]] | tuple[list[dict[str, object]], list[dict[str, object]]]) -> None:
        self._require_role_id(role_id)
        if isinstance(snapshot, tuple):
            rows_by_table = {"memory_items": snapshot[0], "semantic_memory": snapshot[1]}
        else:
            rows_by_table = snapshot
        table_names = (
            "memory_items",
            "semantic_memory",
            "memory_embedding_spaces",
            "consolidation_events",
            "memory_replacements",
        )
        with self._connect() as connection:
            for table in table_names:
                connection.execute(f"DELETE FROM {table} WHERE role_id = ?", (role_id,))
            for table in table_names:
                table_rows = rows_by_table.get(table, [])
                if table_rows:
                    columns = list(table_rows[0])
                    placeholders = ", ".join("?" for _ in columns)
                    connection.executemany(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                        [[row[column] for column in columns] for row in table_rows],
                    )

    def _item(self, row: sqlite3.Row) -> MemoryItem:
        source_payload = self._decode_json(row["source_ref"], {})
        source = MemorySourceRef.model_validate(source_payload)
        extra = self._decode_json(row["extra_json"], {})
        return MemoryItem(
            id=str(row["id"]),
            roleId=str(row["role_id"]),
            memoryType=str(row["memory_type"]),
            summary=str(row["summary"]),
            extra=extra if isinstance(extra, dict) else {},
            sourceRef=source,
            happenedAt=datetime.fromisoformat(row["happened_at"]) if row["happened_at"] else None,
            status=str(row["status"]),
            createdAt=datetime.fromisoformat(row["created_at"]),
            updatedAt=datetime.fromisoformat(row["updated_at"]),
            reinforcement=int(row["reinforcement"]),
            contentHash=str(row["content_hash"]),
            emotionalWeight=int(row["emotional_weight"]),
            hasEmbedding=row["embedding_json"] is not None,
        )

    @staticmethod
    def _origin(source_ref: MemoryOrigin) -> dict[str, Any]:
        return source_ref.model_dump(mode="json")

    @staticmethod
    def _decode_json(value: str, fallback: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return fallback
