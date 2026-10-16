import io
import json
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401


@pytest.mark.parametrize('failure', ['compressed_response', 'raw_over_32mib', 'archive_17_entries', 'inflate_over_64mib', 'actual_pixels_over_request', 'invalid_object_base64'])
def test_real_http_untrusted_media_limits_reject_once_without_secret_or_late_display(novelai_service, failure):
    service = novelai_service
    if failure == 'compressed_response':
        service.peer.headers['Content-Encoding'] = 'gzip'
    elif failure == 'raw_over_32mib':
        service.peer.body = b'x' * (32 * 1024 * 1024 + 1)
    elif failure == 'invalid_object_base64':
        service.peer.media_type = 'application/json'
        service.peer.body = json.dumps({'images': [{'image': '!!!', 'index': 0, 'seed': 171}]}).encode()
    else:
        body = io.BytesIO()
        with zipfile.ZipFile(body, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            if failure == 'archive_17_entries':
                for index in range(17):
                    archive.writestr(str(index), b'not an image')
            elif failure == 'inflate_over_64mib':
                archive.writestr('oversized.png', b'x' * (64 * 1024 * 1024 + 1))
            else:
                png = io.BytesIO()
                Image.new('RGB', (65, 64)).save(png, format='PNG')
                archive.writestr('oversized.png', png.getvalue())
        service.peer.body = body.getvalue()
    api = FastAPI()
    api.include_router(service.application.router())
    with TestClient(api) as client:
        path = f'/api/roles/{service.role.id}/generations'
        request = {'requestId': '17117117117117117117117117117121',
            'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request}
        response = client.post(path, json=request)
        assert response.status_code == 200, response.text
        view = response.json()['generation']
        assert view['status'] == 'unknown' and view['consumption'] == 'possible'
        assert view['attachments'] == []
        assert client.post(path, json=request).json()['generation']['attemptId'] == view['attemptId']
        assert len(service.peer.requests) == 1
        assert not service.sessions.list_messages(f'role:{service.role.id}')
        assert not list(service.application.root.rglob('*.partial'))
        assert all(value not in response.text for value in ('fixture-secret-171', 'base64', 'path'))
