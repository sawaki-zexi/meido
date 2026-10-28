"""Admitted QQ text files: remote IDs are descriptors, never local paths."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import httpx

from .base import ChannelAdapterError
from .models import Attachment, DownloadedMedia
from .media import MediaTooLargeError, UntrustedMediaUrlError

MAX_FILE_BYTES = 2 * 1024 * 1024
FILE_TIMEOUT_SECONDS = 20.0


class FileRejectedError(ValueError):
    """A controlled, user-facing rejection; never contains remote URLs."""


def file_attachment(data: Mapping) -> Attachment:
    name = str(data.get("name") or data.get("file") or "未命名文件").replace("\\", "/").rsplit("/", 1)[-1]
    size = str(data.get("size") or data.get("file_size") or "")
    return Attachment("file", str(data.get("url") or "") or None, name,
        size=int(size) if size.isdecimal() else None,
        file_id=str(data.get("file_id") or data.get("id") or "") or None)


def file_description(name: str | None, error: str = "") -> str:
    return f"[文件：{name or '未命名文件'}" + (f"；{error}，未读取正文]" if error else "]")


def describe_failures(text: str, failures: list[dict]) -> str:
    for failure in failures:
        original = file_description(failure["name"])
        description = file_description(failure["name"], failure["reason"])
        text = text.replace(original, description, 1) if original in text else text + "\n" + description
    return text.strip()


async def download_file(attachment, event, transport, downloader, *, max_bytes):
    limit = min(MAX_FILE_BYTES, max_bytes)
    suffix = Path(attachment.file_name or "").suffix.lower()
    try:
        if suffix not in {".txt", ".md"}:
            raise FileRejectedError("仅支持 TXT/MD")
        if attachment.size is not None and attachment.size > limit:
            raise FileRejectedError("文件超过 2 MiB 上限")
        async with asyncio.timeout(FILE_TIMEOUT_SECONDS):
            check_media_authorization(event)
            url = attachment.url
            resolved = False
            if not url and attachment.file_id and transport is not None:
                operation = "get_group_file_url" if event.is_group else "get_private_file_url"
                params = {"file_id": attachment.file_id}
                if event.is_group:
                    params["group_id"] = event.chat_id
                # NapCat v4.18.28: both fields are strings; no `group` or busid.
                response = await transport(operation, params)
                check_media_authorization(event)
                if not isinstance(response, Mapping):
                    raise FileRejectedError("文件地址查询返回无效结果")
                payload = response.get("data") if isinstance(response.get("data"), Mapping) else response
                url = payload.get("url")
                resolved = True
            if not isinstance(url, str) or not url:
                raise FileRejectedError("没有可用下载地址")
            data, _mime = await downloader(url, max_bytes=limit, resolved=resolved)
            check_media_authorization(event)
            if len(data) > limit:
                raise FileRejectedError("文件超过 2 MiB 上限")
            if any(byte < 32 and byte not in (9, 10, 13) for byte in data):
                raise FileRejectedError("文件包含二进制内容")
            try:
                data.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise FileRejectedError("文件不是 UTF-8 文本") from None
        return DownloadedMedia("file", data, attachment.file_name,
            "text/markdown" if suffix == ".md" else "text/plain",
            file_id=attachment.file_id, source_size=attachment.size)
    except PermissionError:
        raise
    except FileRejectedError as error:
        reason = str(error)
    except MediaTooLargeError:
        reason = "文件超过 2 MiB 上限"
    except UntrustedMediaUrlError:
        reason = "文件下载地址不可信"
    except (TimeoutError, httpx.TimeoutException):
        reason = "文件接收超时"
    except (ChannelAdapterError, httpx.HTTPError, OSError, ValueError):
        reason = "文件下载失败或地址已失效"
    return DownloadedMedia("file", b"", attachment.file_name, error=reason,
        file_id=attachment.file_id, source_size=attachment.size)


def check_media_authorization(event) -> None:
    if event.authorize_media is not None and not event.authorize_media():
        raise PermissionError("渠道附件来源许可已失效")
