"""Owned Feishu SDK execution; no platform work runs on the host event loop."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import aclosing
import importlib
from importlib.metadata import version
import json
import logging
import math
import threading
from types import FunctionType
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx

from .base import ChannelAdapterError
from .directory import observed_now

HANDSHAKE_TIMEOUT = 20.0
MAX_HANDSHAKE_BYTES = 64 * 1024
STOP_TIMEOUT = 10.0
DISCONNECT_TIMEOUT = 5.0
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 256
MAX_WEBSOCKET_URL = 8 * 1024


class FeishuConnectionError(ChannelAdapterError):
    """A bounded, safe diagnostic without platform response text or URLs."""


async def wait_owned(task: asyncio.Task[Any]) -> Any:
    """A caller's cancellation never abandons an owned cleanup operation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class FeishuLongConnectionRunner:
    def __init__(self, factory: Callable[[Callable[[Mapping[str, Any]], None]], Any], on_event: Callable[[Mapping[str, Any]], None], *, reconnect_min: float = 1.0, reconnect_max: float = 30.0) -> None:
        self._factory, self._on_event = factory, on_event
        self._reconnect_min, self._reconnect_max = reconnect_min, reconnect_max
        self.connected = False
        self._online: bool | None = None
        self._observed_at: str | None = None
        self._connection_reason = "not_started"
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = threading.Event()
        self._dispatch_lock = threading.Lock()
        self._cancel_sent = False
        self._stop_waiter: asyncio.Task[None] | None = None
        self._connection: Any = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._cleanup_retry: asyncio.Event | None = None
        self._stop_generation = 0

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def connection_snapshot(self) -> dict[str, Any]:
        connection = "connected" if self.connected else "connecting" if self.alive else "disconnected"
        return {"connection": connection, "online": self._online,
                "observedAt": self._observed_at, "reason": self._connection_reason}

    def start(self) -> None:
        if self.alive:
            if self._stop.is_set():
                raise FeishuConnectionError("feishu_stopping")
            return
        self._stop.clear()
        self._cancel_sent = False
        self._stop_generation = 0
        self._stop_waiter = None
        self._online = None
        self._observed_at, self._connection_reason = observed_now(), "connecting"
        self._thread = threading.Thread(target=self._run, name="feishu-long-connection", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        with self._dispatch_lock:
            self._stop.set()
        self._stop_generation += 1
        self.connected, self._online = False, None
        self._observed_at, self._connection_reason = observed_now(), "stopping"
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:
                # The thread may have closed its loop after the snapshot.
                pass
        if self._stop_waiter is None or (self._stop_waiter.done() and self._stop_waiter.exception() is not None):
            self._stop_waiter = asyncio.create_task(self._wait_for_exit())
        await wait_owned(self._stop_waiter)
        self._online = False
        self._observed_at, self._connection_reason = observed_now(), "stopped"

    def _request_stop(self) -> None:
        if self._cleanup_retry is not None:
            self._cleanup_retry.set()
        if not self._cancel_sent and self._task is not None and not self._task.done():
            self._cancel_sent = True
            self._task.cancel()

    async def _wait_for_exit(self) -> None:
        deadline = asyncio.get_running_loop().time() + STOP_TIMEOUT
        while self.alive:
            if asyncio.get_running_loop().time() >= deadline:
                raise FeishuConnectionError("feishu_stop_timeout")
            await asyncio.sleep(.01)
        if self._thread is not None:
            self._thread.join()
        self._online = False
        self._observed_at, self._connection_reason = observed_now(), "stopped"

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._cleanup_retry = asyncio.Event()
        try:
            self._task = loop.create_task(self._supervise())
            if self._stop.is_set():
                self._task.cancel()
            try:
                loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                pass
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._task, self._loop, self._cleanup_retry = None, None, None

    async def _clean_connection(self, connection: Any) -> None:
        # Only this task may release or retry this Connection. Supervisor
        # cancellation is shielded by wait_owned; failed cleanup parks here,
        # retaining the original object until another explicit stop retries it.
        while True:
            generation = self._stop_generation
            try:
                attempt = asyncio.create_task(connection.disconnect())
                done, _ = await asyncio.wait({attempt}, timeout=DISCONNECT_TIMEOUT)
                if not done:
                    # A deadline changes the observation, never ownership.
                    # Keep joining this same attempt; host stop can report its
                    # own deadline and a later explicit retry still owns it.
                    self._connection_reason = "cleanup_pending"
                await wait_owned(attempt)
                self._connection = None
                return
            except (Exception, asyncio.CancelledError):
                self._connection_reason = "cleanup_failed"
                assert self._cleanup_retry is not None
                self._cleanup_retry.clear()
                if self._stop_generation <= generation:
                    await self._cleanup_retry.wait()

    async def _supervise(self) -> None:
        delay = self._reconnect_min
        while not self._stop.is_set():
            connection = None
            try:
                connection = self._factory(self._dispatch)
                self._connection = connection
                await connection.connect()
                if self._stop.is_set():
                    raise asyncio.CancelledError
                self.connected, self._online = True, True
                self._observed_at, self._connection_reason = observed_now(), "long_connection_connected"
                delay = self._reconnect_min
                keepalive = asyncio.create_task(connection.keepalive())
                try:
                    while not self._stop.is_set() and connection.is_connected():
                        await asyncio.sleep(.1)
                finally:
                    keepalive.cancel()
                    await asyncio.gather(keepalive, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected = False
            finally:
                self.connected = False
                self._online = None if self._stop.is_set() else False
                self._observed_at = observed_now()
                self._connection_reason = "stopping" if self._stop.is_set() else "connection_lost"
                if connection is not None:
                    self._cleanup_task = asyncio.create_task(self._clean_connection(connection))
                    await wait_owned(self._cleanup_task)
            if not self._stop.is_set():
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._reconnect_max)

    def _dispatch(self, payload: Mapping[str, Any]) -> None:
        with self._dispatch_lock:
            if not self._stop.is_set():
                self._on_event(payload)


def _validate_json(value: Any) -> None:
    pending, count = [(value, 1)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise FeishuConnectionError("feishu_handshake_structure")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise FeishuConnectionError("feishu_handshake_structure")


def _websocket_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_WEBSOCKET_URL:
        raise FeishuConnectionError("feishu_handshake_url")
    try:
        url = urlsplit(value)
        query = parse_qs(url.query, max_num_fields=64)
        if url.scheme not in {"ws", "wss"} or not url.hostname or url.fragment:
            raise ValueError
        _ = url.port
        if len(query.get("device_id", [])) != 1 or len(query.get("service_id", [])) != 1:
            raise ValueError
        if not 0 < int(query["service_id"][0]) < 2 ** 31:
            raise ValueError
    except (ValueError, UnicodeError):
        raise FeishuConnectionError("feishu_handshake_url") from None
    return value


def _configuration(client: Any, config: Any) -> dict[str, int | float]:
    fields = {
        "ReconnectCount": ("_reconnect_count", -1, 10000),
        "ReconnectNonce": ("_reconnect_nonce", 0, 86400),
        "ReconnectInterval": ("_reconnect_interval", 0, 86400),
        "PingInterval": ("_ping_interval", .01, 86400),
    }
    values = {}
    for name, (attribute, minimum, maximum) in fields.items():
        value = getattr(config, name, None)
        if value is None:
            value = getattr(client, attribute)
        if type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum:
            raise FeishuConnectionError("feishu_configuration")
        if name == "ReconnectCount" and type(value) is not int:
            raise FeishuConnectionError("feishu_configuration")
        values[name] = value
    return values


async def _endpoint(client: Any) -> str:
    from lark_oapi.ws.exception import ClientException, ServerException  # type: ignore[import-untyped]
    from lark_oapi.ws.model import ClientConfig  # type: ignore[import-untyped]

    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            async with httpx.AsyncClient(timeout=HANDSHAKE_TIMEOUT, trust_env=False, follow_redirects=False) as http:
                async with http.stream("POST", client._domain + "/callback/ws/endpoint", headers={"locale": "zh", "User-Agent": client._user_agent, "Accept-Encoding": "identity"}, json={"AppID": client._app_id, "AppSecret": client._app_secret}) as response:
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise FeishuConnectionError("feishu_handshake_encoding")
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isdecimal() or int(length) > MAX_HANDSHAKE_BYTES):
                        raise FeishuConnectionError("feishu_handshake_size")
                    raw = bytearray()
                    # HTTPX declares AsyncIterator, but aiter_raw is an async
                    # generator and therefore owns the aclose used here.
                    raw_stream = cast(AsyncGenerator[bytes, None], response.aiter_raw(chunk_size=4096))
                    async with aclosing(raw_stream) as stream:
                        async for chunk in stream:
                            if len(raw) + len(chunk) > MAX_HANDSHAKE_BYTES:
                                raise FeishuConnectionError("feishu_handshake_size")
                            raw.extend(chunk)
                    if response.status_code != 200:
                        raise ServerException(response.status_code, "feishu_handshake_http")
        body = json.loads(raw.decode("utf-8"))
        _validate_json(body)
        if not isinstance(body, dict) or type(body.get("code")) is not int:
            raise FeishuConnectionError("feishu_handshake_response")
        code = body["code"]
        if code in {1, 1000040343}:
            raise ServerException(code, "feishu_handshake_rejected")
        if code != 0:
            raise ClientException(code, "feishu_handshake_rejected")
        data = body.get("data")
        if not isinstance(data, dict):
            raise FeishuConnectionError("feishu_handshake_response")
        url = _websocket_url(data.get("URL"))
        config = data.get("ClientConfig")
        if config is not None:
            if not isinstance(config, dict):
                raise FeishuConnectionError("feishu_configuration")
            client._configure(ClientConfig(config))
        return url
    except (FeishuConnectionError, ClientException, ServerException):
        raise
    except Exception:
        raise FeishuConnectionError("feishu_handshake_failed") from None


def feishu_sdk_connection_factory(app_id: str, app_secret: str, endpoint: str) -> Callable[[Callable[[Mapping[str, Any]], None]], Any]:
    def factory(on_event: Callable[[Mapping[str, Any]], None]) -> Any:
        if version("lark-oapi") != "1.7.4":
            raise FeishuConnectionError("feishu_sdk_version")
        from lark_oapi import EventDispatcherHandler, LogLevel  # type: ignore[import-untyped]
        from lark_oapi.ws.client import Client as SdkClient  # type: ignore[import-untyped]
        module: Any = importlib.import_module("lark_oapi.ws.client")

        class LoopProxy:
            def create_task(self, coroutine: Any) -> asyncio.Task[Any]:
                task = asyncio.get_running_loop().create_task(coroutine)
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                return task

        module.loop = LoopProxy()
        # Reuse the installed function's unchanged code and closure. Only this
        # invocation's globals receive private protocol logging kwargs; neither
        # websockets.connect nor any registered/global logger is patched.
        original = module.Client._connect
        if not callable(getattr(module, "_ws_connect_kwargs", None)) or "_ws_connect_kwargs" not in original.__code__.co_names:
            raise FeishuConnectionError("feishu_sdk_contract")
        protocol_logger = logging.Logger("meido.feishu.protocol.private")
        protocol_logger.disabled, protocol_logger.propagate = True, False
        protocol_logger.addHandler(logging.NullHandler())
        private_globals = dict(original.__globals__)
        private_globals["_ws_connect_kwargs"] = lambda: {**module._ws_connect_kwargs(), "logger": protocol_logger}
        sdk_connect = FunctionType(original.__code__, private_globals, original.__name__, original.__defaults__, original.__closure__)
        sdk_connect.__kwdefaults__ = original.__kwdefaults__

        class ManagedClient(SdkClient):
            _prepared_url: str | None = None

            async def _connect(self) -> None:
                if self._conn is not None:
                    return
                self._prepared_url = await _endpoint(self)
                try:
                    await asyncio.sleep(0)
                    await sdk_connect(self)
                except Exception as error:
                    from lark_oapi.ws.exception import ClientException, ServerException
                    if isinstance(error, (ClientException, ServerException)):
                        raise type(error)(error.code, "feishu_websocket_rejected") from None
                    raise FeishuConnectionError("feishu_websocket_failed") from None
                finally:
                    self._prepared_url = None

            def _get_conn_url(self) -> str:
                if self._prepared_url is None:
                    raise FeishuConnectionError("feishu_handshake_missing")
                value, self._prepared_url = self._prepared_url, None
                return value

            def _configure(self, config: Any) -> None:
                from lark_oapi.ws.model import ClientConfig
                # PONG and discovery both update config through this boundary.
                # Validate the entire patch before mutating any SDK field.
                super()._configure(ClientConfig(_configuration(self, config)))

            def _fmt_log(self, fmt: str, *args: Any) -> str:
                kinds = sorted({type(value).__name__ for value in args if isinstance(value, BaseException)})
                return "feishu_sdk_error:" + ",".join(kinds) if kinds else "feishu_sdk_event"

        builder = EventDispatcherHandler.builder("", "")
        builder.register_p2_customized_event("im.message.receive_v1", lambda event: on_event(_event_payload(event)))
        client = ManagedClient(app_id, app_secret, log_level=LogLevel.WARNING, event_handler=builder.build(), domain=endpoint, auto_reconnect=False)

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


def _event_payload(event: Any) -> dict[str, Any]:
    header = getattr(event, "header", None)
    body = getattr(event, "event", None)
    if isinstance(body, Mapping):
        data = dict(body)
    else:
        try:
            from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]
            value = MessageToDict(body, preserving_proto_field_name=True)
            data = value if isinstance(value, dict) else {}
        except Exception:
            data = {}
    return {"header": {"event_id": str(getattr(header, "event_id", "") or ""), "event_type": str(getattr(header, "event_type", "") or "")}, "event": data}
