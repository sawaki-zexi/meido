from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol
from urllib.parse import urlencode

import httpx

from .agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition


_SUPPORTED_TYPES = frozenset({"doc", "docx"})
_MAX_QUERY_LENGTH = 500
_MAX_RESULT_LIMIT = 8
_MAX_TOOL_OUTPUT = 12000
_DEFAULT_CHUNK_CHARS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RemoteDocument:
    document_id: str
    node_id: str
    object_token: str
    object_type: str
    title: str
    url: str
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class DocumentScan:
    documents: list[RemoteDocument]
    complete: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncReport:
    trigger: str
    complete: bool
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    unchanged: int = 0
    errors: tuple[str, ...] = ()
    finished_at: str = ""
    changed_document_ids: tuple[str, ...] = ()
    deleted_document_titles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OwnerKnowledgeStatus:
    enabled: bool
    connected: bool
    last_sync: SyncReport | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerKnowledgeHit:
    kind: str
    text: str
    document_id: str | None = None
    title: str | None = None
    url: str | None = None
    location: str | None = None
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class _Chunk:
    chunk_id: str
    ordinal: int
    text: str
    location: str
    vector: tuple[float, ...]


class FeishuReader(Protocol):
    async def scan(self) -> DocumentScan: ...

    async def read_document(self, document: RemoteDocument) -> str: ...


class Embedder(Protocol):
    model_id: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class LocalEmbeddingAdapter:
    """Generate semantic vectors locally through FastEmbed."""

    model_id = "local-hash-v1"

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        self._model = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self.model_id = f"fastembed:{model_name}" if model_name else self.model_id
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]

            self._text_embedding = TextEmbedding
            self._model_name = model_name
        except Exception as error:
            self._load_error = error
            self._text_embedding = None
            self._model_name = model_name
        else:
            self._load_error = None

    @property
    def ready(self) -> bool:
        return self._text_embedding is not None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> None:
        with self._model_lock:
            if self._model is None:
                if self._text_embedding is None:
                    raise RuntimeError("FastEmbed 本机语义模型不可用") from self._load_error
                try:
                    self._model = self._text_embedding(model_name=self._model_name)
                except Exception as error:
                    self._load_error = error
                    self._text_embedding = None
                    raise RuntimeError("FastEmbed 本机语义模型加载失败") from error

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._load()
        if self._model is not None:
            with self._inference_lock:
                return [list(map(float, vector)) for vector in self._model.embed(texts)]
        raise RuntimeError("本机语义 embedding 模型不可用") from self._load_error

    def embed_query(self, text: str) -> list[float]:
        self._load()
        if self._model is not None:
            with self._inference_lock:
                return list(map(float, next(iter(self._model.embed([text])))))
        raise RuntimeError("本机语义 embedding 模型不可用") from self._load_error


