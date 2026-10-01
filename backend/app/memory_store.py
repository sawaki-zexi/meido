from __future__ import annotations

import hashlib
import json
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
                    content_hash TEXT NOT NULL
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
    ) -> MemoryItem:
        summary = normalize_summary(summary)
        if not summary:
            raise ValueError("记忆内容不能为空")
        item_hash = content_hash(summary)
        now = _now()
        extra_payload = dict(extra or {})
        with self._connect() as connection:
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1, ?)
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
                            status = CASE WHEN status = 'forgotten' THEN 'active' ELSE status END
                        WHERE id = ?
                        """,
                        (json.dumps(source_payload, ensure_ascii=False), now, row["id"]),
                    )
                    row = connection.execute("SELECT * FROM memory_items WHERE id = ?", (row["id"],)).fetchone()
            assert row is not None
            return self._item(row)

    def list_active(self, role_id: str) -> list[MemoryItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items WHERE role_id = ? AND status = 'active' ORDER BY updated_at DESC, created_at DESC",
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

    def get(self, role_id: str, item_id: str) -> MemoryItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_items WHERE role_id = ? AND id = ?", (role_id, item_id)
            ).fetchone()
        return self._item(row) if row is not None else None

    def delete_role(self, role_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM memory_items WHERE role_id = ?", (role_id,))
            connection.execute("DELETE FROM semantic_memory WHERE role_id = ?", (role_id,))

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
