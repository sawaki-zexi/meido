"""Bounded directory observations, independent of permission to send messages."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from copy import deepcopy
from typing import Any
from .models import Target

MAX_RECORDS = 5000
MAX_RESPONSE_BYTES = 1024 * 1024
SNAPSHOT_TTL = 60
MAX_SNAPSHOTS = 8
QUERY_TIMEOUT = 10


def observed_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DirectoryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class DirectoryObservation:
    targets: list[Target] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DirectoryQuery:
    kind: str = "known"
    group_id: str = ""
    member_id: str = ""
    limit: int = 25
    cursor: str = ""

    def __post_init__(self) -> None:
        if (self.kind not in {"known", "friends", "groups", "members", "member"}
                or type(self.limit) is not int or not 1 <= self.limit <= 50
                or any(not isinstance(x, str) or len(x) > 512 for x in (self.group_id, self.member_id, self.cursor))):
            raise DirectoryError("invalid_query")
        if bool(self.group_id) != (self.kind in {"members", "member"}) or bool(self.member_id) != (self.kind == "member"):
            raise DirectoryError("invalid_query")

    def identity(self) -> tuple[str, str, str, int]:
        return self.kind, self.group_id, self.member_id, self.limit


def supported_kinds(channel: str) -> list[str]:
    return {"qq": ["known", "friends", "groups", "members"],
            "telegram": ["known", "member"]}.get(channel, ["known"])


def bound_records(items: list[dict[str, Any]]) -> None:
    if len(items) > MAX_RECORDS or len(json.dumps(items, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise DirectoryError("directory_too_large")


@dataclass
class _Snapshot:
    owner: tuple[str, str, str, str]
    query: tuple[str, str, str, int]
    items: list[dict[str, Any]]
    observed_at: str
    expires_at: float
    coverage: str
    cursors: dict[str, int]


class DirectoryPages:
    """Short-lived immutable pages bound to caller, query and account generation."""
    def __init__(self) -> None:
        self._snapshots: list[_Snapshot] = []

    def _prune(self) -> None:
        self._snapshots = [item for item in self._snapshots if item.expires_at > time.monotonic()]

    def read(self, owner: tuple[str, str, str, str], query: DirectoryQuery) -> dict[str, Any]:
        self._prune()
        for snapshot in self._snapshots:
            if query.cursor in snapshot.cursors and snapshot.owner == owner and snapshot.query == query.identity():
                return self._page(snapshot, snapshot.cursors[query.cursor], query.limit)
        raise DirectoryError("invalid_or_expired_cursor")

    def create(self, owner: tuple[str, str, str, str], query: DirectoryQuery, items: list[dict[str, Any]], *, coverage: str) -> dict[str, Any]:
        self.ensure_capacity(owner)
        bound_records(items)
        snapshot = _Snapshot(owner, query.identity(), deepcopy(items), observed_now(), time.monotonic() + SNAPSHOT_TTL, coverage, {})
        if len(items) > query.limit:
            self._snapshots.append(snapshot)
        return self._page(snapshot, 0, query.limit)

    def ensure_capacity(self, owner: tuple[str, str, str, str]) -> None:
        self._prune()
        if len(self._snapshots) >= MAX_SNAPSHOTS or sum(item.owner[0] == owner[0] for item in self._snapshots) >= 2:
            raise DirectoryError("directory_busy")

    @staticmethod
    def _page(snapshot: _Snapshot, offset: int, limit: int) -> dict[str, Any]:
        following = offset + limit
        cursor = None
        if following < len(snapshot.items):
            cursor = next((key for key, value in snapshot.cursors.items() if value == following), None)
            if cursor is None:
                cursor = uuid.uuid4().hex
                snapshot.cursors[cursor] = following
        return {"targets": deepcopy(snapshot.items[offset:following]), "nextCursor": cursor, "coverage": snapshot.coverage,
                "complete": True, "observedAt": snapshot.observed_at, "total": len(snapshot.items)}
