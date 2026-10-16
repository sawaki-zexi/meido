import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401


@pytest.mark.parametrize('status', [200, 201])
def test_paid_http_uses_one_post_and_keeps_only_complete_bounded_response(tmp_path, status, monkeypatch):
    from backend.app.novelai_generation.http import NovelaiHttp
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'untrusted-environment-cert.pem'))
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            requests.append((self.path, self.headers.get('Authorization'), json.loads(body)))
            self.send_response(status)
            self.send_header('Content-Type', 'application/zip')
            self.end_headers()
            self.wfile.write(b'controlled-response')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        path = tmp_path / 'response.bin'
        client = NovelaiHttp.for_test(f'http://127.0.0.1:{server.server_port}')
        result = asyncio.run(client.generate({'input': 'fixture tags'}, 'secret-171', path, deadline=time.monotonic() + 10))
        assert result.media_type == 'application/zip'
        assert path.read_bytes() == b'controlled-response'
        assert requests == [('/ai/generate-image', 'Bearer secret-171', {'input': 'fixture tags'})]
        assert not list(tmp_path.glob('*.partial'))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_original_deadline_ends_accepted_http_and_removes_partial_without_retry(novelai_service):
    from backend.app.novelai_generation.http import GenerationHttpError
    service = novelai_service
    service.peer.block = True
    destination = service.application.root / service.role.id / 'controlled-deadline.response'
    async def scenario():
        started = time.monotonic()
        with pytest.raises(GenerationHttpError, match='transport_unknown'):
            await service.application.http.generate({'input': 'fixture'}, 'fixture-secret-171', destination,
                deadline=started + .5)
        assert time.monotonic() - started < 2
        assert service.peer.accepted.is_set() and len(service.peer.requests) == 1
        assert not destination.exists() and not destination.with_suffix('.partial').exists()
    try:
        asyncio.run(scenario())
    finally:
        service.peer.release.set()
