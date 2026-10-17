from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from websockets.asyncio.server import serve

from backend.app.channels.directory import DirectoryError
from backend.app.channels.transports import FeishuWebhookTransport, NapCatWebSocketTransport, QQBotGatewayTransport

pytestmark = pytest.mark.usefixtures("local_platform_only")


async def ignore_event(payload):
    pass


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.reply = {"online": True}

    async def recv(self):
        packet = await self.incoming.get()
        if packet is None:
            raise ConnectionError("closed")
        return packet

    async def send(self, raw):
        packet = json.loads(raw)
        self.sent.append(packet)
        if "echo" in packet:
            await self.incoming.put(json.dumps({
                "status": "ok", "retcode": 0, "echo": packet["echo"], "data": self.reply,
            }))

    async def close(self):
        await self.incoming.put(None)


class SocketContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_):
        await self.socket.close()


def test_napcat_local_websocket_exposes_oversized_directory_response():
    async def scenario():
        async def respond(socket):
            request = json.loads(await socket.recv())
            await socket.send(json.dumps({
                "status": "ok", "retcode": 0, "echo": request["echo"],
                "data": {"blob": "x" * 1_048_576},
            }))
            await socket.wait_closed()

        async with serve(respond, "127.0.0.1", 0, compression=None) as server:
            port = server.sockets[0].getsockname()[1]
            transport = NapCatWebSocketTransport(
                f"ws://127.0.0.1:{port}", reconnect_min=60, request_timeout=2,
            )
            try:
                await transport.start(ignore_event)
                await transport.wait_connected()
                with pytest.raises(DirectoryError, match="^directory_too_large$"):
                    await transport("get_group_list", {})
            finally:
                await transport.stop()

    asyncio.run(scenario())


def test_napcat_local_websocket_accepts_response_at_exact_one_mebibyte():
    async def scenario():
        async def respond(socket):
            request = json.loads(await socket.recv())
            prefix = '{"status":"ok","retcode":0,"echo":"' + request["echo"] + '","data":{"blob":"'
            suffix = '"}}'
            raw = prefix + "x" * (1_048_576 - len(prefix) - len(suffix)) + suffix
            assert len(raw.encode("utf-8")) == 1_048_576
            await socket.send(raw)
            await socket.wait_closed()

        async with serve(respond, "127.0.0.1", 0, compression=None) as server:
            port = server.sockets[0].getsockname()[1]
            transport = NapCatWebSocketTransport(f"ws://127.0.0.1:{port}")
            try:
                await transport.start(ignore_event)
                await transport.wait_connected()
                result = await transport("get_group_list", {})
                assert result["blob"].startswith("xxxx")
                assert len(result["blob"]) > 1_048_000
            finally:
                await transport.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [b"x" * 1_048_577, "汉" * 349_526], ids=["binary", "utf8"])
def test_napcat_checks_raw_byte_limit_before_json_for_custom_connectors(raw):
    async def scenario():
        class OversizedSocket(Socket):
            async def send(self, request):
                await self.incoming.put(raw)

        socket = OversizedSocket()
        transport = NapCatWebSocketTransport(
            "ws://unused", connector=lambda *args, **options: SocketContext(socket), reconnect_min=60,
        )
        try:
            await transport.start(ignore_event)
            await transport.wait_connected()
            with pytest.raises(DirectoryError, match="^directory_too_large$"):
                await transport("get_friend_list", {})
        finally:
            await transport.stop()

    asyncio.run(scenario())


