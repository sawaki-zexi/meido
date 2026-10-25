from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import httpx

from .base import ChannelAdapterError

MAX_CHANNEL_MEDIA_BYTES = 10 * 1024 * 1024
_ALLOWED_MEDIA_SUFFIXES = (".qpic.cn", ".qq.com", ".qq.com.cn", ".gtimg.cn", ".myqcloud.com")


async def download_qq_media(url: str, *, max_bytes: int = MAX_CHANNEL_MEDIA_BYTES) -> tuple[bytes, str]:
    """Download QQ-hosted media without allowing redirects or private URLs."""

    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not host
        or address is not None
        or not any(host.endswith(suffix) and host != suffix[1:] for suffix in _ALLOWED_MEDIA_SUFFIXES)
    ):
        raise ChannelAdapterError("渠道附件地址不在可信 QQ 媒体域名内")
    content = bytearray()
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > max_bytes:
                raise ChannelAdapterError("渠道附件超过大小限制")
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > max_bytes:
                    raise ChannelAdapterError("渠道附件超过大小限制")
                content.extend(chunk)
            return bytes(content), response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]


def media_filename(name: str | None, mime_type: str | None) -> str:
    from pathlib import Path

    candidate = Path(name or "").name
    if Path(candidate).suffix:
        return candidate
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "text/csv": ".csv",
        "application/json": ".json",
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }.get((mime_type or "").split(";", 1)[0].lower(), "")
    return (candidate or "attachment") + suffix