class FeishuApiConnector:
    """Read-only connector for a user-authorized Feishu Wiki."""

    def __init__(
        self,
        token_getter: Callable[[], str],
        base_url: str = "https://open.feishu.cn/open-apis",
        refresh_token: Callable[[], object] | None = None,
    ) -> None:
        self._token_getter = token_getter
        self.base_url = base_url.rstrip("/")
        self._refresh_token = refresh_token

    async def _get(self, path: str, params: Mapping[str, object] | None = None) -> dict[str, object]:
        token = self._token_getter()
        if not token:
            raise RuntimeError("飞书尚未连接")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self.base_url}{path}",
                params=dict(params or {}),
                headers={"Authorization": f"Bearer {token}"},
            )
            payload = response.json() if response.status_code != 401 else {}
            invalid_user_token = isinstance(payload, dict) and str(payload.get("code")) in {"99991663", "99991664"}
            if (response.status_code == 401 or invalid_user_token) and self._refresh_token is not None:
                refreshed = self._refresh_token()
                if asyncio.iscoroutine(refreshed):
                    await refreshed
                token = self._token_getter()
                response = await client.get(
                    f"{self.base_url}{path}",
                    params=dict(params or {}),
                    headers={"Authorization": f"Bearer {token}"},
                )
            response.raise_for_status()
        if not isinstance(payload, dict) or payload.get("code", 0) not in (0, None):
            raise RuntimeError("飞书接口返回错误")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def _post(self, path: str, payload: Mapping[str, object]) -> dict[str, object]:
        token = self._token_getter()
        if not token:
            raise RuntimeError("飞书尚未连接")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}{path}",
                json=dict(payload),
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or result.get("code", 0) not in (0, None):
            raise RuntimeError("飞书接口返回错误")
        data = result.get("data")
        return data if isinstance(data, dict) else {}

    async def scan(self) -> DocumentScan:
        documents: list[RemoteDocument] = []
        errors: list[str] = []
        try:
            spaces = await self._paged("/wiki/v2/spaces", ("items", "spaces"), params={"user_id_type": "open_id"})
            for space in spaces:
                space_id = str(space.get("space_id", ""))
                if not space_id:
                    errors.append("space:missing_id")
                    continue
                await self._scan_nodes(space_id, "", documents, errors)
        except Exception as error:
            errors.append(type(error).__name__)
        return DocumentScan(documents=documents, complete=not errors, errors=tuple(errors))

    async def _scan_nodes(self, space_id: str, parent: str, documents: list[RemoteDocument], errors: list[str]) -> None:
        try:
            nodes = await self._paged(f"/wiki/v2/spaces/{space_id}/nodes", ("items", "nodes"), params={
                "parent_node_token": parent,
                "user_id_type": "open_id",
            })
        except Exception as error:
            errors.append(type(error).__name__)
            return
        for node in nodes:
            node_token = str(node.get("node_token", ""))
            node_info = node
            child_has_more = bool(node.get("has_child", False))
            if node_token and (node.get("obj_type") is None or node.get("obj_token") is None or node.get("title") is None):
                try:
                    node_info = await self._get("/wiki/v2/spaces/get_node", {"token": node_token})
                    node_info = node_info.get("node", node_info)
                    if not isinstance(node_info, dict):
                        node_info = node
                except Exception as error:
                    errors.append(f"node:{type(error).__name__}")
                    continue
            obj_type = str(node_info.get("obj_type", node.get("obj_type", "")))
            object_token = str(node_info.get("obj_token", node.get("obj_token", "")))
            if obj_type and obj_type not in {"folder", "wiki", "space"}:
                documents.append(RemoteDocument(
                    document_id=object_token or node_token,
                    node_id=node_token,
                    object_token=object_token or node_token,
                    object_type=obj_type,
                    title=str(node_info.get("title", node.get("title", "未命名文档"))),
                    url=f"https://feishu.cn/wiki/{node_token}",
                    updated_at=str(node_info.get("obj_edit_time", node.get("obj_edit_time", ""))),
                ))
            if node_token and child_has_more:
                await self._scan_nodes(space_id, node_token, documents, errors)

    async def _paged(self, path: str, list_key: str | tuple[str, ...], *, params: Mapping[str, object] | None = None) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        page_token = ""
        seen_page_tokens: set[str] = set()
        while True:
            request_params = {**dict(params or {}), "page_size": 50}
            if page_token:
                request_params["page_token"] = page_token
            data = await self._get(path, request_params)
            keys = (list_key,) if isinstance(list_key, str) else list_key
            items = next((data[key] for key in keys if key in data), None)
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise RuntimeError("飞书分页响应格式无效")
            values.extend(items)
            if not data.get("has_more"):
                return values
            page_token = str(data.get("page_token", ""))
            if not page_token or page_token in seen_page_tokens:
                raise RuntimeError("飞书分页未提供新的 page token")
            seen_page_tokens.add(page_token)

    async def read_document(self, document: RemoteDocument) -> str:
        if document.object_type == "docx":
            data = await self._get(f"/docx/v1/documents/{document.object_token}/raw_content")
            content = data.get("content", "")
            if not isinstance(content, str):
                raise RuntimeError("飞书文档正文格式无效")
            return content
        task = await self._post("/drive/v1/export_tasks", {
            "file_extension": "docx",
            "token": document.object_token,
            "type": "doc",
        })
        ticket = str(task.get("ticket", ""))
        if not ticket:
            raise RuntimeError("飞书文档导出任务未返回 ticket")
        for _ in range(30):
            result = await self._get(f"/drive/v1/export_tasks/{ticket}")
            task_result = result.get("result")
            if isinstance(task_result, dict) and task_result.get("file_token"):
                file_token = str(task_result["file_token"])
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.get(
                        f"{self.base_url}/drive/v1/export_tasks/{ticket}/download",
                        headers={"Authorization": f"Bearer {self._token_getter()}"},
                    )
                    response.raise_for_status()
                return _docx_text(response.content)
            status = str(result.get("job_status", ""))
            if status in {"failed", "4", "5"}:
                raise RuntimeError("飞书文档导出失败")
            await asyncio.sleep(1)
        raise TimeoutError("飞书文档导出超时")


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
        root = ET.fromstring(xml)
    except Exception as error:
        raise RuntimeError("飞书导出的 Word 文档无法解析") from error
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(item.text or "" for item in paragraph.findall(".//w:t", namespace)).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)

    async def exchange_code(self, code: str, app_id: str, app_secret: str, redirect_uri: str) -> dict[str, object]:
        if not app_id or not app_secret or not code:
            raise RuntimeError("飞书 OAuth 配置不完整")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
                json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
                headers={"Authorization": "Bearer " + await self._app_token(client, app_id, app_secret)},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("code", 0) not in (0, None):
            raise RuntimeError("飞书 OAuth 授权失败")
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    async def refresh_token(self, refresh_token: str, app_id: str, app_secret: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            app_token = await self._app_token(client, app_id, app_secret)
            response = await client.post(
                "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("code", 0) not in (0, None):
            raise RuntimeError("飞书令牌刷新失败")
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    @staticmethod
    async def _app_token(client: httpx.AsyncClient, app_id: str, app_secret: str) -> str:
        response = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("app_access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError("无法获取飞书应用访问令牌")
        return token


class _VectorIndex(Protocol):
    def upsert(self, item_id: str, model_id: str, vector: list[float]) -> None: ...

    def search(self, model_id: str, vector: list[float], limit: int) -> list[tuple[str, float]]: ...

    def delete(self, item_id: str) -> None: ...


class CredentialStore(Protocol):
    def get(self, key: str) -> str: ...

    def set(self, key: str, value: str) -> None: ...


class SystemCredentialStore:
    """Keep Feishu secrets in the operating system credential vault."""

    def get(self, key: str) -> str:
        try:
            import keyring  # type: ignore[import-not-found]

            return keyring.get_password("meido.owner-knowledge", key) or ""
        except Exception:
            # An unavailable vault means the optional Feishu connection is not
            # configured. Writes still fail loudly below so secrets are never
            # silently persisted somewhere else.
            return ""

    def set(self, key: str, value: str) -> None:
        try:
            import keyring  # type: ignore[import-not-found]

            if value:
                keyring.set_password("meido.owner-knowledge", key, value)
            else:
                try:
                    keyring.delete_password("meido.owner-knowledge", key)
                except keyring.errors.PasswordDeleteError:
                    pass
        except Exception as error:
            raise RuntimeError("系统凭据库不可用，未保存飞书凭据") from error


class SQLiteVecIndex:
    """Persistent sqlite-vec index for owner document chunks."""

    def __init__(self, database_path: str | Path) -> None:
        import sqlite_vec  # type: ignore[import-not-found]

        self.database_path = str(database_path)
        self._sqlite_vec = sqlite_vec

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.enable_load_extension(True)
        self._sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        return connection

    @staticmethod
    def _table(dimension: int) -> str:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        return f"owner_vec_{dimension}"

    def _ensure_table(self, connection: sqlite3.Connection, dimension: int) -> str:
        table = self._table(dimension)
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(item_id TEXT PRIMARY KEY, model_id TEXT, embedding float[{dimension}] distance_metric=cosine)"
        )
        return table

    def upsert(self, item_id: str, model_id: str, vector: list[float]) -> None:
        with self._connect() as connection:
            table = self._ensure_table(connection, len(vector))
            connection.execute(f"DELETE FROM {table} WHERE item_id = ?", (item_id,))
            connection.execute(
                f"INSERT INTO {table}(item_id, model_id, embedding) VALUES (?, ?, ?)",
                (item_id, model_id, self._sqlite_vec.serialize_float32(vector)),
            )

    def search(self, model_id: str, vector: list[float], limit: int) -> list[tuple[str, float]]:
        if limit <= 0:
            return []
        table = self._table(len(vector))
        with self._connect() as connection:
            connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(item_id TEXT PRIMARY KEY, model_id TEXT, embedding float[{len(vector)}] distance_metric=cosine)"
            )
            rows = connection.execute(
                f"SELECT item_id, distance FROM {table} WHERE embedding MATCH ? AND k = ? AND model_id = ?",
                (self._sqlite_vec.serialize_float32(vector), limit, model_id),
            ).fetchall()
        return [(str(row[0]), max(-1.0, min(1.0, 1.0 - float(row[1])))) for row in rows]

    def delete(self, item_id: str) -> None:
        with self._connect() as connection:
            for table in self._tables(connection):
                connection.execute(f"DELETE FROM {table} WHERE item_id = ?", (item_id,))

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> list[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'owner_vec_%'"
        ).fetchall()
        return [str(row[0]) for row in rows if re.fullmatch(r"owner_vec_\d+", str(row[0]))]


class OwnerKnowledgeStore:
    """Local document mirror, FTS index, vector index, and plugin state."""

    def __init__(
        self,
        database_path: str | Path,
        vector_index: _VectorIndex | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self.database_path = str(database_path)
        self.credential_store = credential_store or SystemCredentialStore()
        self.vector_index = vector_index
        if self.vector_index is None:
            try:
                candidate = SQLiteVecIndex(database_path)
                probe = candidate._connect()
                probe.close()
                self.vector_index = candidate
            except Exception:
                self.vector_index = None
        self._initialize()

    @staticmethod
    def _credential_key(key: str) -> bool:
        return key in {"feishu_access_token", "feishu_refresh_token", "feishu_app_secret"}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS owner_documents (
                    document_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    object_token TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    synced_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES owner_documents(document_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    location TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    UNIQUE(document_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS owner_chunks_document ON owner_chunks(document_id, ordinal);
                CREATE VIRTUAL TABLE IF NOT EXISTS owner_chunks_fts USING fts5(
                    chunk_id UNINDEXED, title, body, terms, tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS owner_plugin_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    scanned INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL DEFAULT 0,
                    updated INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    unchanged INTEGER NOT NULL DEFAULT 0,
                    errors_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS owner_understandings (
                    role_id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_understanding_jobs (
                    role_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    full_refresh INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def get_state(self, key: str, default: str = "") -> str:
        if self._credential_key(key):
            return self.credential_store.get(key) or default
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM owner_plugin_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_state(self, key: str, value: str) -> None:
        if self._credential_key(key):
            self.credential_store.set(key, value)
            return
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO owner_plugin_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def is_enabled(self) -> bool:
        return self.get_state("enabled") == "true"

    def needs_update(self, document: RemoteDocument, model_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at, embedding_model, status FROM owner_documents WHERE document_id = ?",
                (document.document_id,),
            ).fetchone()
        if row is None or row["status"] != "synced" or row["embedding_model"] != model_id:
            return True
        return not document.updated_at or str(row["updated_at"]) != document.updated_at

    def document_content_hash(self, document_id: str) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT content_hash, embedding_model FROM owner_documents WHERE document_id = ? AND status = 'synced'",
                (document_id,),
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def touch_unchanged_document(self, document: RemoteDocument) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_documents SET node_id = ?, object_token = ?, object_type = ?, title = ?, url = ?, updated_at = ?, synced_at = ?, last_error = NULL WHERE document_id = ?",
                (
                    document.node_id,
                    document.object_token,
                    document.object_type,
                    document.title,
                    document.url,
                    document.updated_at,
                    _now(),
                    document.document_id,
                ),
            )

    def replace_document(
        self,
        document: RemoteDocument,
        content: str,
        chunks: list[_Chunk],
        model_id: str,
    ) -> None:
        now = _now()
        with self._connect() as connection:
            old_chunk_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT chunk_id FROM owner_chunks WHERE document_id = ?", (document.document_id,)
                )
            ]
        new_chunk_ids = {chunk.chunk_id for chunk in chunks}
        if self.vector_index is not None:
            for chunk in chunks:
                self.vector_index.upsert(chunk.chunk_id, model_id, list(chunk.vector))
        with self._connect() as connection:
            for chunk_id in old_chunk_ids:
                connection.execute("DELETE FROM owner_chunks_fts WHERE chunk_id = ?", (chunk_id,))
            connection.execute("DELETE FROM owner_chunks WHERE document_id = ?", (document.document_id,))
            connection.execute(
                """INSERT INTO owner_documents(
                    document_id, node_id, object_token, object_type, title, url, updated_at,
                    content, content_hash, embedding_model, status, last_error, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'synced', NULL, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    node_id=excluded.node_id, object_token=excluded.object_token,
                    object_type=excluded.object_type, title=excluded.title, url=excluded.url,
                    updated_at=excluded.updated_at, content=excluded.content,
                    content_hash=excluded.content_hash, embedding_model=excluded.embedding_model,
                    status='synced', last_error=NULL, synced_at=excluded.synced_at""",
                (
                    document.document_id,
                    document.node_id,
                    document.object_token,
                    document.object_type,
                    document.title,
                    document.url,
                    document.updated_at,
                    content,
                    _sha256(content),
                    model_id,
                    now,
                ),
            )
            for chunk in chunks:
                vector = list(chunk.vector)
                connection.execute(
                    "INSERT INTO owner_chunks(chunk_id, document_id, ordinal, body, location, embedding_json, embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chunk.chunk_id, document.document_id, chunk.ordinal, chunk.text, chunk.location, json.dumps(vector), model_id),
                )
                connection.execute(
                    "INSERT INTO owner_chunks_fts(chunk_id, title, body, terms) VALUES (?, ?, ?, ?)",
                    (chunk.chunk_id, document.title, chunk.text, _fts_terms(document.title + " " + chunk.text)),
                )
        if self.vector_index is not None:
            for chunk_id in old_chunk_ids:
                if chunk_id not in new_chunk_ids:
                    self.vector_index.delete(chunk_id)

    def mark_skipped(self, document: RemoteDocument) -> None:
        old_chunk_ids: list[str] = []
        with self._connect() as connection:
            old_chunk_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT chunk_id FROM owner_chunks WHERE document_id = ?", (document.document_id,)
                )
            ]
            for chunk_id in old_chunk_ids:
                connection.execute("DELETE FROM owner_chunks_fts WHERE chunk_id = ?", (chunk_id,))
            connection.execute("DELETE FROM owner_chunks WHERE document_id = ?", (document.document_id,))
            connection.execute(
                """INSERT INTO owner_documents(
                    document_id, node_id, object_token, object_type, title, url, updated_at,
                    content, content_hash, embedding_model, status, last_error, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', 'skipped', NULL, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    node_id=excluded.node_id, object_token=excluded.object_token,
                    object_type=excluded.object_type, title=excluded.title, url=excluded.url,
                    updated_at=excluded.updated_at, content='', content_hash='',
                    embedding_model='', status='skipped', last_error=NULL, synced_at=excluded.synced_at""",
                (
                    document.document_id,
                    document.node_id,
                    document.object_token,
                    document.object_type,
                    document.title,
                    document.url,
                    document.updated_at,
                    _now(),
                ),
            )
        if self.vector_index is not None:
            for chunk_id in old_chunk_ids:
                self.vector_index.delete(chunk_id)

    def delete_missing_documents(self, seen_document_ids: set[str]) -> list[tuple[str, str]]:
        missing_chunk_ids: list[str] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document_id, title FROM owner_documents WHERE status IN ('synced', 'skipped')"
            ).fetchall()
            missing = [(str(row[0]), str(row[1])) for row in rows if str(row[0]) not in seen_document_ids]
            for document_id, _ in missing:
                chunk_ids = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT chunk_id FROM owner_chunks WHERE document_id = ?", (document_id,)
                    )
                ]
                for chunk_id in chunk_ids:
                    connection.execute("DELETE FROM owner_chunks_fts WHERE chunk_id = ?", (chunk_id,))
                    missing_chunk_ids.append(chunk_id)
                connection.execute("DELETE FROM owner_chunks WHERE document_id = ?", (document_id,))
                connection.execute("DELETE FROM owner_documents WHERE document_id = ?", (document_id,))
        if self.vector_index is not None:
            for chunk_id in missing_chunk_ids:
                self.vector_index.delete(chunk_id)
        return missing

    def list_documents(self) -> list[RemoteDocument]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document_id, node_id, object_token, object_type, title, url, updated_at FROM owner_documents WHERE status = 'synced' ORDER BY title"
            ).fetchall()
        return [
            RemoteDocument(
                document_id=str(row["document_id"]),
                node_id=str(row["node_id"]),
                object_token=str(row["object_token"]),
                object_type=str(row["object_type"]),
                title=str(row["title"]),
                url=str(row["url"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def list_document_statuses(self, limit: int = 200) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document_id, object_type, title, url, updated_at, status, last_error FROM owner_documents ORDER BY title LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [{
            "documentId": str(row["document_id"]),
            "objectType": str(row["object_type"]),
            "title": str(row["title"]),
            "url": str(row["url"]),
            "updatedAt": str(row["updated_at"]),
            "status": str(row["status"]),
            "error": str(row["last_error"] or ""),
        } for row in rows]

    def search(self, query: str, vector: list[float], model_id: str, limit: int) -> list[OwnerKnowledgeHit]:
        vector_matches: list[tuple[str, float]] = []
        if self.vector_index is not None:
            try:
                vector_matches = self.vector_index.search(model_id, vector, max(limit * 4, limit))
            except Exception:
                vector_matches = []
        if not vector_matches:
            vector_matches = self._cosine_matches(vector, model_id, max(limit * 4, limit))
        lexical_matches = self._fts_matches(query, max(limit * 4, limit))

        scores: dict[str, float] = {}
        relevant_vector_ids = {chunk_id for chunk_id, score in vector_matches if score >= 0.22}
        for rank, (chunk_id, _) in enumerate(vector_matches, 1):
            if chunk_id not in relevant_vector_ids:
                continue
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
        for rank, chunk_id in enumerate(lexical_matches, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
        if not scores:
            return []

        ranked_ids = [item[0] for item in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]]
        placeholders = ",".join("?" for _ in ranked_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT c.chunk_id, c.document_id, c.body, c.location, d.title, d.url
                FROM owner_chunks c JOIN owner_documents d ON d.document_id = c.document_id
                WHERE c.chunk_id IN ({placeholders}) AND d.status = 'synced'""",
                ranked_ids,
            ).fetchall()
        by_id = {str(row["chunk_id"]): row for row in rows}
        hits: list[OwnerKnowledgeHit] = []
        for chunk_id in ranked_ids:
            row = by_id.get(chunk_id)
            if row is None:
                continue
            hits.append(OwnerKnowledgeHit(
                kind="owner_knowledge",
                text=str(row["body"]),
                document_id=str(row["document_id"]),
                title=str(row["title"]),
                url=str(row["url"]),
                location=str(row["location"]),
                score=scores[chunk_id],
            ))
        return hits

    def _fts_matches(self, query: str, limit: int) -> list[str]:
        terms = _fts_terms(query).split()
        if not terms:
            return []
        expression = " OR ".join('"' + item.replace('"', '""') + '"' for item in terms[:16])
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT chunk_id FROM owner_chunks_fts WHERE terms MATCH ? ORDER BY bm25(owner_chunks_fts) LIMIT ?",
                    (expression, limit),
                ).fetchall()
            return [str(row[0]) for row in rows]
        except sqlite3.OperationalError:
            return []

    def _cosine_matches(self, query: list[float], model_id: str, limit: int) -> list[tuple[str, float]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chunk_id, embedding_json FROM owner_chunks WHERE embedding_model = ?",
                (model_id,),
            ).fetchall()
        return sorted(
            ((str(row["chunk_id"]), _cosine(query, json.loads(str(row["embedding_json"])))) for row in rows),
            key=lambda item: item[1],
            reverse=True,
        )[:limit]

    def count_chunks(self, document_ids: Iterable[str] | None = None) -> int:
        bounded_ids = sorted(set(document_ids)) if document_ids is not None else None
        if bounded_ids == []:
            return 0
        where = "WHERE d.status = 'synced'"
        parameters: list[object] = []
        if bounded_ids is not None:
            where += f" AND c.document_id IN ({','.join('?' for _ in bounded_ids)})"
            parameters.extend(bounded_ids)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM owner_chunks c JOIN owner_documents d ON d.document_id = c.document_id {where}",
                parameters,
            ).fetchone()
        return int(row[0]) if row else 0

    def list_chunks(
        self,
        *,
        limit: int = 1000,
        offset: int = 0,
        document_ids: Iterable[str] | None = None,
    ) -> list[OwnerKnowledgeHit]:
        bounded_ids = sorted(set(document_ids)) if document_ids is not None else None
        if bounded_ids == []:
            return []
        where = "WHERE d.status = 'synced'"
        parameters: list[object] = []
        if bounded_ids is not None:
            where += f" AND c.document_id IN ({','.join('?' for _ in bounded_ids)})"
            parameters.extend(bounded_ids)
        parameters.extend((max(1, min(limit, 1000)), max(0, offset)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT c.document_id, c.body, c.location, d.title, d.url
                FROM owner_chunks c JOIN owner_documents d ON d.document_id = c.document_id
                {where} ORDER BY d.title, c.ordinal LIMIT ? OFFSET ?""",
                parameters,
            ).fetchall()
        return [OwnerKnowledgeHit(
            kind="owner_knowledge",
            text=str(row["body"]),
            document_id=str(row["document_id"]),
            title=str(row["title"]),
            url=str(row["url"]),
            location=str(row["location"]),
        ) for row in rows]

    def get_understanding(self, role_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT role_id, body, updated_at, version FROM owner_understandings WHERE role_id = ?",
                (role_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "roleId": str(row["role_id"]),
            "body": str(row["body"]),
            "updatedAt": str(row["updated_at"]),
            "version": int(row["version"]),
        }

    def search_understanding(self, role_id: str, vector: list[float], model_id: str) -> OwnerKnowledgeHit | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body, embedding_json FROM owner_understandings WHERE role_id = ? AND embedding_model = ?",
                (role_id, model_id),
            ).fetchone()
        if row is None:
            return None
        score = _cosine(vector, json.loads(str(row["embedding_json"])))
        if score < 0.22:
            return None
        return OwnerKnowledgeHit(kind="role_understanding", text=str(row["body"]), score=score)

    def save_understanding(self, role_id: str, body: str, vector: list[float], model_id: str) -> dict[str, object]:
        existing = self.get_understanding(role_id)
        version = int(existing["version"]) + 1 if existing else 1
        updated_at = _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO owner_understandings(role_id, body, embedding_json, embedding_model, updated_at, version)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(role_id) DO UPDATE SET body=excluded.body, embedding_json=excluded.embedding_json,
                    embedding_model=excluded.embedding_model, updated_at=excluded.updated_at, version=excluded.version""",
                (role_id, body, json.dumps(vector), model_id, updated_at, version),
            )
        return {"roleId": role_id, "body": body, "updatedAt": updated_at, "version": version}

    def enqueue_understanding(self, role_id: str, payload: Mapping[str, object], *, full_refresh: bool = False) -> None:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT payload_json, full_refresh FROM owner_understanding_jobs WHERE role_id = ?", (role_id,)
            ).fetchone()
            merged = dict(payload)
            if existing is not None:
                try:
                    previous = json.loads(str(existing["payload_json"]))
                except json.JSONDecodeError:
                    previous = {}
                if isinstance(previous, dict):
                    merged["changedDocumentIds"] = list(dict.fromkeys([
                        *previous.get("changedDocumentIds", []), *merged.get("changedDocumentIds", [])
                    ]))
                    merged["deletedDocumentTitles"] = list(dict.fromkeys([
                        *previous.get("deletedDocumentTitles", []), *merged.get("deletedDocumentTitles", [])
                    ]))
                    if existing["full_refresh"]:
                        full_refresh = True
            connection.execute(
                """INSERT INTO owner_understanding_jobs(role_id, payload_json, full_refresh, status, updated_at)
                VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT(role_id) DO UPDATE SET payload_json=excluded.payload_json,
                    full_refresh=MAX(owner_understanding_jobs.full_refresh, excluded.full_refresh),
                    status='pending', updated_at=excluded.updated_at""",
                (role_id, json.dumps(merged, ensure_ascii=False), int(full_refresh), _now()),
            )

    def understanding_job(self, role_id: str) -> tuple[dict[str, object], bool, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, full_refresh, updated_at FROM owner_understanding_jobs WHERE role_id = ?", (role_id,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return (payload if isinstance(payload, dict) else {}, bool(row["full_refresh"]), str(row["updated_at"]))

    def finish_understanding(self, role_id: str, *, expected_updated_at: str | None = None) -> None:
        with self._connect() as connection:
            if expected_updated_at is None:
                connection.execute("DELETE FROM owner_understanding_jobs WHERE role_id = ?", (role_id,))
            else:
                connection.execute(
                    "DELETE FROM owner_understanding_jobs WHERE role_id = ? AND updated_at = ?",
                    (role_id, expected_updated_at),
                )

    def fail_understanding(self, role_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_understanding_jobs SET status = 'failed', attempts = attempts + 1, last_error = ?, updated_at = ? WHERE role_id = ?",
                (error[:200], _now(), role_id),
            )

    def defer_understanding(self, role_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_understanding_jobs SET status = 'pending', updated_at = ? WHERE role_id = ?",
                (_now(), role_id),
            )

    def pending_understandings(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT role_id FROM owner_understanding_jobs WHERE status = 'pending' ORDER BY updated_at"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def resume_failed_understandings(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_understanding_jobs SET status = 'pending', updated_at = ? WHERE status = 'failed'",
                (_now(),),
            )

    def delete_role(self, role_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM owner_understandings WHERE role_id = ?", (role_id,))
            connection.execute("DELETE FROM owner_understanding_jobs WHERE role_id = ?", (role_id,))

    def snapshot_role(self, role_id: str) -> dict[str, object]:
        with self._connect() as connection:
            understanding = connection.execute(
                "SELECT * FROM owner_understandings WHERE role_id = ?", (role_id,)
            ).fetchone()
            job = connection.execute(
                "SELECT * FROM owner_understanding_jobs WHERE role_id = ?", (role_id,)
            ).fetchone()
        return {
            "understanding": dict(understanding) if understanding else None,
            "job": dict(job) if job else None,
        }

    def restore_role(self, role_id: str, snapshot: Mapping[str, object]) -> None:
        understanding = snapshot.get("understanding")
        job = snapshot.get("job")
        with self._connect() as connection:
            if isinstance(understanding, Mapping):
                connection.execute(
                    """INSERT OR REPLACE INTO owner_understandings(role_id, body, embedding_json, embedding_model, updated_at, version)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    tuple(understanding[key] for key in ("role_id", "body", "embedding_json", "embedding_model", "updated_at", "version")),
                )
            if isinstance(job, Mapping):
                connection.execute(
                    """INSERT OR REPLACE INTO owner_understanding_jobs(role_id, payload_json, full_refresh, status, attempts, last_error, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    tuple(job[key] for key in ("role_id", "payload_json", "full_refresh", "status", "attempts", "last_error", "updated_at")),
                )

    def clear_source(self) -> None:
        app_id = self.get_state("feishu_app_id")
        with self._connect() as connection:
            chunk_ids = [str(row[0]) for row in connection.execute("SELECT chunk_id FROM owner_chunks")]
        with self._connect() as connection:
            connection.execute("DELETE FROM owner_chunks_fts")
            connection.execute("DELETE FROM owner_chunks")
            connection.execute("DELETE FROM owner_documents")
            connection.execute("DELETE FROM owner_understandings")
            connection.execute("DELETE FROM owner_understanding_jobs")
            connection.execute("DELETE FROM owner_sync_runs")
            connection.execute("DELETE FROM owner_plugin_state")
        if app_id:
            self.set_state("feishu_app_id", app_id)
        self.set_state("feishu_access_token", "")
        self.set_state("feishu_refresh_token", "")
        if self.vector_index is not None:
            for chunk_id in chunk_ids:
                self.vector_index.delete(chunk_id)

    def create_sync_run(self, trigger: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO owner_sync_runs(trigger, status, started_at) VALUES (?, 'running', ?)",
                (trigger, _now()),
            )
            return int(cursor.lastrowid)

    def list_sync_runs(self, limit: int = 20) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM owner_sync_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)
            ).fetchall()
        return [{
            "id": int(row["id"]),
            "trigger": str(row["trigger"]),
            "status": str(row["status"]),
            "startedAt": str(row["started_at"]),
            "finishedAt": str(row["finished_at"] or ""),
            "scanned": int(row["scanned"]),
            "created": int(row["created"]),
            "updated": int(row["updated"]),
            "deleted": int(row["deleted"]),
            "skipped": int(row["skipped"]),
            "failed": int(row["failed"]),
            "unchanged": int(row["unchanged"]),
            "errors": json.loads(str(row["errors_json"])),
        } for row in rows]

    def latest_sync_report(self) -> SyncReport | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM owner_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return SyncReport(
            trigger=str(row["trigger"]),
            complete=str(row["status"]) == "succeeded",
            created=int(row["created"]),
            updated=int(row["updated"]),
            deleted=int(row["deleted"]),
            skipped=int(row["skipped"]),
            failed=int(row["failed"]),
            unchanged=int(row["unchanged"]),
            errors=tuple(json.loads(str(row["errors_json"]))),
            finished_at=str(row["finished_at"] or ""),
        )

    def finish_sync_run(self, run_id: int, report: SyncReport, scanned: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE owner_sync_runs SET status = ?, finished_at = ?, scanned = ?, created = ?,
                    updated = ?, deleted = ?, skipped = ?, failed = ?, unchanged = ?, errors_json = ?
                WHERE id = ?""",
                (
                    "succeeded" if report.complete else "partial_error",
                    report.finished_at,
                    scanned,
                    report.created,
                    report.updated,
                    report.deleted,
                    report.skipped,
                    report.failed,
                    report.unchanged,
                    json.dumps(report.errors, ensure_ascii=False),
                    run_id,
                ),
            )


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return -1.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _fts_terms(text: str) -> str:
    terms: list[str] = []
    for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold()):
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            if len(token) == 1:
                terms.append(token)
            else:
                terms.extend(token[index:index + 2] for index in range(len(token) - 1))
        else:
            terms.append(token)
    return " ".join(terms)


def chunk_document(text: str, title: str, *, max_chars: int = _DEFAULT_CHUNK_CHARS) -> list[tuple[str, str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    current_size = 0
    current_location = "段落 1"
    paragraph_number = 0
    for paragraph in paragraphs:
        paragraph_number += 1
        pieces = [paragraph[index:index + max_chars] for index in range(0, len(paragraph), max_chars)]
        for piece in pieces:
            if current and current_size + len(piece) + 1 > max_chars:
                chunks.append(("\n".join(current), current_location))
                current = []
                current_size = 0
            if not current:
                current_location = f"段落 {paragraph_number}"
            current.append(piece)
            current_size += len(piece) + 1
    if current:
        chunks.append(("\n".join(current), current_location))
    return chunks


class OwnerKnowledgePlugin:
    """Application-level lifecycle for the owner knowledge feature."""

    def __init__(self, store: OwnerKnowledgeStore, connector: FeishuReader, embedder: Embedder) -> None:
        self.store = store
        self.connector = connector
        self.embedder = embedder
        self._sync_lock = asyncio.Lock()
        self._last_sync: SyncReport | None = None
        self._last_error: str | None = None
        self._task_lock = threading.Lock()
        self._role_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._understanding_workers: set[asyncio.Task[dict[str, object]]] = set()
        self._deleting_roles: set[str] = set()
        self._pause_requested = False

    def status(self) -> OwnerKnowledgeStatus:
        return OwnerKnowledgeStatus(
            enabled=self.store.is_enabled(),
            connected=self.store.get_state("feishu_access_token") != "",
            last_sync=self._last_sync or self.store.latest_sync_report(),
            last_error=self._last_error or self.store.get_state("last_error") or None,
        )

    def record_error(self, message: str) -> None:
        self._last_error = message[:300]
        self.store.set_state("last_error", self._last_error)

    async def begin_role_deletion(self, role_id: str) -> None:
        with self._task_lock:
            self._deleting_roles.add(role_id)
            tasks = tuple(
                task for task in self._understanding_workers
                if task.get_name() == f"owner-understanding:{role_id}"
            )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def end_role_deletion(self, role_id: str) -> None:
        with self._task_lock:
            self._deleting_roles.discard(role_id)

    async def enable(self) -> SyncReport:
        if self.store.get_state("feishu_access_token") == "":
            raise RuntimeError("请先连接飞书")
        self.store.set_state("enabled", "false")
        report = await self._sync("resume")
        if not report.complete:
            error = "; ".join(report.errors) or "飞书补同步未完整完成"
            self._last_error = error
            raise RuntimeError(error)
        self.store.set_state("enabled", "true")
        if not self.store.get_state("last_full_refresh_at"):
            self.store.set_state("last_full_refresh_at", _now())
        self._last_error = None
        self.store.set_state("last_error", "")
        return report

    async def startup(self) -> SyncReport | None:
        if not self.store.is_enabled():
            return None
        self.store.resume_failed_understandings()
        self.store.set_state("enabled", "false")
        report = await self._sync("startup")
        if report.complete:
            self.store.set_state("enabled", "true")
            if not self.store.get_state("last_full_refresh_at"):
                self.store.set_state("last_full_refresh_at", _now())
            self._last_error = None
            self.store.set_state("last_error", "")
        else:
            self._last_error = "; ".join(report.errors) or "启动补同步未完整完成"
            self.store.set_state("last_error", self._last_error)
        return report

    def queue_understanding_refresh(
        self,
        role_id: str,
        role: object,
        model_adapter: object,
        configuration: object | None,
        memory_text: str,
        *,
        changed_document_ids: Iterable[str] = (),
        deleted_document_titles: Iterable[str] = (),
        full_refresh: bool = False,
    ) -> None:
        if not self.store.is_enabled() or role_id in self._deleting_roles:
            return
        changed_document_ids = tuple(changed_document_ids)
        deleted_document_titles = tuple(deleted_document_titles)
        with self._task_lock:
            running = any(task.get_name() == f"owner-understanding:{role_id}" for task in self._understanding_workers)
        if running and not (changed_document_ids or deleted_document_titles or full_refresh):
            return
        self.store.enqueue_understanding(role_id, {
            "kind": "full" if full_refresh else "incremental",
            "changedDocumentIds": list(changed_document_ids),
            "deletedDocumentTitles": list(deleted_document_titles),
        }, full_refresh=full_refresh)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if running:
            return
        task = loop.create_task(
            self.generate_understanding(role, model_adapter, configuration, memory_text),
            name=f"owner-understanding:{role_id}",
        )
        self._understanding_workers.add(task)
        def complete(done: asyncio.Task[dict[str, object]]) -> None:
            self._understanding_workers.discard(done)
            if done.cancelled() or done.exception() is not None or not self.store.is_enabled():
                return
            if role_id not in self.store.pending_understandings():
                return
            job = self.store.understanding_job(role_id)
            if job is None:
                return
            payload, full, _ = job
            self.queue_understanding_refresh(
                role_id,
                role,
                model_adapter,
                configuration,
                memory_text,
                changed_document_ids=payload.get("changedDocumentIds", []),
                deleted_document_titles=payload.get("deletedDocumentTitles", []),
                full_refresh=full,
            )
        task.add_done_callback(complete)

    async def close(self) -> None:
        tasks = tuple(self._understanding_workers)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._understanding_workers.clear()

    async def pause(self) -> None:
        self._pause_requested = True
        self.store.set_state("enabled", "false")

    async def sync(self) -> SyncReport:
        if not self.store.is_enabled():
            raise RuntimeError("主人资料插件已暂停")
        return await self._sync("manual")

    async def _sync(self, trigger: str, *, force_read_all: bool = False) -> SyncReport:
        async with self._sync_lock:
            if trigger in {"resume", "startup"}:
                self._pause_requested = False
            run_id = self.store.create_sync_run(trigger)
            created = updated = deleted = skipped = failed = unchanged = 0
            errors: list[str] = []
            changed_document_ids: list[str] = []
            deleted_titles: list[str] = []
            try:
                scan = await self.connector.scan()
                errors.extend(scan.errors)
                seen: set[str] = set()
                for document in scan.documents:
                    if self._pause_requested:
                        errors.append("主人资料插件已暂停")
                        break
                    seen.add(document.document_id)
                    if document.object_type not in _SUPPORTED_TYPES:
                        self.store.mark_skipped(document)
                        skipped += 1
                        continue
                    try:
                        if not force_read_all and not self.store.needs_update(document, self.embedder.model_id):
                            unchanged += 1
                            continue
                        content = await self.connector.read_document(document)
                        previous_hash = self.store.document_content_hash(document.document_id)
                        if previous_hash == (_sha256(content), self.embedder.model_id):
                            self.store.touch_unchanged_document(document)
                            unchanged += 1
                            continue
                        pieces = chunk_document(content, document.title)
                        vectors = await asyncio.to_thread(
                            self.embedder.embed_documents,
                            [piece for piece, _ in pieces],
                        ) if pieces else []
                        if len(vectors) != len(pieces):
                            raise RuntimeError("embedding 输出数量与文档片段不匹配")
                        chunks = [
                            _Chunk(
                                chunk_id=_sha256(f"{document.document_id}:{self.embedder.model_id}:{index}:{_sha256(piece)}"),
                                ordinal=index,
                                text=piece,
                                location=location,
                                vector=tuple(float(value) for value in vector),
                            )
                            for index, ((piece, location), vector) in enumerate(zip(pieces, vectors))
                        ]
                        existed = any(item.document_id == document.document_id for item in self.store.list_documents())
                        self.store.replace_document(document, content, chunks, self.embedder.model_id)
                        changed_document_ids.append(document.document_id)
                        if existed:
                            updated += 1
                        else:
                            created += 1
                    except Exception as error:
                        failed += 1
                        errors.append(f"{document.title}: {type(error).__name__}: {error}")
                if self._pause_requested:
                    errors.append("主人资料插件已暂停")
                complete = scan.complete and not errors
                if complete:
                    deleted_documents = self.store.delete_missing_documents(seen)
                    deleted = len(deleted_documents)
                    deleted_titles.extend(title for _, title in deleted_documents)
                report = SyncReport(
                    trigger=trigger,
                    complete=complete,
                    created=created,
                    updated=updated,
                    deleted=deleted,
                    skipped=skipped,
                    failed=failed,
                    unchanged=unchanged,
                    errors=tuple(errors),
                    finished_at=_now(),
                    changed_document_ids=tuple(changed_document_ids),
                    deleted_document_titles=tuple(deleted_titles),
                )
                self._last_sync = report
                self._last_error = "; ".join(errors) if errors else None
                self.store.finish_sync_run(run_id, report, len(scan.documents))
                self.store.set_state("last_error", self._last_error or "")
                if report.complete:
                    self.store.set_state("last_sync_at", report.finished_at)
                return report
            except Exception as error:
                report = SyncReport(
                    trigger=trigger,
                    complete=False,
                    failed=failed + 1,
                    errors=tuple((*errors, f"{type(error).__name__}: {error}")),
                    finished_at=_now(),
                )
                self._last_sync = report
                self._last_error = "; ".join(report.errors)
                self.store.set_state("last_error", self._last_error)
                self.store.finish_sync_run(run_id, report, 0)
                return report

    async def search(self, query: str, *, role_id: str, limit: int = 5) -> list[OwnerKnowledgeHit]:
        if not self.store.is_enabled():
            return []
        query = query.strip()
        if not query or len(query) > _MAX_QUERY_LENGTH:
            return []
        bounded_limit = max(1, min(limit, _MAX_RESULT_LIMIT))
        vector = await asyncio.to_thread(self.embedder.embed_query, query)
        understanding = self.store.search_understanding(role_id, vector, self.embedder.model_id)
        hits = self.store.search(query, vector, self.embedder.model_id, max(1, bounded_limit - int(understanding is not None)))
        if understanding is not None:
            hits.insert(0, understanding)
        bounded_hits: list[OwnerKnowledgeHit] = []
        remaining = 4000
        for hit in hits[:bounded_limit]:
            if remaining <= 0:
                break
            body = hit.text[:remaining]
            bounded_hits.append(OwnerKnowledgeHit(
                kind=hit.kind,
                text=body,
                document_id=hit.document_id,
                title=hit.title,
                url=hit.url,
                location=hit.location,
                score=hit.score,
            ))
            remaining -= len(body)
        return bounded_hits

    async def generate_understanding(self, role: object, model_adapter: object, configuration: object | None, memory_text: str = "") -> dict[str, object]:
        """Generate one role's private, replaceable understanding atomically."""
        role_id = str(getattr(role, "id", ""))
        loop = asyncio.get_running_loop()
        with self._task_lock:
            lock = self._role_locks.setdefault((id(loop), role_id), asyncio.Lock())
        async with lock:
            return await self._generate_understanding_locked(role_id, role, model_adapter, configuration, memory_text)

    async def _generate_understanding_locked(self, role_id: str, role: object, model_adapter: object, configuration: object | None, memory_text: str) -> dict[str, object]:
        if not self.store.is_enabled() or role_id in self._deleting_roles:
            raise RuntimeError("主人资料插件已暂停")
        complete_messages = getattr(model_adapter, "complete_messages", None)
        if not callable(complete_messages):
            raise RuntimeError("当前模型连接不支持角色理解生成")
        profile = getattr(role, "profile", None)
        role_name = str(getattr(role, "name", "角色"))
        self.store.enqueue_understanding(role_id, {"kind": "generation"})
        try:
            job = self.store.understanding_job(role_id)
            job_payload, full_refresh, job_updated_at = job if job is not None else ({}, True, "")
            changed_ids = job_payload.get("changedDocumentIds", [])
            deleted_titles = job_payload.get("deletedDocumentTitles", [])
            changed_document_ids = (
                None
                if full_refresh or not isinstance(changed_ids, list) or not changed_ids
                else changed_ids
            )
            total_chunks = self.store.count_chunks(changed_document_ids) if changed_document_ids is not None else self.store.count_chunks()
            if total_chunks == 0:
                raise RuntimeError("没有可用于生成角色理解的主人资料")
            old = self.store.get_understanding(role_id)
            intermediate: list[str] = []
            batch_size = 12
            for batch_number, offset in enumerate(range(0, total_chunks, batch_size), 1):
                batch = self.store.list_chunks(
                    limit=batch_size,
                    offset=offset,
                    document_ids=changed_document_ids,
                )
                evidence = "\n".join(
                    f"[{item.title or '主人资料'} / {item.location or '正文'}]\n{item.text}"
                    for item in batch
                )
                prompt = (
                    "阅读本批主人资料，结合角色设定，提炼帮助角色理解主人的观察。"
                    "不要复述日记原文，不要把推测写成确定事实，只输出简短观察。\n"
                    f"角色：{role_name}\n角色设定：{getattr(profile, 'profile', '')}\n"
                    f"性格：{getattr(profile, 'personality', '')}\n批次：{batch_number}/{(total_chunks + batch_size - 1) // batch_size}\n"
                    f"新增、修改或删除的资料标题：{', '.join(str(item) for item in deleted_titles)[:1000]}\n"
                    f"资料：{evidence[:14000]}"
                )
                partial = await complete_messages(
                    [{"role": "system", "content": "你是一个谨慎的角色理解整理器。"}, {"role": "user", "content": prompt}],
                    configuration,
                    max_tokens=700,
                )
                if not str(partial).strip():
                    raise RuntimeError("角色理解中间结果为空")
                intermediate.append(str(partial).strip()[:2500])
            final_prompt = (
                "把分批观察、角色既有记忆和上一版理解整合成一段角色视角下对主人的整体认识。"
                "这是主观理解，不要逐字复述资料，只输出正文。\n"
                f"角色设定：{getattr(profile, 'profile', '')}\n性格：{getattr(profile, 'personality', '')}\n"
                f"行为规则：{getattr(profile, 'behaviorRules', '')}\n既有记忆：{memory_text[:8000]}\n"
                f"上一版：{str(old.get('body', ''))[:4000] if old else ''}\n"
                f"分批观察：{'\n'.join(intermediate)}"
            )
            result = await complete_messages(
                [{"role": "system", "content": "你是一个谨慎的角色理解整理器。"}, {"role": "user", "content": final_prompt}],
                configuration,
                max_tokens=1200,
            )
            body = str(result).strip()
            if not body or len(body) > 12000:
                raise RuntimeError("角色理解输出无效")
            if role_id in self._deleting_roles:
                raise RuntimeError("角色正在删除")
            vector = await asyncio.to_thread(self.embedder.embed_query, body)
            if role_id in self._deleting_roles:
                raise RuntimeError("角色正在删除")
            saved = self.store.save_understanding(role_id, body, vector, self.embedder.model_id)
            self.store.finish_understanding(role_id, expected_updated_at=job_updated_at)
            return saved
        except Exception as error:
            if self._pause_requested and role_id not in self._deleting_roles:
                self.store.defer_understanding(role_id)
            else:
                self.store.fail_understanding(role_id, type(error).__name__)
            raise


class ApplicationPluginManager:
    """Own lifecycle and independently controlled built-in app plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, OwnerKnowledgePlugin] = {}

    def register(self, plugin_id: str, plugin: OwnerKnowledgePlugin) -> None:
        if not plugin_id.strip() or plugin_id in self._plugins:
            raise ValueError("application plugin ID must be unique and non-empty")
        self._plugins[plugin_id] = plugin

    def get(self, plugin_id: str) -> OwnerKnowledgePlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as error:
            raise KeyError(plugin_id) from error

    def list(self) -> list[dict[str, object]]:
        return [
            {"id": plugin_id, "kind": "application", **asdict(plugin.status())}
            for plugin_id, plugin in self._plugins.items()
        ]

    async def enable(self, plugin_id: str) -> SyncReport:
        return await self.get(plugin_id).enable()

    async def pause(self, plugin_id: str) -> None:
        await self.get(plugin_id).pause()


class OwnerKnowledgeScheduler:
    """Daily scan and restart catch-up for the application plugin."""

    def __init__(
        self,
        plugin: OwnerKnowledgePlugin,
        on_change: Callable[[str | None, tuple[str, ...], tuple[str, ...], bool], object],
        *,
        interval_seconds: float = 24 * 60 * 60,
    ) -> None:
        self.plugin = plugin
        self.on_change = on_change
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = asyncio.Event()
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        self._stopping.set()
        if task.done():
            return
        if task.get_loop() is not asyncio.get_running_loop():
            task.cancel()
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while not self._stopping.is_set():
            if self.plugin.status().enabled:
                try:
                    last_sync = self.plugin.store.get_state("last_sync_at")
                    last_full_refresh = self.plugin.store.get_state("last_full_refresh_at")
                    due = not last_sync
                    if last_sync:
                        try:
                            last = datetime.fromisoformat(last_sync)
                            due = (datetime.now(timezone.utc) - last).total_seconds() >= self.interval_seconds
                        except ValueError:
                            due = True
                    full_due = not last_full_refresh
                    if last_full_refresh:
                        try:
                            last_full = datetime.fromisoformat(last_full_refresh)
                            full_due = (datetime.now(timezone.utc) - last_full).total_seconds() >= 7 * self.interval_seconds
                        except ValueError:
                            full_due = True
                    due = due or full_due
                    if due:
                        report = await self.plugin._sync("startup" if not last_sync else "scheduled", force_read_all=full_due)
                        if not report.complete and not last_sync:
                            await self.plugin.pause()
                        elif report.created or report.updated or report.deleted:
                            self.on_change(None, report.changed_document_ids, report.deleted_document_titles, False)
                        if report.complete and full_due:
                            self.on_change(None, (), (), True)
                            self.plugin.store.set_state("last_full_refresh_at", _now())
                except Exception:
                    pass
                for role_id in self.plugin.store.pending_understandings():
                    if not self.plugin.status().enabled or self._stopping.is_set():
                        break
                    try:
                        result = self.on_change(role_id, (), (), False)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        continue
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass


def feishu_authorization_url(app_id: str, redirect_uri: str, state: str) -> str:
    if not app_id.strip() or not redirect_uri.strip():
        raise ValueError("Feishu OAuth app ID and redirect URI are required")
    query = urlencode({
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "scope": "wiki:wiki:readonly docx:document:readonly",
    })
    return f"https://open.feishu.cn/open-apis/authen/v1/authorize?{query}"


class RoleBoundOwnerKnowledgeReader:
    """Narrow read-only port bound to one runtime role."""

    def __init__(self, role_id: str, search: Callable[..., object]) -> None:
        self.role_id = role_id
        self._search = search

    async def search(self, query: str, *, role_id: str, limit: int) -> object:
        if role_id != self.role_id:
            raise ValueError("owner knowledge reader is bound to the current role")
        result = self._search(query, role_id=self.role_id, limit=limit)
        return await result if asyncio.iscoroutine(result) else result


class OwnerKnowledgeTool:
    """Role-bound, read-only search tool for the Agent Runtime."""

    definition = ToolDefinition(
        name="search_owner_knowledge",
        description="按需检索主人同步到本地的资料以及当前角色对主人的想法，只读且仅限当前角色。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": _MAX_QUERY_LENGTH},
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_RESULT_LIMIT},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk="read_only",
        source="builtin:owner-knowledge",
        version="1.0.0",
        exposure="direct",
        timeout_seconds=20.0,
        output_limit=_MAX_TOOL_OUTPUT,
    )

    def __init__(self, reader: object) -> None:
        self._reader = reader

    async def execute(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
        on_update=None,
    ) -> AgentToolResult:
        del on_update
        context.signal.raise_if_cancelled()
        if set(arguments) - {"query", "limit"}:
            raise ValueError("unsupported owner knowledge search argument")
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query.strip()) > _MAX_QUERY_LENGTH:
            raise ValueError("query must be a non-empty string within the length limit")
        limit = arguments.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_RESULT_LIMIT:
            raise ValueError(f"limit must be an integer between 1 and {_MAX_RESULT_LIMIT}")
        result = self._reader.search(query.strip(), role_id=context.role_id, limit=limit)
        if asyncio.iscoroutine(result):
            result = await result
        context.signal.raise_if_cancelled()
        payload = {"query": query.strip(), "results": [_hit_payload(item) for item in list(result)[:limit]]}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > _MAX_TOOL_OUTPUT:
            payload["results"] = payload["results"][:2]
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return AgentToolResult(encoded, details={"status": "succeeded", "count": len(payload["results"])})


def _hit_payload(hit: object) -> dict[str, object]:
    if isinstance(hit, Mapping):
        value = dict(hit)
    else:
        value = {
            "kind": getattr(hit, "kind", "owner_knowledge"),
            "text": getattr(hit, "text", ""),
            "documentId": getattr(hit, "document_id", None),
            "title": getattr(hit, "title", None),
            "url": getattr(hit, "url", None),
            "location": getattr(hit, "location", None),
            "score": getattr(hit, "score", 0.0),
        }
    text = value.get("text", "")
    return {
        "kind": str(value.get("kind", "owner_knowledge"))[:40],
        "text": str(text)[:2000],
        "documentId": str(value["document_id"] if "document_id" in value else value.get("documentId", ""))[:200],
        "title": str(value.get("title", ""))[:300],
        "url": str(value.get("url", ""))[:1000],
        "location": str(value.get("location", ""))[:200],
        "score": float(value.get("score", 0.0)),
    }
