"""A real idle connection must not prevent the original scene peer from exiting."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from novelai_scene_fixtures import scene_main
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def test_idle_scene_connection_does_not_block_original_server_close(scene_main, monkeypatch):
    from backend.app import main
    _, _, _, connections = scene_main
    endpoint = urlsplit(main.model_configuration_store.get(connections[0]).baseUrl)
    entered, returned, forced = threading.Event(), threading.Event(), threading.Event()
    handle, close = BaseHTTPRequestHandler.handle_one_request, ThreadingHTTPServer.server_close

    def tracked_handle(handler):
        if handler.server.server_port != endpoint.port:
            return handle(handler)
        entered.set()
        try:
            return handle(handler)
        finally:
            returned.set()

    monkeypatch.setattr(BaseHTTPRequestHandler, 'handle_one_request', tracked_handle)
    connection = socket.create_connection((endpoint.hostname, endpoint.port), timeout=3)
    assert entered.wait(3)

    def release():
        forced.set()
        connection.shutdown(socket.SHUT_RDWR)
        connection.close()

    def tracked_close(server):
        if server.server_port != endpoint.port:
            return close(server)
        # The watchdog only releases this test's socket, never fakes server exit.
        watchdog = threading.Timer(1.5, release)
        watchdog.start()
        try:
            close(server)
        finally:
            watchdog.cancel()
            watchdog.join()
            connection.close()
        assert returned.is_set() and not forced.is_set(), 'original server_close waited for an idle client to disconnect'

    monkeypatch.setattr(ThreadingHTTPServer, 'server_close', tracked_close)
