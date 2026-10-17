from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib
import json
import logging
import threading

import pytest


@pytest.fixture(autouse=True)
def isolated_platform(tmp_path, monkeypatch, local_platform_only):
    for key, name in (
        ("MEIDO_DATA_DIR", "data"),
        ("MEIDO_ROLES_DIR", "roles"),
        ("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", "owner"),
    ):
        monkeypatch.setenv(key, str((tmp_path / name).resolve()))
    yield
    assert not [thread for thread in threading.enumerate() if thread.name == "feishu-long-connection"]


@asynccontextmanager
async def held_endpoint():
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = set()
    requests = []

    async def respond(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:")), 0)
            await reader.readexactly(length)
            requests.append(headers.split(b"\r\n", 1)[0].decode())
            entered.set()
            await release.wait()
            body = b'{"msg":"controlled failure"}'
            writer.write(b"HTTP/1.1 500 Error\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/open-apis", entered, release, requests
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(tasks)), 5)


@asynccontextmanager
async def document_endpoint(document):
    tasks = set()

    async def respond(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n") if line.lower().startswith(b"content-length:")), 0)
            await reader.readexactly(length)
            body = json.dumps(document).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/open-apis"
    finally:
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(tasks)), 5)


async def stop_and_join(transport, runner):
    try:
        await transport.stop()
    finally:
        # Keep the network guard installed even if stop itself reports failure.
        # These fixtures release every local barrier before reaching this wait.
        while runner.alive:
            await asyncio.sleep(.01)
        runner._thread.join()


def test_stop_reclaims_real_sdk_while_endpoint_response_is_pending():
    async def scenario():
        # Load the installed dependency under the guard before timing stop.
        importlib.import_module("lark_oapi.ws.client")
        from backend.app.channels.transports import FeishuWebhookTransport

        async with held_endpoint() as (endpoint, entered, release, requests):
            transport = FeishuWebhookTransport("synthetic-id", "synthetic-secret", endpoint=endpoint, long_connection=True)
            await transport.start(lambda payload: asyncio.sleep(0))
            runner = transport._long_connection
            try:
                await asyncio.wait_for(entered.wait(), 10)
                await transport.stop()
                assert not runner.alive, "stop returned successfully while its SDK connection thread remained alive"
                assert requests == ["POST /callback/ws/endpoint HTTP/1.1"]
                assert transport.connection_snapshot()["reason"] == "stopped"
            finally:
                release.set()
                await asyncio.to_thread(runner._thread.join, 5)
                assert not runner.alive
                await transport.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("reject", [False, True])
def test_real_sdk_websocket_logs_never_include_wire_secrets(monkeypatch, caplog, capsys, reject):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        from websockets.asyncio.server import serve
        from websockets.http11 import Response
        from websockets.datastructures import Headers
        importlib.import_module("lark_oapi.ws.client")
        from backend.app.channels.transports import FeishuWebhookTransport

        closed = asyncio.Event()

        async def socket_handler(socket):
            await socket.recv()
            await socket.close(code=1008, reason="remote-close-canary")
            closed.set()

        async def handshake(connection, request):
            if reject:
                closed.set()
                return Response(403, "Forbidden", Headers({"handshake-status": "514", "handshake-autherrcode": "1000040350", "handshake-msg": "remote-close-canary"}), b"secret-canary")

        async with serve(socket_handler, "127.0.0.1", 0, process_request=handshake) as server:
            port = server.sockets[0].getsockname()[1]
            document = {"code": 0, "data": {"URL": f"ws://127.0.0.1:{port}/wire?device_id=private-device-canary&service_id=1&token=url-token-canary"}}
            async with document_endpoint(document) as endpoint:
                transport = FeishuWebhookTransport("synthetic-id", "secret-canary", endpoint=endpoint, long_connection=True, long_connection_reconnect_min=60)
                await transport.start(lambda payload: asyncio.sleep(0))
                runner = transport._long_connection
                try:
                    await asyncio.wait_for(closed.wait(), 3)
                    await asyncio.sleep(.05)
                finally:
                    await stop_and_join(transport, runner)
                    assert not runner.alive

    asyncio.run(scenario())
    # The localhost server deliberately sees the wire; only application/client
    # diagnostics are under this contract.
    messages = "\n".join(record.getMessage() for record in caplog.records if record.name != "websockets.server") + capsys.readouterr().out
    for canary in ("private-device-canary", "url-token-canary", "remote-close-canary", "secret-canary"):
        assert canary not in messages


@pytest.mark.parametrize("pong_config", [{"ReconnectCount": -1}, {"PingInterval": 0}, {"PingInterval": float("inf")}])
def test_real_sdk_protocol_keeps_partial_configuration_and_negative_sentinel(monkeypatch, pong_config):
    async def scenario():
        from websockets.asyncio.server import serve
        from lark_oapi.ws.pb.pbbp2_pb2 import Frame
        from backend.app.channels.transports import FeishuWebhookTransport
        import requests

        synchronous_posts = []

        def forbidden_post(*args, **kwargs):
            synchronous_posts.append(True)
            raise AssertionError("SDK synchronous handshake must never be used")

        monkeypatch.setattr(requests, "post", forbidden_post)
        second_ping = asyncio.Event()
        delivered = asyncio.Queue()
        packets = []

        async def socket_handler(socket):
            ping = Frame.FromString(await socket.recv())
            packets.append(ping)
            pong = Frame(SeqID=0, LogID=0, service=1)
            pong.method = 0
            header = pong.headers.add()
            header.key, header.value = "type", "pong"
            # The omitted PingInterval must keep the handshake value.
            pong.payload = json.dumps(pong_config).encode()
            await socket.send(pong.SerializeToString())
            event = Frame(SeqID=1, LogID=0, service=1)
            event.method = 1
            for key, value in {"type": "event", "message_id": "wire-1", "trace_id": "trace", "sum": "1", "seq": "0"}.items():
                header = event.headers.add()
                header.key, header.value = key, value
            event.payload = json.dumps({"schema": "2.0", "header": {"event_id": "event-1", "event_type": "im.message.receive_v1"}, "event": {"message": {"message_id": "message-1", "content": '{"text":"local fixture"}'}}}).encode()
            await socket.send(event.SerializeToString())
            async for raw in socket:
                packet = Frame.FromString(raw)
                packets.append(packet)
                if any(header.key == "type" and header.value == "ping" for header in packet.headers):
                    second_ping.set()

        async def on_event(payload):
            await delivered.put(payload)

        async with serve(socket_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            document = {"code": 0, "data": {"URL": f"ws://127.0.0.1:{port}/wire?device_id=fixture&service_id=1", "ClientConfig": {"PingInterval": .05, "ReconnectCount": -1}}}
            async with document_endpoint(document) as endpoint:
                transport = FeishuWebhookTransport("synthetic-id", "synthetic-secret", endpoint=endpoint, long_connection=True, long_connection_reconnect_min=60)
                await transport.start(on_event)
                runner = transport._long_connection
                try:
                    payload = await asyncio.wait_for(delivered.get(), 3)
                    assert payload["header"]["event_id"] == "event-1"
                    assert payload["event"]["message"]["message_id"] == "message-1"
                    await asyncio.wait_for(second_ping.wait(), .8)
                finally:
                    await stop_and_join(transport, runner)
                    assert not runner.alive
        assert synchronous_posts == []
        assert packets

    asyncio.run(scenario())
