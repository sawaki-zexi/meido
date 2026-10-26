from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import httpx
from collections.abc import Mapping
from typing import Any

from backend.app.channels import (
    FeishuWebhookTransport,
    FeishuLongConnectionRunner,
    NapCatAdapter,
    NapCatConfig,
    NapCatWebSocketTransport,
    QQBotGatewayTransport,
    QQBotAdapter,
    QQBotConfig,
)


class FakeSocket:
    def __init__(self, initial: list[Mapping[str, Any]] | None = None) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        for packet in initial or []:
            self.incoming.put_nowait(json.dumps(packet))

    async def recv(self) -> str:
        value = await self.incoming.get()
        if value is None:
            raise ConnectionError("closed")
        return value

    async def send(self, value: str) -> None:
        packet = json.loads(value)
        self.sent.append(packet)
        if "echo" in packet:
            await self.incoming.put(json.dumps({
                "status": "ok", "retcode": 0,
                "data": {"message_id": "out-1"}, "echo": packet["echo"],
            }))

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


class FakeSocketContext:
    def __init__(self, socket: FakeSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> FakeSocket:
        return self.socket

    async def __aexit__(self, *_: object) -> None:
        await self.socket.close()


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.001)


def test_feishu_long_connection_runs_off_host_loop_dispatches_and_stops() -> None:
    async def run() -> None:
        class Connection:
            def __init__(self, callback):
                self.callback = callback
                self.loop = asyncio.get_running_loop()
                self.thread = threading.current_thread().name
                self.connected = False
                self.disconnects = 0

            async def connect(self):
                self.connected = True
                connections.append(self)

            def is_connected(self):
                return self.connected

            async def keepalive(self):
                await asyncio.Future()

            async def disconnect(self):
                self.disconnects += 1
                self.connected = False

        host_loop = asyncio.get_running_loop()
        connections = []
        received = asyncio.Queue()
        runner = FeishuLongConnectionRunner(lambda callback: Connection(callback), lambda payload: asyncio.run_coroutine_threadsafe(received.put(payload), host_loop), reconnect_min=0.001, reconnect_max=0.002)
        runner.start()
        await wait_until(lambda: bool(connections))
        first = connections[0]
        assert first.loop is not host_loop
        assert first.thread == "feishu-long-connection"
        first.callback({"event": "first"})
        assert await asyncio.wait_for(received.get(), 1) == {"event": "first"}

        first.connected = False
        await wait_until(lambda: len(connections) > 1, timeout=2)
        await runner.stop()
        await runner.stop()
        assert not runner.alive
        assert all(connection.disconnects >= 1 for connection in connections)

    asyncio.run(run())


def test_feishu_transport_dispatches_on_host_loop_and_restarts_idempotently() -> None:
    async def run() -> None:
        connections = []

        class Connection:
            def __init__(self, callback):
                self.callback = callback
                self.connected = False

            async def connect(self):
                self.connected = True
                connections.append(self)

            def is_connected(self):
                return self.connected

            async def keepalive(self):
                await asyncio.Future()

            async def disconnect(self):
                self.connected = False

        client = FakeHttpClient()
        transport = FeishuWebhookTransport(
            "app", "secret", http_client=client,
            long_connection_factory=lambda callback: Connection(callback),
            long_connection_reconnect_min=0.001,
            long_connection_reconnect_max=0.002,
        )
        host_loop = asyncio.get_running_loop()
        received = asyncio.Event()
        observed = []

        async def on_event(payload):
            observed.append((payload, asyncio.get_running_loop() is host_loop))
            received.set()

        await transport.start(on_event)
        await wait_until(lambda: transport.connected)
        connections[0].callback({"event": "one"})
        await asyncio.wait_for(received.wait(), 1)
        assert observed == [({"event": "one"}, True)]

        connections[0].connected = False
        await wait_until(lambda: len(connections) > 1)
        await transport.stop()
        await transport.stop()
        assert not transport.connected

        received.clear()
        await transport.start(on_event)
        await wait_until(lambda: transport.connected)
        connections[-1].callback({"event": "two"})
        await asyncio.wait_for(received.wait(), 1)
        assert observed[-1] == ({"event": "two"}, True)
        await transport.stop()

    asyncio.run(run())


