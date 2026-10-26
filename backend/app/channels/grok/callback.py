"""Authenticated loopback HTTP, bounded before parsing or admitting messages."""
import asyncio
import hmac
import json
from contextlib import suppress

import h11

from .protocol import CallbackError


class CallbackServer:
    def __init__(self, token, receive):
        self.authorization = ('Bearer ' + token).encode('ascii')
        self.receive = receive
        self.server = None
        self.tasks = set()
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._connected, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def _connected(self, reader, writer):
        if len(self.tasks) >= 16:
            writer.close()
            return
        task = asyncio.create_task(self._serve(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _read(self, connection, reader):
        body = bytearray()
        while True:
            event = connection.next_event()
            if event is h11.NEED_DATA:
                chunk = await reader.read(8192)
                if not chunk:
                    raise CallbackError(400, '请求未完成')
                connection.receive_data(chunk)
            elif isinstance(event, h11.Request):
                headers = dict(event.headers)
                if event.method != b'POST' or b'origin' in headers:
                    raise CallbackError(405, '仅接受本机客户端 POST')
                if not hmac.compare_digest(headers.get(b'authorization', b''), self.authorization):
                    raise CallbackError(401, '回程认证失败')
                if event.target != b'/messages':
                    raise CallbackError(404, '操作不存在')
                if headers.get(b'content-type', b'').split(b';')[0] != b'application/json':
                    raise CallbackError(415, '需要 application/json')
            elif isinstance(event, h11.Data):
                body.extend(event.data)
                if len(body) > 65536:
                    raise CallbackError(413, '消息过长')
            elif isinstance(event, h11.EndOfMessage):
                return json.loads(body.decode('utf-8'))
            else:
                raise CallbackError(400, '请求无效')

    async def _serve(self, reader, writer):
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=8192)
        try:
            try:
                async with asyncio.timeout(10):
                    payload = await self._read(connection, reader)
                    status, response = await self.receive(payload)
            except CallbackError as error:
                status, response = error.status, {'error': error.reason}
            except (h11.RemoteProtocolError, UnicodeError, json.JSONDecodeError):
                status, response = 400, {'error': '请求格式无效'}
            except TimeoutError:
                status, response = 408, {'error': '接收超时，结果未确认'}
            except Exception:
                status, response = 500, {'error': '接收失败，结果未确认'}
            body = json.dumps(response, ensure_ascii=False).encode()
            for event in (h11.Response(status_code=status, headers=[(b'content-type', b'application/json; charset=utf-8'),
                          (b'content-length', str(len(body)).encode()), (b'connection', b'close')]),
                          h11.Data(data=body), h11.EndOfMessage()):
                writer.write(connection.send(event) or b'')
            await writer.drain()
        except (ConnectionError, h11.LocalProtocolError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
