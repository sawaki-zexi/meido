import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
import httpx

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


def test_original_deadline_ends_accepted_http_and_removes_partial_without_retry(novelai_service, monkeypatch):
    from backend.app.novelai_generation.http import GenerationHttpError
    service = novelai_service
    service.peer.block = True
    destination = service.application.root / service.role.id / 'controlled-deadline.response'
    async def scenario():
        # Trust-store preparation can itself exceed 500ms on Windows. This
        # case exercises a request accepted by the peer, with the real network
        # transport ready before the measured request deadline begins.
        transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        try:
            monkeypatch.setattr(httpx, 'AsyncHTTPTransport', lambda **kwargs: transport)
            started = time.monotonic()
            with pytest.raises(GenerationHttpError, match='transport_unknown'):
                await service.application.http.generate({'input': 'fixture'}, 'fixture-secret-171', destination,
                    deadline=started + .5)
            assert time.monotonic() - started < 2
            assert service.peer.accepted.is_set() and len(service.peer.requests) == 1
            assert not destination.exists() and not destination.with_suffix('.partial').exists()
        finally:
            await transport.aclose()
    try:
        asyncio.run(scenario())
    finally:
        service.peer.release.set()


@pytest.mark.parametrize('response_format', ['binary_zip', 'gzip_zip', 'deflate_zip', 'shiori_json'])
def test_manual_generation_imports_shiori_compatible_responses_once(novelai_service, response_format):
    """Exercise the public API, actual HTTP transport and owned image import."""
    import base64
    import gzip
    import io
    import zipfile
    import zlib
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.chat_attachments import resolve_attachment

    service = novelai_service
    if response_format == 'binary_zip':
        service.peer.media_type = 'application/octet-stream'
    elif response_format == 'gzip_zip':
        service.peer.headers['Content-Encoding'] = 'gzip'
        service.peer.body = gzip.compress(service.peer.body)
    elif response_format == 'deflate_zip':
        service.peer.headers['Content-Encoding'] = 'deflate'
        service.peer.body = zlib.compress(service.peer.body)
    else:
        with zipfile.ZipFile(io.BytesIO(service.peer.body)) as archive:
            png = archive.read('image.png')
        service.peer.media_type = 'application/json'
        service.peer.body = json.dumps({'images': [base64.b64encode(png).decode('ascii')]}).encode()
    api = FastAPI()
    api.include_router(service.application.router())
    with TestClient(api) as client:
        path = f'/api/roles/{service.role.id}/generations'
        request = {'requestId': '17117117117117117117117117117129',
            'expectedRevision': service.application.settings.public()['revision'],
            'parameters': {**service.request, 'prompt': '1girl, library, sunset light'}}
        response = client.post(path, json=request)
        assert response.status_code == 200, response.text
        view = response.json()['generation']
        assert view['status'] == 'succeeded', view
        assert len(view['attachments']) == 1
        attachment = resolve_attachment(service.roles.workspace_path(service.role.id), view['attachments'][0]['id'])
        assert attachment['mediaType'] == 'image/png'
        assert client.post(path, json=request).json()['generation']['attemptId'] == view['attemptId']
        assert client.get(path).json()['generations'][0]['attachments'] == view['attachments']
        assert len(service.peer.requests) == 1
        assert not list(service.application.root.rglob('*.partial'))


def test_chat_generation_accepts_headerless_zip_without_retry(novelai_service):
    from datetime import datetime, timezone
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun

    service = novelai_service
    service.peer.media_type = ''
    role = service.role
    session_key = f'role:{role.id}'
    service.sessions.create_run_with_messages(AgentRun(runId='binary-image-run', roleId=role.id,
        sessionKey=session_key, status='running', startedAt=datetime.now(timezone.utc),
        modelSnapshot={'humanInputSource': 'desktop_owner'}), 'Draw yourself in a library.')
    result = asyncio.run(service.application.tool_for(role.id, is_owner=True).execute(
        {'prompt': '1girl, library, sunset light'},
        ToolContext(role.id, session_key, 'binary-image-run', CancellationToken(), 'binary-image-call')))
    assert not result.is_error, result
    assert result.details['status'] == 'succeeded' and len(result.details['attachments']) == 1
    assert len(service.peer.requests) == 1
    assert service.peer.requests[0]['body']['parameters']['width'] == 1024


@pytest.mark.parametrize('body,encoding,code', [
    (b'not a gzip stream', 'gzip', 'invalid_content_encoding'),
    (b'not an image', 'br', 'unsupported_content_encoding'),
])
def test_response_errors_expose_safe_diagnostics_without_retry(novelai_service, body, encoding, code):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    service = novelai_service
    service.peer.body = body
    service.peer.headers['Content-Encoding'] = encoding
    api = FastAPI()
    api.include_router(service.application.router())
    with TestClient(api) as client:
        request = {'requestId': '17117117117117117117117117117130',
            'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request}
        path = f'/api/roles/{service.role.id}/generations'
        response = client.post(path, json=request)
        view = response.json()['generation']
        assert view['status'] == 'unknown' and view['errorCode'] == code
        assert '压缩' in view['errorMessage']
        assert body.decode() not in response.text and 'fixture-secret' not in response.text
        assert client.post(path, json=request).json()['generation']['attemptId'] == view['attemptId']
        assert len(service.peer.requests) == 1
        assert not list(service.application.root.rglob('*.partial'))