def test_napcat_transport_connects_sends_api_calls_and_forwards_raw_events() -> None:
    async def run() -> None:
        socket = FakeSocket()
        connector_args: list[tuple[str, dict[str, Any]]] = []

        def connect(url: str, **kwargs: Any) -> FakeSocketContext:
            connector_args.append((url, kwargs))
            return FakeSocketContext(socket)

        transport = NapCatWebSocketTransport(
            "ws://napcat.local:6700", access_token="secret", connector=connect,
        )
        adapter = NapCatAdapter(NapCatConfig(account_id="role-1"), transport=transport)
        received: list[Mapping[str, Any]] = []

        async def on_event(payload: Mapping[str, Any]) -> None:
            received.append(payload)

        adapter.set_event_handler(on_event)
        await adapter.start()
        await wait_until(lambda: transport.connected)

        result = await transport("send_private_msg", {"user_id": "user-1", "message": "hello"})
        assert result == {"message_id": "out-1"}
        assert socket.sent[0]["action"] == "send_private_msg"
        assert connector_args[0][0] == "ws://napcat.local:6700"
        assert connector_args[0][1]["additional_headers"] == {"Authorization": "Bearer secret"}

        raw = {"post_type": "message", "message_type": "private", "message_id": 2}
        await socket.incoming.put(json.dumps(raw))
        await wait_until(lambda: bool(received))
        assert received == [raw]
        await adapter.stop()
        assert socket.closed

    asyncio.run(run())


def test_napcat_event_handler_can_issue_api_call_without_blocking_reader() -> None:
    """Inbound dispatch must not run inline on the OneBot reader task.

    Event handlers commonly refresh the target directory, which sends a
    request over the same socket and waits for its echo.  Running that handler
    inline would deadlock before the echo can be consumed.
    """

    async def run() -> None:
        socket = FakeSocket()

        def connect(url: str, **kwargs: Any) -> FakeSocketContext:
            return FakeSocketContext(socket)

        transport = NapCatWebSocketTransport("ws://napcat.local:6700", connector=connect)
        completed = asyncio.Event()

        async def on_event(payload: Mapping[str, Any]) -> None:
            assert payload["post_type"] == "message"
            response = await transport("get_group_list", {})
            assert response == {"message_id": "out-1"}
            completed.set()

        await transport.start(on_event)
        await wait_until(lambda: transport.connected)
        await socket.incoming.put(json.dumps({
            "post_type": "message", "message_type": "private", "message_id": 9,
        }))
        await asyncio.wait_for(completed.wait(), timeout=1)
        await transport.stop()

    asyncio.run(run())


class FakeResponse:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def post(self, url: str, *, headers=None, json=None) -> FakeResponse:
        self.calls.append(("POST", url, {"headers": headers or {}, "json": json or {}}))
        if url.endswith("getAppAccessToken"):
            return FakeResponse({"access_token": "access", "expires_in": 3600})
        if url.endswith("tenant_access_token/internal"):
            return FakeResponse({"tenant_access_token": "access", "expire": 3600, "code": 0})
        if url.endswith("/v2/users/openid-1/files"):
            return FakeResponse({"file_info": "file-info-1"})
        return FakeResponse({"id": "sent-1"})

    async def get(self, url: str, *, headers=None) -> FakeResponse:
        self.calls.append(("GET", url, {"headers": headers or {}}))
        if url.endswith("/gateway/bot"):
            return FakeResponse({"url": "wss://gateway.qq.local"})
        raise AssertionError(url)

    async def delete(self, url: str, *, headers=None) -> FakeResponse:
        self.calls.append(("DELETE", url, {"headers": headers or {}}))
        return FakeResponse({"id": "deleted"})


