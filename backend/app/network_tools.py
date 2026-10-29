"""Role network tools; Shiori-compatible arguments with host-owned networking.

Adapted from Shiori-Agent 4f266ae, agent/tools/web_fetch.py and web_search.py.
Copyright (c) 2026 YinFengWindy. MIT license: third_party/shiori-agent/LICENSE.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import math
import re
import socket
import sys
import subprocess
import zlib
import codecs
from pathlib import Path
from contextlib import suppress
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx

from .agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from .network_extract import SSE_BOUNDARY, diagnostic

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
_ACCEPT = {
    "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
    "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
    "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, */*;q=0.1",
}


class NetworkToolError(Exception):
    """A safe, actionable network failure; never includes upstream text or secrets."""

    def __init__(self, code, message, hint, *, retryable=False, http_status=None):
        super().__init__(message)
        self.details = diagnostic(code, message, hint, retryable=retryable)
        if http_status is not None:
            self.details["http_status"] = http_status


def _exception_details(exc):
    if isinstance(exc, NetworkToolError):
        return exc.details
    if isinstance(exc, httpx.InvalidURL):
        return diagnostic("invalid_url", "网页地址或搜索端点无效", "检查 URL 格式与宿主搜索端点配置")
    if isinstance(exc, socket.gaierror):
        return diagnostic("dns_error", "域名解析失败", "检查本机 DNS 与网络连接后重试", retryable=True)
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return diagnostic("timeout", "网络操作超时", "检查网络连接后重试，或缩小请求范围", retryable=True)
    if isinstance(exc, httpx.ConnectError):
        return diagnostic("connect_error", "无法建立网络连接", "检查网络、TLS 证书与目标服务可达性后重试", retryable=True)
    if isinstance(exc, (httpx.HTTPError, OSError)):
        return diagnostic("transport_error", "网络传输失败", "检查网络连接与目标服务后重试", retryable=True)
    return diagnostic("invalid_response", "网络响应或协议内容无效", "检查来源与服务配置；稍后重试")


def _validate_arguments(definition, arguments):
    try:
        ToolRegistry.validate_definition(definition, dict(arguments))
    except ValueError:
        raise NetworkToolError("invalid_arguments", "联网工具参数无效", "按工具参数说明修正输入后重试") from None


@dataclass(frozen=True)
class NetworkToolsConfig:
    enabled: bool = True
    max_bytes: int = 5 * 1024 * 1024
    max_chars: int = 50_000
    timeout_seconds: float = 120
    search_provider: str = "exa_mcp"
    search_endpoint: str = field(default="", repr=False)
    search_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    search_parameters: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("network enabled must be a boolean")
        for name, ceiling in (("max_bytes", 5 * 1024 * 1024), ("max_chars", 50000)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise ValueError(f"invalid network {name}")
        if isinstance(self.timeout_seconds, bool) or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("invalid network timeout")
        if self.search_provider not in {"exa_mcp", "exa_api"}:
            raise ValueError("unsupported search provider")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.search_headers.items()):
            raise ValueError("search headers must be strings")
        if any(key.lower() in {"host", "cookie", "content-length", "accept-encoding"} for key in self.search_headers):
            raise ValueError("unsupported search header")

    @classmethod
    def from_environment(cls):
        return cls(enabled=os.getenv("MEIDO_NETWORK_ENABLED", "true").lower() not in {"0", "false", "off"},
            search_provider=os.getenv("MEIDO_WEB_SEARCH_PROVIDER", "exa_mcp"),
            search_endpoint=os.getenv("MEIDO_WEB_SEARCH_ENDPOINT", ""),
            search_headers=json.loads(os.getenv("MEIDO_WEB_SEARCH_HEADERS", "{}")),
            search_parameters=json.loads(os.getenv("MEIDO_WEB_SEARCH_PARAMETERS", "{}")))


async def _resolve(host: str, port: int) -> Sequence[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(record[4][0] for record in records))


def _result(data: dict, *, error: bool = False) -> AgentToolResult:
    return AgentToolResult(json.dumps(data, ensure_ascii=False), details=data, is_error=error)


def _media_type(headers: Mapping[str, str]) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _redact_search(value, config: NetworkToolsConfig):
    secrets = set(config.search_headers.values())
    for key, item in config.search_headers.items():
        if key.lower() == "authorization" and " " in item:
            secrets.add(item.split(" ", 1)[1])
    secrets.update(item for key, item in config.search_parameters.items() if isinstance(item, str)
                   and any(part in key.lower() for part in ("key", "token", "secret", "password", "authorization")))
    secrets.update(item for key, item in httpx.URL(config.search_endpoint).params.multi_items())

    def redact(item):
        if isinstance(item, str):
            for secret in sorted(filter(None, secrets), key=len, reverse=True):
                item = item.replace(secret, "[REDACTED]")
        elif isinstance(item, list):
            item = [redact(child) for child in item]
        elif isinstance(item, dict):
            item = {key: redact(child) for key, child in item.items()}
        return item

    return redact(value)


async def _bounded(operation, context: ToolContext, timeout: float):
    task = asyncio.create_task(operation)
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            if context.signal.cancelled:
                return _result({**diagnostic("cancelled", "网络操作已取消", "如仍需要，请重新发起请求"), "status": "cancelled"}, error=True)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return _result({**diagnostic("timeout", "网络操作超时（包含 DNS、下载和解析）", "检查网络后重试，或缩小请求范围", retryable=True), "status": "timed_out"}, error=True)
            done, _ = await asyncio.wait({task}, timeout=min(.02, remaining))
            if done:
                if asyncio.get_running_loop().time() >= deadline:
                    return _result({**diagnostic("timeout", "网络操作超时", "检查网络后重试，或缩小请求范围", retryable=True), "status": "timed_out"}, error=True)
                return task.result()
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
        if ip.ipv4_mapped is not None:
            return _public_address(str(ip.ipv4_mapped))
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return _public_address(str(ipaddress.IPv4Address(int(ip) & 0xffffffff)))
    return ip.is_global and not ip.is_multicast and not ip.is_unspecified


def _validate_url(url: httpx.URL) -> str:
    if url.scheme not in {"http", "https"} or not url.host or url.username or url.password:
        raise NetworkToolError("unsafe_url", "仅支持无凭据的 HTTP/HTTPS URL", "提供不含用户名和密码的完整 HTTP/HTTPS 地址")
    host = url.host.rstrip(".").lower()
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal")) or "%" in host:
        raise NetworkToolError("unsafe_url", "禁止访问本地域名", "请使用公开网页来源")
    return host


def _search_service_url(url: httpx.URL) -> httpx.URL:
    """The host, not the model, owns this fixed service destination.

    Keep the hostname for the host's proxy/TUN DNS, as Shiori does. This is
    never used for URLs supplied by a model or returned by a search service.
    """
    host = _validate_url(url)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _public_address(str(address)):
            raise NetworkToolError("unsafe_url", "搜索端点不能使用非公共 IP 地址", "请检查宿主搜索端点配置")
    return url.copy_with(fragment=None)


async def _pinned_url(url: httpx.URL, resolver: Resolver) -> httpx.URL:
    host = _validate_url(url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = await resolver(host, url.port or (443 if url.scheme == "https" else 80))
    else:
        addresses = [str(literal)]
    if not addresses:
        raise NetworkToolError("dns_error", "域名解析未返回地址", "检查本机 DNS 与网络连接后重试", retryable=True)
    if any(not _public_address(address) for address in addresses):
        raise NetworkToolError("unsafe_url", "禁止访问内网/本地或非公共地址", "请使用公开网页来源；不要通过重试绕过地址限制")
    return url.copy_with(host=addresses[0], fragment=None)


async def _download(config: NetworkToolsConfig, resolver: Resolver | None, transport, url: httpx.URL,
                    *, method="GET", headers=None, payload=None):
    # Only a host-owned search POST may use service DNS/proxy routing. Arbitrary
    # fetch URLs still validate and pin public DNS on every hop, without proxies.
    service = resolver is None and method == "POST"
    if resolver is None and not service:
        raise ValueError("public downloads require a resolver")
    async with httpx.AsyncClient(transport=transport, trust_env=service, follow_redirects=False, timeout=None) as client:
        for redirect in range(6):
            target = _search_service_url(url) if service else await _pinned_url(url, resolver)
            request_headers = {"User-Agent": "meido/1.0", "Accept-Encoding": "gzip, deflate, identity", **(headers or {}),
                               "Host": url.netloc.decode()}
            async with client.stream(method, target, headers=request_headers, json=payload,
                                     extensions={"sni_hostname": url.host}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect == 5 or "location" not in response.headers:
                        raise NetworkToolError("redirect_error", "重定向过多或缺少跳转目标", "请提供有效的最终网页地址")
                    next_url = url.join(response.headers["location"])
                    # Search credentials are never forwarded to a redirect.
                    if method != "GET":
                        raise NetworkToolError("redirect_error", "搜索服务重定向被拒绝", "请由宿主更新搜索端点；不会向跳转目标转发凭据")
                    url = next_url
                    client.cookies.clear()
                    continue
                if response.status_code != 200:
                    raise NetworkToolError("http_error", f"HTTP {response.status_code}",
                        "检查宿主搜索凭据或目标访问权限" if response.status_code in {401, 403} else
                        "服务限流，请稍后重试并检查配额" if response.status_code == 429 else
                        "目标服务暂时不可用，请稍后重试" if response.status_code >= 500 else
                        "检查网页地址或宿主搜索端点配置",
                        retryable=response.status_code in {408, 429} or response.status_code >= 500,
                        http_status=response.status_code)
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > config.max_bytes):
                    raise NetworkToolError("response_too_large", "响应过大或 Content-Length 无效", "缩小请求范围或选择其他来源")
                encoding = response.headers.get("content-encoding", "identity").lower()
                if encoding not in {"gzip", "deflate", "identity"}:
                    raise NetworkToolError("unsupported_content", "不支持此压缩响应编码", "选择支持 gzip、deflate 或未压缩内容的来源")
                predecoded = response.is_stream_consumed
                decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
                           if encoding != "identity" and not predecoded else None)

                async def chunks():
                    if predecoded:
                        yield response.content
                    else:
                        async for raw in response.aiter_raw():
                            yield raw

                body = bytearray()
                event_offset = 0
                wire_bytes = 0
                async for chunk in chunks():
                    await asyncio.sleep(0)  # Allow deadlines/cancellation even on a fast peer.
                    wire_bytes += len(chunk)
                    if wire_bytes > config.max_bytes:
                        raise NetworkToolError("response_too_large", "响应过大（超过下载字节上限）", "缩小请求范围或选择其他来源")
                    if decoder:
                        chunk = decoder.decompress(chunk, config.max_bytes - len(body) + 1)
                    if len(body) + len(chunk) > config.max_bytes:
                        raise NetworkToolError("response_too_large", "响应过大（超过解压后字节上限）", "缩小请求范围或选择其他来源")
                    body.extend(chunk)
                    if method == "POST" and _media_type(response.headers) == "text/event-stream":
                        while separator := re.search(SSE_BOUNDARY.encode(), body[event_offset:]):
                            end = event_offset + separator.start()
                            event = body[event_offset:end]
                            event_offset += separator.end()
                            data = b"\n".join(line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:"))
                            if data and data != b"[DONE]":
                                reply = json.loads(data)
                                if isinstance(reply, dict) and type(reply.get("id")) is int and reply["id"] == 1 and ("result" in reply or "error" in reply):
                                    return bytes(body[:event_offset]), response.headers, str(url), response.status_code
                if decoder and (not decoder.eof or decoder.unused_data):
                    raise NetworkToolError("invalid_response", "压缩响应不完整或有额外压缩成员", "稍后重试或选择其他来源")
                return bytes(body), response.headers, str(url), response.status_code
    raise NetworkToolError("redirect_error", "重定向失败", "请提供有效的最终网页地址")


async def download_public_bytes(url: str) -> tuple[bytes, httpx.Headers, str]:
    """Fetch explicit outgoing image bytes with the existing public-network boundary."""
    config = NetworkToolsConfig.from_environment()
    if not config.enabled:
        raise ValueError('宿主未允许网络工具')
    try:
        async with asyncio.timeout(min(30, config.timeout_seconds)):
            body, headers, final_url, _status = await _download(config, _resolve, None, httpx.URL(url))
    except (NetworkToolError, httpx.HTTPError, OSError, TimeoutError) as error:
        details = _exception_details(error)
        raise ValueError(f"图片下载失败（{details['error_code']}）：{details['error']}") from None
    return body, headers, final_url


async def _convert(text: str, fmt: str, is_html: bool, limit: int):
    if fmt == "html" or not is_html:
        if not text.strip():
            raise NetworkToolError("empty_content", "网页正文为空", "请更换可读取的网页来源")
        return {"text": text[:limit], "truncated": len(text) > limit}
    return await _worker({"text": text, "format": fmt, "is_html": is_html, "limit": limit})


async def _worker(request):
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("network_extract.py")),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise NetworkToolError("parse_error", "无法启动内容解析进程", "请检查本机 Python 运行环境与进程权限") from None
    try:
        stdout, _ = await process.communicate(json.dumps(request, ensure_ascii=False).encode())
        if process.returncode != 0:
            raise NetworkToolError("parse_error", "内容解析失败或正文为空", "请更换可读取的来源；若持续失败，请检查本机解析依赖")
        return json.loads(stdout)
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class WebFetchTool:
    def __init__(self, config: NetworkToolsConfig | None = None, *, resolver: Resolver = _resolve,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config = config or NetworkToolsConfig()
        self.resolver = resolver
        self.transport = transport
        self.definition = ToolDefinition(
            name="web_fetch", description="抓取指定 HTTP/HTTPS 网页，返回来源和正文，支持 markdown、text、html。",
            input_schema={"type": "object", "properties": {
                "url": {"type": "string", "minLength": 1, "maxLength": 8192},
                "format": {"type": "string", "enum": ["text", "markdown", "html"]},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
            }, "required": ["url"], "additionalProperties": False},
            external_allowed=True, approval="auto" if self.config.enabled else "deny",
            timeout_seconds=self.config.timeout_seconds + 1, search_hint='读取网址 浏览网页',
        )

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None):
        timeout = arguments.get("timeout", 30)
        # Full schema validation still occurs in the operation before I/O.
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            timeout = 30
        return await _bounded(self._execute(arguments), context, min(timeout, self.config.timeout_seconds))

    async def _execute(self, arguments):
        try:
            _validate_arguments(self.definition, arguments)
            if not self.config.enabled:
                raise NetworkToolError("network_disabled", "宿主未允许网络工具", "请由主人启用宿主联网设置")
            url = httpx.URL(arguments["url"])
            fmt = arguments.get("format", "markdown")
            body, response_headers, final_url, status = await _download(self.config, self.resolver, self.transport, url,
                headers={"Accept": _ACCEPT[fmt], "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            content_type = _media_type(response_headers)
            if content_type and not (content_type.startswith("text/") or content_type in {
                    "application/json", "application/xml", "application/xhtml+xml", "application/javascript"}
                    or content_type.endswith(("+json", "+xml"))):
                raise NetworkToolError("unsupported_content", "不支持二进制或未知内容类型", "请使用处理该文件格式的专用工具")
            encoding = httpx.Response(200, headers={"content-type": response_headers.get("content-type", "")}).charset_encoding
            if not encoding:
                encoding = ("utf-32" if body.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)) else
                            "utf-16" if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig")
            try:
                text = body.decode(encoding).lstrip("\ufeff")
            except (LookupError, UnicodeError):
                raise NetworkToolError("invalid_response", "正文编码解析失败", "请更换可读取的网页来源") from None
            if any(ord(char) < 32 and char not in "\t\n\r" for char in text[:4096]):
                raise NetworkToolError("unsupported_content", "响应包含二进制内容", "请使用处理该文件格式的专用工具")
            converted = await _convert(text, fmt, content_type in {"text/html", "application/xhtml+xml"}, self.config.max_chars)
            text, truncated = converted["text"], converted["truncated"]
            return _result({"url": str(url), "final_url": final_url, "status": status,
                            "content_type": response_headers.get("content-type", ""),
                            "format": fmt, "length": len(text), "text": text, "truncated": truncated,
                            "note": "内容已截断；本工具不提供分页，请缩小来源范围" if truncated else ""})
        except (NetworkToolError, ValueError, httpx.HTTPError, httpx.InvalidURL, OSError, zlib.error) as exc:
            return _result(_exception_details(exc), error=True)


class WebSearchTool:
    def __init__(self, config: NetworkToolsConfig | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config = config or NetworkToolsConfig()
        self.transport = transport
        self.definition = ToolDefinition(
            name="web_search", description="搜索互联网的标题、摘要和来源 URL；拿到来源后可用 web_fetch 阅读。",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 10000},
                "num_results": {"type": "integer", "minimum": 1, "maximum": 20},
                "livecrawl": {"type": "string", "enum": ["fallback", "preferred"]},
                "type": {"type": "string", "enum": ["auto", "fast", "deep"]},
            }, "required": ["query"], "additionalProperties": False},
            external_allowed=True, approval="auto" if self.config.enabled else "deny",
            timeout_seconds=min(25, self.config.timeout_seconds) + 1, search_hint='谷歌 Bing 查资料',
        )

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None):
        result = await _bounded(self._execute(arguments), context, min(25, self.config.timeout_seconds))
        # Errors contain only fixed diagnostics, never the private query,
        # configured endpoint, credentials or raw provider response.
        data = {"provider": self.config.search_provider, **result.details}
        if not result.is_error:
            data = _redact_search({"query": arguments.get("query"), **data}, self.config)
        return _result(data, error=result.is_error)

    async def _execute(self, arguments):
        try:
            _validate_arguments(self.definition, arguments)
            if not self.config.enabled:
                raise NetworkToolError("network_disabled", "宿主未允许网络工具", "请由主人启用宿主联网设置")
            options = {**self.config.search_parameters,
                    "query": arguments["query"], "numResults": arguments.get("num_results", 8),
                    "livecrawl": arguments.get("livecrawl", "fallback"), "type": arguments.get("type", "auto"),
                }
            mcp = self.config.search_provider == "exa_mcp"
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "web_search_exa", "arguments": options}} if mcp else options
            endpoint = self.config.search_endpoint or ("https://mcp.exa.ai/mcp" if mcp else "https://api.exa.ai/search")
            body, headers, _, status = await _download(self.config, None, self.transport,
                httpx.URL(endpoint), method="POST", payload=payload,
                headers={"Accept": "application/json, text/event-stream", **self.config.search_headers})
            parsed = await _worker({"kind": "search", "text": body.decode("utf-8-sig"), "mcp": mcp,
                                   "sse": _media_type(headers) == "text/event-stream", "limit": self.config.max_chars})
            return _result({**parsed, "status": status}, error="error" in parsed)
        except (NetworkToolError, ValueError, httpx.HTTPError, OSError, TypeError, httpx.InvalidURL, zlib.error, RecursionError) as exc:
            return _result(_exception_details(exc), error=True)
