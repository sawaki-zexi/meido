"""Handshake budgets through the real SDK, transport and localhost protocols."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib
import gzip
import json
import threading

import pytest


APP_ID = "budget-fixture-id"
APP_SECRET = "budget-secret-canary"
RESPONSE_CANARY = "remote-response-canary"
URL_CANARY = "websocket-token-canary"


@pytest.fixture(autouse=True)
def isolated_platform(tmp_path, monkeypatch, local_platform_only):
    for key, name in (
        ("MEIDO_DATA_DIR", "data"),
        ("MEIDO_ROLES_DIR", "roles"),
        ("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", "owner"),
    ):
        monkeypatch.setenv(key, str((tmp_path / name).resolve()))
    # Keep dependency import time out of protocol deadlines, under the guard.
    importlib.import_module("lark_oapi.ws.client")
    import requests

    synchronous_posts = []

    def forbidden_post(*args, **kwargs):
        synchronous_posts.append(True)
        raise AssertionError("SDK synchronous discovery is forbidden")

    monkeypatch.setattr(requests, "post", forbidden_post)
    yield
    assert synchronous_posts == [], "A caught synchronous discovery attempt is still a failure"
    assert not [thread for thread in threading.enumerate() if thread.name == "feishu-long-connection"]


@asynccontextmanager
async def http_endpoint(send_response):
    """A real HTTP origin; handlers and sockets outlive every client cleanup."""
    active, requests = set(), []

    async def respond(reader, writer):
        task = asyncio.current_task()
        active.add(task)
        try:
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            lines = raw_headers.decode("ascii").split("\r\n")
            headers = dict(line.split(":", 1) for line in lines[1:] if line)
            headers = {key.lower(): value.strip() for key, value in headers.items()}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            requests.append((lines[0], headers, json.loads(body)))
            await send_response(writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            active.discard(task)

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield origin, requests
    finally:
        server.close()
        await server.wait_closed()
        if active:
            await asyncio.wait_for(asyncio.gather(*tuple(active)), 3)


def document_response(document, *, status="200 OK", extra_headers=b"", content_length=True):
    body = json.dumps(document, separators=(",", ":")).encode()

    async def respond(writer):
        length = b"Content-Length: " + str(len(body)).encode() + b"\r\n" if content_length else b""
        writer.write(b"HTTP/1.1 " + status.encode() + b"\r\nConnection: close\r\n" + length + extra_headers + b"\r\n" + body)
        await writer.drain()

    return respond


@asynccontextmanager
async def recording_transport(monkeypatch, endpoint):
    """Only observe and rethrow; the factory and SDK still run in Runner's loop."""
    from backend.app.channels import transports

    host_loop = asyncio.get_running_loop()
    errors = asyncio.Queue()
    original_factory = transports.feishu_sdk_connection_factory

    def observed_factory(*args):
        actual = original_factory(*args)

        def factory(on_event):
            connection = actual(on_event)

            class RecordingConnection:
                async def connect(self):
                    try:
                        await connection.connect()
                    except Exception as error:
                        host_loop.call_soon_threadsafe(errors.put_nowait, error)
                        raise

                def __getattr__(self, name):
                    return getattr(connection, name)

            return RecordingConnection()

        return factory

    monkeypatch.setattr(transports, "feishu_sdk_connection_factory", observed_factory)
    transport = transports.FeishuWebhookTransport(
        APP_ID, APP_SECRET, endpoint=endpoint, long_connection=True,
        long_connection_reconnect_min=60,
    )
    await transport.start(lambda payload: asyncio.sleep(0))
    runner = transport._long_connection
    try:
        yield transport, errors
    finally:
        await transport.stop()
        assert not runner.alive


def assert_no_canaries(text):
    for canary in (APP_SECRET, RESPONSE_CANARY, URL_CANARY):
        assert canary not in text


@pytest.mark.parametrize("violation", ("depth", "nodes", "url"))
def test_oversized_structure_is_rejected_before_real_websocket(monkeypatch, caplog, capsys, violation):
    async def scenario():
        from websockets.asyncio.server import ServerConnection, serve
        from backend.app.channels.base import ChannelAdapterError

        sockets, accepted_connections = [], []

        class ObservedServerConnection(ServerConnection):
            def connection_made(self, transport):
                # Count TCP admission too: a too-long URL may be rejected by
                # the websocket HTTP parser before its handler is invoked.
                accepted_connections.append(True)
                super().connection_made(transport)

        async def websocket(socket):
            sockets.append(socket)
            await socket.wait_closed()

        async with serve(websocket, "127.0.0.1", 0, create_connection=ObservedServerConnection) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/wire?device_id=fixture&service_id=1&token={URL_CANARY}"
            document = {"code": 0, "data": {"URL": url}}
            if violation == "depth":
                extension = RESPONSE_CANARY
                for _ in range(12):
                    extension = {"nested": extension}
                document["data"]["extension"] = extension
            elif violation == "nodes":
                document["data"]["extension"] = [RESPONSE_CANARY] * 300
            else:
                document["data"]["URL"] += "x" * 9000
            async with http_endpoint(document_response(document)) as (origin, requests):
                async with recording_transport(monkeypatch, origin + "/open-apis/unused") as (transport, errors):
                    error = await asyncio.wait_for(errors.get(), 3)
                    assert isinstance(error, ChannelAdapterError)
                    assert not transport.connected
                    assert sockets == []
                    assert accepted_connections == []
                    assert_no_canaries(str(error))
                assert requests[0][0] == "POST /callback/ws/endpoint HTTP/1.1"

    asyncio.run(scenario())
    assert_no_canaries(caplog.text + capsys.readouterr().out)


