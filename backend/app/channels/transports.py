"""Concrete platform transports for channel adapters.

NapCat and QQBot use outbound WebSocket connections with bounded reconnect
backoff. Feishu uses an HTTP event endpoint because its bot event subscription
is configured as an HTTPS webhook; the host mounts ``handle_webhook``.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx
from websockets.exceptions import ConnectionClosed, PayloadTooBig

from .base import ChannelAdapterError, EventHandler, PlatformRejectedError
from .directory import DirectoryError, MAX_RESPONSE_BYTES, observed_now

logger = logging.getLogger(__name__)

WebSocketConnector = Callable[..., Any]


class FeishuLongConnectionRunner:
    """Run one long connection on a dedicated event loop and thread."""

    def __init__(self, factory: Callable[[Callable[[Mapping[str, Any]], None]], Any], on_event: Callable[[Mapping[str, Any]], None], *, reconnect_min: float = 1.0, reconnect_max: float = 30.0) -> None:
        self._factory = factory
        self._on_event = on_event
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self.connected = False
        self._online: bool | None = None
        self._observed_at: str | None = None
        self._connection_reason = "not_started"
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._wake: asyncio.Event | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def connection_snapshot(self) -> dict[str, Any]:
        connection = "connected" if self.connected else "connecting" if self.alive else "disconnected"
        return {"connection": connection, "online": self._online,
                "observedAt": self._observed_at, "reason": self._connection_reason}

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._online = None
        self._observed_at = observed_now()
        self._connection_reason = "connecting"
        self._thread = threading.Thread(target=self._run, name="feishu-long-connection", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 10)
        self.connected = False
        self._online = False
        self._observed_at = observed_now()
        self._connection_reason = "stopped"

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._wake = asyncio.Event()
        try:
            loop.run_until_complete(self._supervise())
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._loop = None
            self._wake = None

    async def _supervise(self) -> None:
        delay = self._reconnect_min
        while not self._stop.is_set():
            connection = None
            try:
                connection = self._factory(self._dispatch)
                await connection.connect()
                self.connected = True
                self._online = True
                self._observed_at = observed_now()
                self._connection_reason = "long_connection_connected"
                delay = self._reconnect_min
                keepalive = asyncio.create_task(connection.keepalive())
                try:
                    while not self._stop.is_set() and connection.is_connected():
                        await self._sleep(0.1)
                finally:
                    keepalive.cancel()
                    await asyncio.gather(keepalive, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected = False
            finally:
                self.connected = False
                self._online = False
                self._observed_at = observed_now()
                self._connection_reason = "stopped" if self._stop.is_set() else "connection_lost"
                if connection is not None:
                    try:
                        await asyncio.wait_for(connection.disconnect(), 5)
                    except Exception:
                        pass
            if not self._stop.is_set():
                await self._sleep(delay)
                delay = min(delay * 2, self._reconnect_max)

    async def _sleep(self, seconds: float) -> None:
        if self._wake is None:
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            pass

    def _dispatch(self, payload: Mapping[str, Any]) -> None:
        if not self._stop.is_set():
            self._on_event(payload)


def feishu_sdk_connection_factory(app_id: str, app_secret: str, endpoint: str) -> Callable[[Callable[[Mapping[str, Any]], None]], Any]:
    """Create a lazy lark-oapi SDK factory, keeping the optional SDK import local."""

    def factory(on_event: Callable[[Mapping[str, Any]], None]) -> Any:
        import importlib

        from lark_oapi import EventDispatcherHandler, LogLevel
        client_module = importlib.import_module("lark_oapi.ws.client")

        class LoopProxy:
            def create_task(self, coroutine: Any) -> asyncio.Task[Any]:
                return asyncio.get_running_loop().create_task(coroutine)
            def run_until_complete(self, awaitable: Any) -> Any:
                return asyncio.get_event_loop().run_until_complete(awaitable)

        client_module.loop = LoopProxy()
        Client = client_module.Client

        builder = EventDispatcherHandler.builder("", "")
        builder.register_p2_customized_event("im.message.receive_v1", lambda event: on_event(_feishu_event_payload(event)))
        client = Client(app_id, app_secret, log_level=LogLevel.WARNING, event_handler=builder.build(), domain=endpoint, auto_reconnect=False)

        class Connection:
            async def connect(self) -> None:
                await client._connect()
            def is_connected(self) -> bool:
                return client._conn is not None
            async def keepalive(self) -> None:
                await client._ping_loop()
            async def disconnect(self) -> None:
                await client._disconnect()

        return Connection()

    return factory


def _feishu_event_payload(event: Any) -> dict[str, Any]:
    header = getattr(event, "header", None)
    body = getattr(event, "event", None)
    return {"header": {"event_id": str(getattr(header, "event_id", "") or ""), "event_type": str(getattr(header, "event_type", "") or "")}, "event": _protobuf_mapping(body)}


def _protobuf_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    try:
        from google.protobuf.json_format import MessageToDict
        result = MessageToDict(value, preserving_proto_field_name=True)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


class ChannelTransportError(ChannelAdapterError):
    """A platform transport could not complete an operation."""


async def _invoke_event(handler: EventHandler | None, payload: Mapping[str, Any]) -> None:
    if handler is None:
        return
    try:
        await handler(payload)
    except Exception:
        logger.exception("渠道入站事件处理器失败")


class NapCatWebSocketTransport:
    """OneBot v11 forward-WebSocket client for NapCat."""

    def __init__(
        self,
        endpoint: str,
        *,
        access_token: str = "",
        connector: WebSocketConnector | None = None,
        request_timeout: float = 15,
        reconnect_min: float = 1,
        reconnect_max: float = 30,
    ) -> None:
        self.endpoint = endpoint
        self.access_token = access_token
        self.connector = connector
        self.request_timeout = request_timeout
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self.connected = False
        self._online: bool | None = None
        self._observed_at: str | None = None
        self._connection_reason = "not_started"
        self._handler: EventHandler | None = None
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Mapping[str, Any]]] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()

    def _connector(self) -> WebSocketConnector:
        if self.connector is not None:
            return self.connector
        from websockets.asyncio.client import connect

        return connect

    async def start(self, on_event: EventHandler) -> None:
        if self._task is not None and not self._task.done():
            self._handler = on_event
            return
        self._handler = on_event
        self._stopping.clear()
        self._online = None
        self._observed_at = observed_now()
        self._connection_reason = "connecting"
        self._task = asyncio.create_task(self._run(), name="napcat-onebot-websocket")

    def connection_snapshot(self) -> dict[str, Any]:
        connection = "connected" if self.connected else (
            "connecting" if self._task is not None and not self._task.done() and not self._stopping.is_set()
            else "disconnected"
        )
        return {"connection": connection, "online": self._online,
                "observedAt": self._observed_at, "reason": self._connection_reason}

    async def stop(self) -> None:
        self._stopping.set()
        ws = self._ws
        if ws is not None:
            await ws.close()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        event_tasks, self._event_tasks = self._event_tasks, set()
        for event_task in event_tasks:
            event_task.cancel()
        if event_tasks:
            await asyncio.gather(*event_tasks, return_exceptions=True)
        self.connected = False
        self._online = None
        self._observed_at = observed_now()
        self._connection_reason = "stopped"
        self._fail_pending(ChannelTransportError("NapCat WebSocket 已断开"))

    async def __call__(self, action: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        ws = self._ws
        if not self.connected or ws is None:
            raise ChannelTransportError("NapCat WebSocket 尚未连接")
        echo = uuid.uuid4().hex
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        request = {"action": action, "params": dict(params), "echo": echo}
        try:
            async with self._send_lock:
                await ws.send(json.dumps(request, ensure_ascii=False))
            result = await asyncio.wait_for(future, timeout=self.request_timeout)
            if action == "get_status" and self._ws is ws and self.connected and type(result.get("online")) is bool:
                self._online = result["online"]
                self._observed_at = observed_now()
                self._connection_reason = "status_online" if self._online else "status_offline"
            return result
        except TimeoutError:
            raise
        finally:
            self._pending.pop(echo, None)

    async def wait_connected(self, timeout: float = 10.0) -> None:
        """Wait for the reader connection before issuing an initialization RPC."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.connected:
            if self._stopping.is_set() or self._task is None or self._task.done():
                raise ChannelTransportError("NapCat WebSocket 未启动")
            if asyncio.get_running_loop().time() >= deadline:
                raise ChannelTransportError("NapCat WebSocket 连接超时")
            await asyncio.sleep(0.01)

    async def _run(self) -> None:
        delay = self.reconnect_min
        while not self._stopping.is_set():
            try:
                headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
                async with self._connector()(
                    self.endpoint,
                    additional_headers=headers,
                    open_timeout=10,
                    close_timeout=3,
                    ping_interval=20,
                    max_size=MAX_RESPONSE_BYTES,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    self._online = None
                    self._observed_at = observed_now()
                    self._connection_reason = "online_unknown"
                    delay = self.reconnect_min
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, DirectoryError):
                    self._fail_pending(error)
                    self._connection_reason = error.code
                elif isinstance(error, PayloadTooBig) or (
                    isinstance(error, ConnectionClosed)
                    and any(frame is not None and frame.code == 1009 for frame in (error.sent, error.rcvd))
                ):
                    self._fail_pending(DirectoryError("directory_too_large"))
                    self._connection_reason = "directory_too_large"
                else:
                    self._connection_reason = "connection_lost"
                if not self._stopping.is_set():
                    logger.warning("NapCat WebSocket 断开，将在 %.1f 秒后重连 (%s)", delay, type(error).__name__)
            finally:
                self.connected = False
                self._online = None
                self._observed_at = observed_now()
                self._ws = None
                self._fail_pending(ChannelTransportError("NapCat WebSocket 已断开"))
            if not self._stopping.is_set():
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max)

    async def _read_loop(self, ws: Any) -> None:
        while not self._stopping.is_set():
            raw = await ws.recv()
            if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_RESPONSE_BYTES:
                raise DirectoryError("directory_too_large")
            packet = json.loads(raw)
            if not isinstance(packet, dict):
                continue
            echo = packet.get("echo")
            if echo is not None:
                future = self._pending.get(str(echo))
                if future is not None and not future.done():
                    if int(packet.get("retcode", 0) or 0) != 0 or packet.get("status") == "failed":
                        future.set_exception(PlatformRejectedError("NapCat API 请求失败"))
                    else:
                        data = packet.get("data")
                        future.set_result(data if isinstance(data, dict) else {"items": data} if isinstance(data, list) else {})
            elif "post_type" in packet:
                # The handler may call OneBot APIs (for example while
                # refreshing the target directory).  Running it inline would
                # deadlock this reader because API responses are delivered by
                # the same loop.
                event_task = asyncio.create_task(_invoke_event(self._handler, packet))
                self._event_tasks.add(event_task)
                event_task.add_done_callback(self._event_tasks.discard)

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)