def test_napcat_snapshot_needs_status_evidence_from_current_connection():
    async def scenario():
        sockets = [Socket(), Socket()]
        opened = []

        def connect(url, **options):
            assert options["max_size"] == 1_048_576
            socket = sockets[len(opened)]
            opened.append(socket)
            return SocketContext(socket)

        transport = NapCatWebSocketTransport("ws://unused", connector=connect, reconnect_min=0)
        assert transport.connection_snapshot()["online"] is None
        assert transport.connection_snapshot()["observedAt"] is None
        try:
            await transport.start(ignore_event)
            await transport.wait_connected()
            assert transport.connection_snapshot()["connection"] == "connected"
            assert transport.connection_snapshot()["online"] is None
            sockets[0].reply = {"online": "true"}
            await transport("get_status", {})
            assert transport.connection_snapshot()["online"] is None
            sockets[0].reply = {"online": True}
            await transport("get_status", {})
            online = transport.connection_snapshot()
            assert online["online"] is True
            assert online["observedAt"]
            await sockets[0].close()
            await wait_until(lambda: len(opened) == 2 and transport.connected)
            assert transport.connection_snapshot()["online"] is None
            assert transport.connection_snapshot()["observedAt"] != online["observedAt"]
            sockets[1].reply = {"online": False}
            await transport("get_status", {})
            assert transport.connection_snapshot()["online"] is False
        finally:
            await transport.stop()
        assert transport.connection_snapshot()["connection"] == "disconnected"
        assert transport.connection_snapshot()["online"] is None

    asyncio.run(scenario())


def test_qqbot_snapshot_needs_gateway_ready_or_resumed_in_current_connection():
    async def scenario():
        sockets = [Socket(), Socket()]
        opened = []

        def connect(url, **options):
            socket = sockets[len(opened)]
            opened.append(socket)
            return SocketContext(socket)

        def respond(request):
            if request.url.path.endswith("getAppAccessToken"):
                return httpx.Response(200, json={"access_token": "memory-only", "expires_in": 3600})
            return httpx.Response(200, json={"url": "wss://unused"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            transport = QQBotGatewayTransport(
                "memory-app", "memory-secret", http_client=client,
                connector=connect, reconnect_min=0,
            )
            assert transport.connection_snapshot()["online"] is None
            try:
                await transport.start(ignore_event)
                await wait_until(lambda: transport.connected)
                assert transport.connection_snapshot()["online"] is None
                await sockets[0].incoming.put(json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {}}))
                await asyncio.sleep(0)
                assert transport.connection_snapshot()["online"] is None
                await sockets[0].incoming.put(json.dumps({"op": 0, "t": "READY", "d": {"session_id": "one"}}))
                await wait_until(lambda: transport.connection_snapshot()["online"] is True)
                assert transport.connection_snapshot()["observedAt"]
                await sockets[0].close()
                await wait_until(lambda: len(opened) == 2 and transport.connected)
                assert transport.connection_snapshot()["online"] is None
                await sockets[1].incoming.put(json.dumps({"op": 0, "t": "RESUMED", "d": None}))
                await wait_until(lambda: transport.connection_snapshot()["online"] is True)
            finally:
                await transport.stop()
            assert transport.connection_snapshot()["connection"] == "disconnected"
            assert transport.connection_snapshot()["online"] is False

    asyncio.run(scenario())


def test_feishu_snapshot_uses_long_connection_evidence_and_not_webhook_handler():
    class Connection:
        def __init__(self):
            self.connected = False

        async def connect(self):
            self.connected = True

        def is_connected(self):
            return self.connected

        async def keepalive(self):
            await asyncio.Future()

        async def disconnect(self):
            self.connected = False

    async def scenario():
        connections = []

        def factory(on_event):
            connection = Connection()
            connections.append(connection)
            return connection

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
            webhook = FeishuWebhookTransport("memory-app", "memory-secret", http_client=client)
            await webhook.start(ignore_event)
            try:
                snapshot = webhook.connection_snapshot()
                assert snapshot["connection"] == "unknown"
                assert snapshot["online"] is None
                assert snapshot["observedAt"] is None
            finally:
                await webhook.stop()

            transport = FeishuWebhookTransport(
                "memory-app", "memory-secret", http_client=client,
                long_connection_factory=factory, long_connection_reconnect_min=60,
            )
            try:
                await transport.start(ignore_event)
                await wait_until(lambda: transport.connection_snapshot()["online"] is True)
                snapshot = transport.connection_snapshot()
                assert snapshot["connection"] == "connected"
                assert snapshot["observedAt"]
                connections[0].connected = False
                await wait_until(lambda: transport.connection_snapshot()["online"] is False)
                assert transport.connection_snapshot()["connection"] == "connecting"
            finally:
                await transport.stop()
            assert transport.connection_snapshot()["connection"] == "disconnected"
            assert transport.connection_snapshot()["online"] is False

    asyncio.run(scenario())