def test_qqbot_gateway_identifies_receives_dispatch_and_sends_c2c() -> None:
    async def run() -> None:
        socket = FakeSocket([{"op": 10, "d": {"heartbeat_interval": 60000}}])
        client = FakeHttpClient()
        opened: list[str] = []

        def connect(url: str, **_: Any) -> FakeSocketContext:
            opened.append(url)
            return FakeSocketContext(socket)

        transport = QQBotGatewayTransport(
            "app-1", "app-secret", connector=connect, http_client=client,
            reconnect_min=0.01, reconnect_max=0.02,
        )
        received: list[Mapping[str, Any]] = []

        async def on_event(payload: Mapping[str, Any]) -> None:
            received.append(payload)

        await transport.start(on_event)
        await wait_until(lambda: any(item.get("op") == 2 for item in socket.sent))
        identify = next(item for item in socket.sent if item.get("op") == 2)
        assert identify["d"]["token"] == "QQBot access"
        assert identify["d"]["intents"] == 1 << 25
        assert opened == ["wss://gateway.qq.local"]

        event = {"op": 0, "s": 4, "t": "C2C_MESSAGE_CREATE", "d": {"id": "m-1"}}
        await socket.incoming.put(json.dumps(event))
        await wait_until(lambda: bool(received))
        assert received == [event]

        result = await transport("send_c2c", {"openid": "openid-1", "content": "reply", "msg_id": "m-1"})
        assert result == {"id": "sent-1"}
        send_call = next(call for call in client.calls if call[1].endswith("/v2/users/openid-1/messages"))
        assert send_call[2]["json"] == {"msg_type": 0, "content": "reply", "msg_id": "m-1"}
        assert send_call[2]["headers"]["Authorization"] == "QQBot access"

        stream = await transport("stream_c2c", {
            "openid": "openid-1",
            "body": {
                "input_mode": "replace", "input_state": 1, "content_type": "markdown",
                "content_raw": "preview", "event_id": "m-1", "msg_id": "m-1",
                "msg_seq": 42, "index": 0,
            },
        })
        assert stream == {"id": "sent-1"}
        stream_call = next(call for call in client.calls if call[1].endswith("/v2/users/openid-1/stream_messages"))
        assert stream_call[2]["json"]["input_state"] == 1
        assert stream_call[2]["json"]["content_raw"] == "preview"
        assert stream_call[2]["headers"]["Authorization"] == "QQBot access"
        upload = await transport("upload_c2c_file", {
            "openid": "openid-1",
            "body": {"file_type": 1, "file_data": "aW1hZ2U=", "srv_send_msg": False},
        })
        assert upload == {"file_info": "file-info-1"}
        upload_call = next(call for call in client.calls if call[1].endswith("/v2/users/openid-1/files"))
        assert upload_call[2]["json"] == {"file_type": 1, "file_data": "aW1hZ2U=", "srv_send_msg": False}
        image = await transport("send_c2c_image", {
            "openid": "openid-1", "file_info": "file-info-1", "msg_seq": 12,
        })
        assert image == {"id": "sent-1"}
        image_call = [call for call in client.calls if call[1].endswith("/v2/users/openid-1/messages")][-1]
        assert image_call[2]["json"] == {
            "msg_type": 7, "content": " ", "media": {"file_info": "file-info-1"}, "msg_seq": 12,
        }
        deleted = await transport("delete_c2c", {"openid": "openid-1", "message_id": "stream-1"})
        assert deleted == {"id": "deleted"}
        assert any(call[0] == "DELETE" and call[1].endswith("/v2/users/openid-1/messages/stream-1") for call in client.calls)
        await transport.stop()

    asyncio.run(run())


def test_qqbot_wire_contract_uses_access_token_and_integer_intents() -> None:
    async def run() -> None:
        requests: list[httpx.Request] = []
        wire_packets: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
            if request.url.path.endswith("/gateway/bot"):
                return httpx.Response(200, json={"url": "wss://gateway.qq.test"})
            return httpx.Response(200, json={"id": "sent"})

        class WireSocket(FakeSocket):
            async def send(self, value: str) -> None:
                wire_packets.append(value)
                await super().send(value)

        socket = WireSocket([{"op": 10, "d": {"heartbeat_interval": 60000}}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            transport = QQBotGatewayTransport(
                "app-1", "secret", http_client=client,
                connector=lambda *_, **__: FakeSocketContext(socket),
            )
            await transport.start(lambda _: asyncio.sleep(0))
            await wait_until(lambda: bool(wire_packets))
            identify = json.loads(wire_packets[0])
            assert identify == {"op": 2, "d": {"token": "QQBot access", "intents": 33554432, "shard": [0, 1]}}
            assert QQBotAdapter(QQBotConfig(account_id="account", app_id="app-1")).gateway_identify(access_token="access") == identify
            await transport("send_c2c", {"openid": "openid", "content": "reply", "msg_id": "trigger"})
            for request in requests[1:]:
                assert request.headers["Authorization"] == "QQBot access"
                assert request.headers["X-Union-Appid"] == "app-1"
            assert json.loads(requests[-1].content) == {"msg_type": 0, "content": "reply", "msg_id": "trigger"}
            await transport.stop()

            resume_socket = WireSocket([{"op": 10, "d": {"heartbeat_interval": 60000}}])
            transport.connector = lambda *_, **__: FakeSocketContext(resume_socket)
            transport._session_id, transport._sequence, transport._resume_gateway_url = "session", 42, "wss://gateway.qq.test"
            await transport.start(lambda _: asyncio.sleep(0))
            await wait_until(lambda: any(packet["op"] == 6 for packet in resume_socket.sent))
            resume = next(packet for packet in resume_socket.sent if packet["op"] == 6)
            assert resume == {"op": 6, "d": {"token": "QQBot access", "session_id": "session", "seq": 42}}
            await transport.stop()

    asyncio.run(run())


def test_qqbot_reader_receives_stop_and_heartbeat_ack_during_reply() -> None:
    async def run() -> None:
        socket = FakeSocket([{"op": 10, "d": {"heartbeat_interval": 60000}}])
        started, release, completed, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(payload):
            content = payload["d"]["content"]
            if content == "/stop":
                assert started.is_set() and not completed.is_set()
                release.set()
                stopped.set()
            else:
                started.set()
                await release.wait()
                completed.set()

        transport = QQBotGatewayTransport("app", "secret", http_client=FakeHttpClient(), connector=lambda *_, **__: FakeSocketContext(socket))
        await transport.start(handler)
        await wait_until(lambda: bool(socket.sent))
        await socket.incoming.put(json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"content": "hello"}}))
        await asyncio.wait_for(started.wait(), timeout=1)
        await socket.incoming.put(json.dumps({"op": 11}))
        await asyncio.wait_for(transport._heartbeat_ack.wait(), timeout=1)
        await socket.incoming.put(json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"content": "/stop"}}))
        await asyncio.wait_for(stopped.wait(), timeout=1)
        await transport.stop()
        assert completed.is_set()
        assert not transport._event_tasks

    asyncio.run(run())


