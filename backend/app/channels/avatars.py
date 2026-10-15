"""Bounded, seven-day avatar cache owned by the channel host.

Platforms fetch photos; this cache validates and thumbnails them. Identity and
permission checks never depend on a cached photo or a platform display name.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import warnings
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps

if TYPE_CHECKING:
    from ..multichannel import ChannelAccount, MultichannelStore

logger = logging.getLogger(__name__)
MAX_AVATAR_BYTES = 1024 * 1024
AVATAR_TTL = timedelta(days=7)
AVATAR_SIZE = 128
_AVATAR_HOST_SUFFIXES = (".qlogo.cn", ".qpic.cn", ".qq.com", ".qq.com.cn", ".gtimg.cn", ".feishucdn.com", ".larksuitecdn.com", ".byteimg.com", ".feishu.cn", ".larksuite.com")


async def download_avatar(url: str) -> bytes:
    """Fetch only platform CDN photos, with no redirects or credential headers."""

    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or not any(host.endswith(suffix) and host != suffix[1:] for suffix in _AVATAR_HOST_SUFFIXES):
        raise ValueError("头像地址不在支持的平台 CDN 内")
    content = bytearray()
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > MAX_AVATAR_BYTES:
                raise ValueError("头像超过大小限制")
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > MAX_AVATAR_BYTES:
                    raise ValueError("头像超过大小限制")
                content.extend(chunk)
    return bytes(content)


def thumbnail(content: bytes) -> bytes:
    if not content or len(content) > MAX_AVATAR_BYTES:
        raise ValueError("头像内容为空或超过大小限制")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as picture:
            if picture.format not in {"PNG", "JPEG", "GIF", "WEBP"} or picture.width * picture.height > 4_000_000:
                raise ValueError("头像格式或分辨率无效")
            picture.seek(0)
            resized = ImageOps.exif_transpose(picture).convert("RGBA")
            resized.thumbnail((AVATAR_SIZE, AVATAR_SIZE))
            output = io.BytesIO()
            resized.save(output, format="PNG")
            return output.getvalue()


class ChannelAvatarStore:
    def __init__(self, store: MultichannelStore) -> None:
        self.store = store
        self.root = Path(store.database_path).parent / "channel-avatars"
        self.tasks: dict[str, asyncio.Task[None]] = {}
        with store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS channel_avatars (
                avatar_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, account_id TEXT NOT NULL,
                kind TEXT NOT NULL, subject_id TEXT NOT NULL, attempted_at TEXT NOT NULL,
                file_name TEXT NOT NULL DEFAULT '', UNIQUE(account_id,kind,subject_id)
            )""")

    @staticmethod
    def key(account_id: str, kind: str, subject_id: str) -> str:
        if kind not in {"account", "sender", "chat"} or not account_id or not subject_id:
            raise ValueError("头像标识无效")
        return hashlib.sha256(f"{account_id}\0{kind}\0{subject_id}".encode()).hexdigest()

    def url(self, account_id: str, kind: str, subject_id: str) -> str | None:
        if not account_id or not subject_id:
            return None
        avatar_id = self.key(account_id, kind, subject_id)
        return f"/api/channels/avatars/{avatar_id}" if self.path(avatar_id) else None

    def path(self, avatar_id: str) -> Path | None:
        with self.store._connect() as db:
            row = db.execute("SELECT file_name FROM channel_avatars WHERE avatar_id=?", (avatar_id,)).fetchone()
        if row is None or not row[0]:
            return None
        path = self.root / str(row[0])
        return path if path.parent == self.root and path.is_file() else None

    def refresh(self, account: ChannelAccount, kind: str, subject_id: str, fetch: Callable[[], Awaitable[bytes | None]]) -> asyncio.Task[None] | None:
        """Schedule one due refresh; failures retain the previous cached photo."""

        if not subject_id:
            return None
        avatar_id = self.key(account.account_id, kind, subject_id)
        if avatar_id in self.tasks:
            return self.tasks[avatar_id]
        now = datetime.now(timezone.utc)
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT attempted_at FROM channel_avatars WHERE avatar_id=?", (avatar_id,)).fetchone()
            if row is not None and now - datetime.fromisoformat(str(row[0])) < AVATAR_TTL:
                return None
            db.execute("INSERT INTO channel_avatars(avatar_id,role_id,account_id,kind,subject_id,attempted_at) VALUES(?,?,?,?,?,?) ON CONFLICT(avatar_id) DO UPDATE SET attempted_at=excluded.attempted_at", (avatar_id, account.role_id, account.account_id, kind, subject_id, now.isoformat()))

        async def run() -> None:
            try:
                content = await fetch()
                if content is not None:
                    png = thumbnail(content)
                    self.root.mkdir(parents=True, exist_ok=True)
                    path = self.root / f"{avatar_id}.png"
                    temporary = self.root / f"{avatar_id}.tmp"
                    temporary.write_bytes(png)
                    temporary.replace(path)
                    with self.store._connect() as db:
                        db.execute("UPDATE channel_avatars SET file_name=? WHERE avatar_id=?", (path.name, avatar_id))
                else:
                    path = self.path(avatar_id)
                    if path:
                        path.unlink(missing_ok=True)
                    with self.store._connect() as db:
                        db.execute("UPDATE channel_avatars SET file_name='' WHERE avatar_id=?", (avatar_id,))
                self.store.publish(account.role_id, {"type": "avatars_updated", "roleId": account.role_id})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("渠道头像刷新失败 account=%s kind=%s error=%s", account.account_id, kind, type(error).__name__)
            finally:
                self.tasks.pop(avatar_id, None)

        task = asyncio.create_task(run())
        self.tasks[avatar_id] = task
        return task

    async def stop_account(self, account_id: str) -> None:
        with self.store._connect() as db:
            ids = [str(row[0]) for row in db.execute("SELECT avatar_id FROM channel_avatars WHERE account_id=?", (account_id,))]
        tasks = [self.tasks[key] for key in ids if key in self.tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def delete_role(self, role_id: str) -> None:
        with self.store._connect() as db:
            rows = db.execute("SELECT avatar_id,file_name FROM channel_avatars WHERE role_id=?", (role_id,)).fetchall()
            db.execute("DELETE FROM channel_avatars WHERE role_id=?", (role_id,))
        for row in rows:
            task = self.tasks.pop(str(row[0]), None)
            if task:
                task.cancel()
            if row[1]:
                (self.root / str(row[1])).unlink(missing_ok=True)