class QQBotGatewayTransport:
    """Official QQBot Gateway client and C2C REST sender."""

    TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

    def __init__(
        self,
        app_id: str,
        client_secret: str,
        *,
        api_endpoint: str = "https://api.sgroup.qq.com",
        token_endpoint: str = TOKEN_URL,
        connector: WebSocketConnector | None = None,
        http_client: Any | None = None,
        request_timeout: float = 20,
        reconnect_min: float = 1,
        reconnect_max: float = 30,
    ) -> None:
        if not app_id or not client_secret:
            raise ValueError("QQBot app_id 和 client_secret 必须配置")
        self.app_id = app_id
        self.client_secret = client_secret
        self.api_endpoint = api_endpoint.rstrip("/")
        self.token_endpoint = token_endpoint
        self.connector = connector
        self.request_timeout = request_timeout
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self._http = http_client or httpx.AsyncClient(timeout=request_timeout)
        self._owns_http = http_client is None
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._handler: EventHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._ws: Any = None
        self._connected = False
        self._online: bool | None = None
        self._observed_at: str | None = None
        self._connection_reason = "not_started"
        self._sequence: int | None = None
        self._session_id: str | None = None
        self._resume_gateway_url: str | None = None
        self._heartbeat_ack = asyncio.Event()
        self._request_timeout = request_timeout
        self._event_tasks: set[asyncio.Task[None]] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    def connection_snapshot(self) -> dict[str, Any]:
        connection = "connected" if self.connected else (
            "connecting" if self._task is not None and not self._task.done() and not self._stopping.is_set()
            else "disconnected"
        )
        return {"connection": connection, "online": self._online,
                "observedAt": self._observed_at, "reason": self._connection_reason}

    async def profile(self) -> Mapping[str, Any]:
        self._ensure_http_open()
        token = await self._access_token()
        response = await self._http.get(f"{self.api_endpoint}/users/@me", headers=self._auth_headers(token))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ChannelTransportError("QQBot 账号资料响应格式错误")
        return payload

    def _connector(self) -> WebSocketConnector:
        if self.connector is not None:
            return self.connector
        from websockets.asyncio.client import connect

        return connect

    async def start(self, on_event: EventHandler) -> None:
        if self._task is not None and not self._task.done():
            self._handler = on_event
            return
        self._handler = on_event
        self._stopping.clear()
        self._online = None
        self._observed_at = observed_now()
        self._connection_reason = "connecting"
        self._task = asyncio.create_task(self._run(), name="qqbot-gateway")

    async def stop(self) -> None:
        self._stopping.set()
        if self._ws is not None:
            await self._ws.close()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._connected = False
        self._online = False
        self._observed_at = observed_now()
        self._connection_reason = "stopped"
        if self._event_tasks:
            await asyncio.gather(*tuple(self._event_tasks), return_exceptions=True)
        if self._owns_http:
            await self._http.aclose()

    async def __call__(self, operation: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation not in {
            "send_c2c", "stream_c2c", "delete_c2c", "upload_c2c_file", "send_c2c_image",
        }:
            raise ChannelTransportError("QQBot 不支持该出站操作")
        self._ensure_http_open()
        token = await self._access_token()
        openid = quote(str(params.get("openid", "")), safe="")
        if not openid:
            raise ChannelTransportError("QQBot C2C 目标缺失")
        if operation == "delete_c2c":
            message_id = quote(str(params.get("message_id", "")), safe="")
            if not message_id:
                raise ChannelTransportError("QQBot C2C 消息 ID 缺失")
            response = await self._http.delete(
                f"{self.api_endpoint}/v2/users/{openid}/messages/{message_id}",
                headers=self._auth_headers(token),
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                payload = {}
            return payload if isinstance(payload, dict) else {}
        elif operation == "upload_c2c_file":
            raw_body = params.get("body")
            if not isinstance(raw_body, Mapping):
                raise ChannelTransportError("QQBot 图片上传请求体缺失")
            body = dict(raw_body)
            if body.get("file_type") != 1 or not body.get("file_data") or body.get("srv_send_msg") is not False:
                raise ChannelTransportError("QQBot 图片上传请求无效")
            path = f"{self.api_endpoint}/v2/users/{openid}/files"
        elif operation == "send_c2c_image":
            file_info = str(params.get("file_info", "")).strip()
            if not file_info:
                raise ChannelTransportError("QQBot 图片 file_info 缺失")
            body = {
                "msg_type": 7,
                "media": {"file_info": file_info},
                "msg_seq": int(params.get("msg_seq", 0)),
            }
            path = f"{self.api_endpoint}/v2/users/{openid}/messages"
        elif operation == "stream_c2c":
            raw_body = params.get("body")
            if not isinstance(raw_body, Mapping):
                raise ChannelTransportError("QQBot 流式请求体缺失")
            body = dict(raw_body)
            if not body.get("msg_id") or not body.get("event_id"):
                raise ChannelTransportError("QQBot 流式请求缺少入站消息 ID")
            path = f"{self.api_endpoint}/v2/users/{openid}/stream_messages"
        else:
            body = {"msg_type": 0, "content": str(params.get("content", ""))}
            if params.get("msg_id"):
                body["msg_id"] = str(params["msg_id"])
            path = f"{self.api_endpoint}/v2/users/{openid}/messages"
        response = await self._http.post(
            path,
            headers=self._auth_headers(token),
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ChannelTransportError("QQBot 返回了无效响应")
        return payload

    async def _access_token(self) -> str:
        self._ensure_http_open()
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            response = await self._http.post(
                self.token_endpoint,
                json={"appId": self.app_id, "clientSecret": self.client_secret},
            )
            response.raise_for_status()
            payload = response.json()
            token = str(payload.get("access_token", "")) if isinstance(payload, dict) else ""
            if not token:
                raise ChannelTransportError("QQBot 获取 access token 失败")
            try:
                expires_in = max(60, int(payload.get("expires_in", 7200)))
            except (TypeError, ValueError):
                expires_in = 7200
            self._token = token
            self._token_expires_at = time.monotonic() + expires_in - min(60, expires_in // 10)
            return token

    def _ensure_http_open(self) -> None:
        if self._owns_http and self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self._request_timeout)

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"QQBot {token}", "X-Union-Appid": self.app_id}

    async def _gateway_url(self) -> str:
        self._ensure_http_open()
        token = await self._access_token()
        response = await self._http.get(
            f"{self.api_endpoint}/gateway/bot",
            headers=self._auth_headers(token),
        )
        response.raise_for_status()
        payload = response.json()
        url = str(payload.get("url", "")) if isinstance(payload, dict) else ""
        if not url:
            raise ChannelTransportError("QQBot Gateway 地址缺失")
        return url

    async def _run(self) -> None:
        delay = self.reconnect_min
        while not self._stopping.is_set():
            try:
                url = self._resume_gateway_url or await self._gateway_url()
                async with self._connector()(url, open_timeout=15, close_timeout=3, ping_interval=None) as ws:
                    self._ws = ws
                    self._connected = True
                    self._online = None
                    self._observed_at = observed_now()
                    self._connection_reason = "gateway_not_ready"
                    delay = self.reconnect_min
                    await self._read_gateway(ws)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not self._stopping.is_set():
                    logger.warning("QQBot Gateway 断开，将在 %.1f 秒后重连 (%s)", delay, type(error).__name__)
            finally:
                self._ws = None
                self._connected = False
                self._online = False
                self._observed_at = observed_now()
                self._connection_reason = "stopped" if self._stopping.is_set() else "connection_lost"
            if not self._stopping.is_set():
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max)

    async def _read_gateway(self, ws: Any) -> None:
        heartbeat: asyncio.Task[None] | None = None
        try:
            while not self._stopping.is_set():
                packet = json.loads(await ws.recv())
                if not isinstance(packet, dict):
                    continue
                opcode = packet.get("op")
                data = packet.get("d")
                if packet.get("s") is not None:
                    self._sequence = int(packet["s"])
                if opcode == 10:
                    interval = max(1.0, float((data or {}).get("heartbeat_interval", 45000)) / 1000)
                    heartbeat = asyncio.create_task(self._heartbeat(ws, interval))
                    if self._session_id and self._sequence is not None:
                        token = await self._access_token()
                        await ws.send(json.dumps({"op": 6, "d": {
                            "token": f"QQBot {token}",
                            "session_id": self._session_id,
                            "seq": self._sequence,
                        }}))
                    else:
                        token = await self._access_token()
                        await ws.send(json.dumps({"op": 2, "d": {
                            "token": f"QQBot {token}",
                            "intents": 1 << 25,
                            "shard": [0, 1],
                        }}))
                elif opcode == 0:
                    event_type = str(packet.get("t", ""))
                    if event_type == "READY" and isinstance(data, dict):
                        self._session_id = str(data.get("session_id", "")) or None
                        self._resume_gateway_url = str(data.get("resume_gateway_url", "")) or None
                        self._online = True
                        self._observed_at = observed_now()
                        self._connection_reason = "gateway_ready"
                    elif event_type == "RESUMED":
                        self._online = True
                        self._observed_at = observed_now()
                        self._connection_reason = "gateway_resumed"
                    event_task = asyncio.create_task(_invoke_event(self._handler, packet))
                    self._event_tasks.add(event_task)
                    event_task.add_done_callback(self._event_tasks.discard)
                elif opcode == 11:
                    self._heartbeat_ack.set()
                elif opcode == 7:
                    raise ChannelTransportError("QQBot Gateway 要求重新连接")
                elif opcode == 9:
                    self._session_id = None
                    self._resume_gateway_url = None
                    self._sequence = None
                    raise ChannelTransportError("QQBot Gateway 会话失效")
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            self._heartbeat_ack.clear()
            await ws.send(json.dumps({"op": 1, "d": self._sequence}))
            try:
                await asyncio.wait_for(self._heartbeat_ack.wait(), timeout=interval)
            except TimeoutError:
                await ws.close()
                raise ChannelTransportError("QQBot Gateway 心跳确认超时")


class FeishuWebhookTransport:
    """Feishu HTTPS webhook ingress and tenant-token REST sender."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        verification_token: str = "",
        encrypt_key: str = "",
        endpoint: str = "https://open.feishu.cn/open-apis",
        http_client: Any | None = None,
        request_timeout: float = 20,
        long_connection_factory: Callable[[Callable[[Mapping[str, Any]], None]], Any] | None = None,
        long_connection: bool = False,
        long_connection_reconnect_min: float = 1.0,
        long_connection_reconnect_max: float = 30.0,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("飞书 app_id 和 app_secret 必须配置")
        self.app_id = app_id
        self.app_secret = app_secret
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.endpoint = endpoint.rstrip("/")
        self._request_timeout = request_timeout
        self._http = http_client or httpx.AsyncClient(timeout=request_timeout)
        self._owns_http = http_client is None
        self._handler: EventHandler | None = None
        self._host_loop: asyncio.AbstractEventLoop | None = None
        self._long_connection_factory = long_connection_factory
        self._long_connection_enabled = long_connection or long_connection_factory is not None
        self._long_connection_reconnect_min = max(0.0, long_connection_reconnect_min)
        self._long_connection_reconnect_max = max(self._long_connection_reconnect_min, long_connection_reconnect_max)
        self._long_connection: FeishuLongConnectionRunner | None = None
        self._last_connection_snapshot: dict[str, Any] = {
            "connection": "disconnected", "online": None, "observedAt": None, "reason": "not_started",
        }
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def start(self, on_event: EventHandler) -> None:
        self._handler = on_event
        self._host_loop = asyncio.get_running_loop()
        self._last_connection_snapshot = {
            "connection": "unknown", "online": None, "observedAt": None, "reason": "webhook_unobserved",
        }
        if self._long_connection_enabled:
            factory = self._long_connection_factory
            if factory is None:
                factory = feishu_sdk_connection_factory(self.app_id, self.app_secret, self.endpoint.split("/open-apis", 1)[0])
            if self._long_connection is None:
                self._long_connection = FeishuLongConnectionRunner(
                    factory,
                    self._dispatch_long_event,
                    reconnect_min=self._long_connection_reconnect_min,
                    reconnect_max=self._long_connection_reconnect_max,
                )
            self._long_connection.start()

    async def stop(self) -> None:
        runner, self._long_connection = self._long_connection, None
        if runner is not None:
            await runner.stop()
            self._last_connection_snapshot = runner.connection_snapshot()
        else:
            self._last_connection_snapshot = {
                "connection": "disconnected", "online": None, "observedAt": observed_now(), "reason": "stopped",
            }
        self._handler = None
        self._host_loop = None
        if self._owns_http:
            await self._http.aclose()

    @property
    def connected(self) -> bool:
        return self._long_connection is not None and self._long_connection.connected

    def connection_snapshot(self) -> dict[str, Any]:
        if self._long_connection is not None:
            return self._long_connection.connection_snapshot()
        return dict(self._last_connection_snapshot)

    def _dispatch_long_event(self, payload: Mapping[str, Any]) -> None:
        handler, loop = self._handler, self._host_loop
        if handler is None or loop is None or loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(_invoke_event(handler, payload), loop)
        future.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)

    async def handle_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> dict[str, Any]:
        """Validate and dispatch one Feishu event request.

        This method is intended to be called by the host's FastAPI route using
        the untouched request body so Feishu's request signature can be checked.
        """

        body_text = raw_body.decode("utf-8")
        payload = json.loads(body_text)
        if not isinstance(payload, dict):
            raise ChannelTransportError("飞书 webhook 内容格式错误")
        normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
        if self.encrypt_key:
            self._verify_signature(body_text, normalized_headers)
        if isinstance(payload.get("encrypt"), str):
            payload = self._decrypt_payload(payload["encrypt"])
        if not isinstance(payload, dict):
            raise ChannelTransportError("飞书 webhook 解密内容格式错误")
        if payload.get("type") == "url_verification":
            self._check_verification_token(payload.get("token"))
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or not challenge:
                raise ChannelTransportError("飞书 webhook challenge 缺失")
            return {"challenge": challenge}
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        self._check_verification_token(header.get("token", payload.get("token")))
        await _invoke_event(self._handler, payload)
        return {"code": 0}

    def _verify_signature(self, body: str, headers: Mapping[str, str]) -> None:
        timestamp = headers.get("x-lark-request-timestamp", "")
        nonce = headers.get("x-lark-request-nonce", "")
        signature = headers.get("x-lark-signature", "")
        if not timestamp or not nonce or not signature:
            raise ChannelTransportError("飞书 webhook 签名头缺失")
        expected = hashlib.sha256(f"{timestamp}{nonce}{self.encrypt_key}{body}".encode("utf-8")).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ChannelTransportError("飞书 webhook 签名校验失败")

    def _decrypt_payload(self, encrypted: str) -> dict[str, Any]:
        if not self.encrypt_key:
            raise ChannelTransportError("飞书 webhook 未配置 Encrypt Key")
        try:
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            ciphertext = base64.b64decode(encrypted, validate=True)
            key = hashlib.sha256(self.encrypt_key.encode("utf-8")).digest()
            if len(ciphertext) < 32 or len(ciphertext) % 16:
                raise ValueError("飞书加密内容长度无效")
            decryptor = Cipher(algorithms.AES(key), modes.CBC(ciphertext[:16])).decryptor()
            padded = decryptor.update(ciphertext[16:]) + decryptor.finalize()
            unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
            plaintext = unpadder.update(padded) + unpadder.finalize()
            payload = json.loads(plaintext.decode("utf-8"))
        except Exception as error:
            raise ChannelTransportError("飞书 webhook 解密失败") from error
        if not isinstance(payload, dict):
            raise ChannelTransportError("飞书 webhook 解密内容格式错误")
        return payload

    def _check_verification_token(self, token: Any) -> None:
        if self.verification_token and not hmac.compare_digest(str(token or ""), self.verification_token):
            raise ChannelTransportError("飞书 webhook token 校验失败")

    async def profile(self, open_id: str | None = None) -> Mapping[str, Any]:
        self._ensure_http_open()
        token = await self._tenant_access_token()
        path = f"/contact/v3/users/{quote(open_id, safe='')}" if open_id else "/bot/v3/info"
        response = await self._http.get(f"{self.endpoint}{path}", headers={"Authorization": f"Bearer {token}"}, params={"user_id_type": "open_id"} if open_id else {})
        payload = self._checked_json_response(response)
        if not open_id:
            bot = payload.get("bot")
            return bot if isinstance(bot, dict) else {}
        data = payload.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        return user if isinstance(user, dict) else {}

    async def __call__(self, operation: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self._ensure_http_open()
        token = await self._tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        if operation == "im.v1.message.resource.get":
            message_id = quote(str(params.get("message_id", "")), safe="")
            file_key = quote(str(params.get("file_key", "")), safe="")
            max_bytes = max(1, int(params.get("max_bytes", 10 * 1024 * 1024)))
            resource_type = str(params.get("type", "file"))
            if not message_id or not file_key or resource_type not in {"image", "file"}:
                raise ChannelTransportError("飞书媒体资源参数无效")
            url = f"{self.endpoint}/im/v1/messages/{message_id}/resources/{file_key}"
            async with self._http.stream("GET", url, headers=headers, params={"type": resource_type}) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > max_bytes:
                    raise ChannelTransportError("飞书附件超过大小限制")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > max_bytes:
                        raise ChannelTransportError("飞书附件超过大小限制")
                    content.extend(chunk)
            return bytes(content)  # type: ignore[return-value]
        if operation == "im.v1.media.create":
            kind = str(params.get("kind", "file"))
            data = params.get("data")
            file_name = str(params.get("file_name") or "attachment")
            if kind not in {"image", "file"} or not isinstance(data, bytes):
                raise ChannelTransportError("飞书上传媒体参数无效")
            if len(data) > 10 * 1024 * 1024:
                raise ChannelTransportError("飞书附件超过大小限制")
            if kind == "image":
                response = await self._http.post(
                    f"{self.endpoint}/im/v1/images",
                    headers=headers,
                    data={"image_type": "message"},
                    files={"image": (file_name, data)},
                )
                result = self._checked_json_response(response)
                key = result.get("data", {}).get("image_key")
                if not key:
                    raise ChannelTransportError("飞书图片上传未返回 image_key")
                return {"image_key": str(key)}
            response = await self._http.post(
                f"{self.endpoint}/im/v1/files",
                headers=headers,
                data={"file_type": "stream", "file_name": file_name},
                files={"file": (file_name, data)},
            )
            result = self._checked_json_response(response)
            key = result.get("data", {}).get("file_key")
            if not key:
                raise ChannelTransportError("飞书文件上传未返回 file_key")
            return {"file_key": str(key)}
        if operation.startswith("cardkit."):
            return await self._cardkit_operation(operation, params, headers)
        if operation != "im.v1.message.create":
            raise ChannelTransportError("飞书不支持该出站操作")
        body = dict(params)
        receive_id_type = str(body.pop("receive_id_type", "chat_id"))
        reply_message_id = str(body.pop("reply_message_id", ""))
        if reply_message_id:
            url = f"{self.endpoint}/im/v1/messages/{quote(reply_message_id, safe='')}/reply"
            body.pop("receive_id", None)
            response = await self._http.post(url, headers=headers, json=body)
        else:
            receive_id = quote(str(body.pop("receive_id", "")), safe="")
            response = await self._http.post(
                f"{self.endpoint}/im/v1/messages?receive_id_type={quote(receive_id_type, safe='')}",
                headers=headers,
                json={**body, "receive_id": receive_id},
            )
        response.raise_for_status()
        return self._checked_json_response(response)

    async def _cardkit_operation(self, operation: str, params: Mapping[str, Any], headers: Mapping[str, str]) -> Mapping[str, Any]:
        card_id = quote(str(params.get("card_id", "")), safe="")
        if operation == "cardkit.create":
            response = await self._http.post(
                f"{self.endpoint}/cardkit/v1/cards",
                headers=headers,
                json={"type": "card_json", "data": str(params.get("card_json", "{}"))},
            )
            result = self._checked_json_response(response)
            created_id = result.get("data", {}).get("card_id")
            if not created_id:
                raise ChannelTransportError("飞书 CardKit 未返回 card_id")
            return {"card_id": str(created_id)}
        if not card_id:
            raise ChannelTransportError("飞书 CardKit card_id 缺失")
        if operation == "cardkit.update":
            element_id = quote(str(params.get("element_id", "reply_md")), safe="")
            response = await self._http.put(
                f"{self.endpoint}/cardkit/v1/cards/{card_id}/elements/{element_id}/content",
                headers=headers,
                json={"content": str(params.get("content", "")), "sequence": int(params.get("sequence", 1)), "uuid": str(params.get("uuid") or uuid.uuid4())},
            )
            return self._checked_json_response(response)
        if operation == "cardkit.finalize":
            element_id = quote(str(params.get("element_id", "reply_md")), safe="")
            content = str(params.get("content", ""))
            stream_response = await self._http.put(
                f"{self.endpoint}/cardkit/v1/cards/{card_id}/elements/{element_id}/content",
                headers=headers,
                json={"content": content, "sequence": int(params.get("sequence", 1)), "uuid": str(params.get("uuid") or uuid.uuid4())},
            )
            try:
                self._checked_json_response(stream_response)
            except ChannelTransportError:
                # Feishu automatically closes streaming mode after ten minutes;
                # a whole-card update can still preserve the final response.
                fallback = await self._http.put(
                    f"{self.endpoint}/cardkit/v1/cards/{card_id}",
                    headers=headers,
                    json={
                        "card": {"type": "card_json", "data": json.dumps({
                            "schema": "2.0",
                            "config": {"update_multi": True, "summary": {"content": content[:60]}},
                            "body": {"elements": [{"tag": "markdown", "content": content}]},
                        }, ensure_ascii=False)},
                        "sequence": int(params.get("settings_sequence", int(params.get("sequence", 1)) + 1)),
                        "uuid": str(params.get("settings_uuid") or uuid.uuid4()),
                    },
                )
                return self._checked_json_response(fallback)
            settings_response = await self._http.patch(
                f"{self.endpoint}/cardkit/v1/cards/{card_id}/settings",
                headers=headers,
                json={
                    "settings": str(params.get("settings", "{}")),
                    "sequence": int(params.get("settings_sequence", int(params.get("sequence", 1)) + 1)),
                    "uuid": str(params.get("settings_uuid") or uuid.uuid4()),
                },
            )
            # The text is already final. Shiori treats settings failure as
            # non-fatal because Feishu ends streaming mode automatically.
            try:
                self._checked_json_response(settings_response)
            except ChannelTransportError:
                return {"card_id": card_id}
            return {"card_id": card_id}
        if operation == "cardkit.update_full":
            content = str(params.get("content", ""))
            response = await self._http.put(
                f"{self.endpoint}/cardkit/v1/cards/{card_id}",
                headers=headers,
                json={
                    "card": {"type": "card_json", "data": json.dumps({
                        "schema": "2.0",
                        "config": {"update_multi": True, "summary": {"content": content[:60]}},
                        "body": {"elements": [{"tag": "markdown", "content": content}]},
                    }, ensure_ascii=False)},
                    "sequence": int(params.get("sequence", 1)),
                    "uuid": str(params.get("uuid") or uuid.uuid4()),
                },
            )
            return self._checked_json_response(response)
        raise ChannelTransportError("飞书不支持该 CardKit 操作")

    @staticmethod
    def _checked_json_response(response: Any) -> dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or int(payload.get("code", 0) or 0) != 0:
            raise PlatformRejectedError("飞书 API 请求失败")
        return payload

    async def _tenant_access_token(self) -> str:
        self._ensure_http_open()
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            response = await self._http.post(
                f"{self.endpoint}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            response.raise_for_status()
            payload = response.json()
            token = str(payload.get("tenant_access_token", "")) if isinstance(payload, dict) else ""
            if not token or (isinstance(payload, dict) and int(payload.get("code", 0) or 0) != 0):
                raise PlatformRejectedError("飞书获取 tenant_access_token 失败")
            try:
                expires_in = max(60, int(payload.get("expire", 7200)))
            except (TypeError, ValueError):
                expires_in = 7200
            self._token = token
            self._token_expires_at = time.monotonic() + expires_in - min(60, expires_in // 10)
            return token

    def _ensure_http_open(self) -> None:
        if self._owns_http and self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self._request_timeout)