def test_qqbot_stop_drains_accepted_handlers_before_closing() -> None:
    async def run() -> None:
        socket = FakeSocket([{"op": 10, "d": {"heartbeat_interval": 60000}}])
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(payload):
            started.set()
            await release.wait()
            completed.set()

        transport = QQBotGatewayTransport("app", "secret", http_client=FakeHttpClient(), connector=lambda *_, **__: FakeSocketContext(socket))
        await transport.start(handler)
        await socket.incoming.put(json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"content": "hello"}}))
        await asyncio.wait_for(started.wait(), timeout=1)
        stopping = asyncio.create_task(transport.stop())
        await wait_until(lambda: socket.closed)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, timeout=1)
        assert completed.is_set()

    asyncio.run(run())


def test_feishu_webhook_verifies_signature_and_rest_sender_uses_tenant_token() -> None:
    async def run() -> None:
        client = FakeHttpClient()
        transport = FeishuWebhookTransport(
            "app-1", "app-secret", verification_token="verify", encrypt_key="encrypt",
            endpoint="https://open.feishu.test/open-apis", http_client=client,
        )
        received: list[Mapping[str, Any]] = []

        async def on_event(payload: Mapping[str, Any]) -> None:
            received.append(payload)

        await transport.start(on_event)
        payload = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1", "event_id": "event-1", "token": "verify"},
            "event": {"message": {"message_id": "message-1"}},
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        timestamp, nonce = "123", "nonce"
        signature = hashlib.sha256(f"{timestamp}{nonce}encrypt{raw.decode()}".encode()).hexdigest()
        headers = {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        }
        assert await transport.handle_webhook(raw, headers) == {"code": 0}
        assert received == [payload]

        bad_headers = {**headers, "X-Lark-Signature": "bad"}
        try:
            await transport.handle_webhook(raw, bad_headers)
        except Exception as error:
            assert "签名" in str(error)
        else:
            raise AssertionError("invalid webhook signature was accepted")

        challenge = {"type": "url_verification", "token": "verify", "challenge": "challenge-1"}
        raw_challenge = json.dumps(challenge, separators=(",", ":")).encode()
        challenge_signature = hashlib.sha256(f"{timestamp}{nonce}encrypt{raw_challenge.decode()}".encode()).hexdigest()
        challenge_headers = {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": challenge_signature,
        }
        assert await transport.handle_webhook(raw_challenge, challenge_headers) == {"challenge": "challenge-1"}
        result = await transport("im.v1.message.create", {
            "receive_id_type": "chat_id", "receive_id": "chat-1", "msg_type": "text", "content": "{\"text\":\"hi\"}",
        })
        assert result["id"] == "sent-1"
        send_call = client.calls[-1]
        assert send_call[1] == "https://open.feishu.test/open-apis/im/v1/messages?receive_id_type=chat_id"
        assert send_call[2]["headers"] == {"Authorization": "Bearer access"}
        assert send_call[2]["json"]["receive_id"] == "chat-1"
        assert client.calls[0][1].endswith("/auth/v3/tenant_access_token/internal")
        await transport.stop()

    asyncio.run(run())