def test_slow_chunked_handshake_has_a_total_deadline(monkeypatch):
    async def scenario():
        from backend.app.channels import feishu_connection
        from backend.app.channels.base import ChannelAdapterError

        # Include real HTTP client's TLS-context setup; a tiny .2s budget can
        # expire before the localhost origin is reached on a loaded machine.
        monkeypatch.setattr(feishu_connection, "HANDSHAKE_TIMEOUT", 2.0)
        entered, sent = asyncio.Event(), []

        async def drip(writer):
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
            await writer.drain()
            entered.set()
            for index in range(50):
                writer.write(b"10\r\n" + b" " * 16 + b"\r\n")
                await writer.drain()
                sent.append(index)
                await asyncio.sleep(.1)
            writer.write(b"0\r\n\r\n")
            await writer.drain()

        async with http_endpoint(drip) as (origin, requests):
            async with recording_transport(monkeypatch, origin + "/open-apis") as (transport, errors):
                await asyncio.wait_for(entered.wait(), 3)
                started = asyncio.get_running_loop().time()
                error = await asyncio.wait_for(errors.get(), 2.8)
                assert asyncio.get_running_loop().time() - started < 2.8
                assert isinstance(error, ChannelAdapterError)
                assert not transport.connected
                assert 2 <= len(sent) < 50
            assert len(requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("encoding", ("identity", "gzip"))
def test_raw_response_budget_applies_without_content_length(monkeypatch, encoding):
    async def scenario():
        # Identity exceeds the raw 64KiB cap. Gzip is small on the wire but must
        # be rejected before any potentially much larger decompression.
        payload = b'{"padding":"' + b"x" * (70 * 1024) + b'"}'
        if encoding == "gzip":
            payload = gzip.compress(b"x" * (2 * 1024 * 1024))

        async def send(writer):
            writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Encoding: " + encoding.encode() + b"\r\n\r\n" + payload)
            await writer.drain()

        async with http_endpoint(send) as (origin, requests):
            async with recording_transport(monkeypatch, origin + "/open-apis") as (transport, errors):
                error = await asyncio.wait_for(errors.get(), 3)
                expected = "feishu_handshake_size" if encoding == "identity" else "feishu_handshake_encoding"
                assert str(error) == expected
                assert not transport.connected
            assert requests[0][1]["accept-encoding"] == "identity"

    asyncio.run(scenario())


@pytest.mark.parametrize("http_status,platform_code,kind,expected_code", (
    ("200 OK", 514, "ClientException", 514),
    ("200 OK", 1, "ServerException", 1),
    ("503 Service Unavailable", 0, "ServerException", 503),
))
def test_remote_errors_keep_safe_sdk_classification_and_request_contract(monkeypatch, caplog, capsys, http_status, platform_code, kind, expected_code):
    async def scenario():
        document = {"code": platform_code, "msg": RESPONSE_CANARY + APP_SECRET + URL_CANARY}
        async with http_endpoint(document_response(document, status=http_status)) as (origin, requests):
            # Existing endpoint conversion must preserve a custom path prefix.
            async with recording_transport(monkeypatch, origin + "/tenant/open-apis/unused") as (transport, errors):
                error = await asyncio.wait_for(errors.get(), 3)
                assert type(error).__name__ == kind
                assert error.code == expected_code
                assert_no_canaries(str(error))
                assert not transport.connected
            assert len(requests) == 1
            method, headers, request_body = requests[0]
            assert method == "POST /tenant/callback/ws/endpoint HTTP/1.1"
            assert request_body == {"AppID": APP_ID, "AppSecret": APP_SECRET}
            assert headers["locale"] == "zh"
            assert headers["accept-encoding"] == "identity"
            assert headers["content-type"] == "application/json"
            assert headers["user-agent"].startswith("oapi-sdk-python/")

    asyncio.run(scenario())
    assert_no_canaries(caplog.text + capsys.readouterr().out)
