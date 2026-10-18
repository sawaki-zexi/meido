from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def test_global_manual_permission_applies_to_new_role_without_enabling_its_scene_cg(novelai_main):
    client, service = novelai_main
    role = client.post('/api/roles', json={'name': 'global manual', 'profile': {'profile': 'role'}}).json()['role']
    path = f"/api/roles/{role['id']}/generations"
    config = client.get('/api/services/novelai').json()
    saved = client.put('/api/services/novelai', json={'manualEnabled': True, 'manualDailyAttempts': 2,
        'confirmManualConsumption': True, 'expectedRevision': config['revision']})
    assert saved.status_code == 200, saved.text
    result = client.post(path, json={'requestId': '11811811811811811811811811811811',
        'expectedRevision': saved.json()['revision'], 'parameters': service.request})
    assert result.status_code == 200, result.text
    assert result.json()['generation']['status'] == 'succeeded'
    fresh = client.get(f"/api/roles/{role['id']}").json()['role']['agentConfig']['novelai']
    assert not fresh['manualEnabled'] and not fresh['sceneCgEnabled'] and fresh['consentRevision'] == 0
    assert len(service.peer.requests) == 1
    changed = client.put('/api/services/novelai', json={'maxSteps': 12,
        'expectedRevision': saved.json()['revision']})
    assert changed.status_code == 200
    assert changed.json()['manualConsentRevision'] == 0
    denied = client.post(path, json={'requestId': '11811811811811811811811811811812',
        'expectedRevision': changed.json()['revision'], 'parameters': service.request})
    assert denied.status_code != 200
    assert len(service.peer.requests) == 1


def test_explicit_owner_ui_generates_once_in_original_session_without_fake_human_run(novelai_service):
    service = novelai_service
    application = FastAPI()
    application.include_router(service.application.router())
    with TestClient(application) as client:
        config = client.get('/api/services/novelai').json()
        assert config['tokenConfigured'] and 'token' not in config
        path = f'/api/roles/{service.role.id}/generations'
        request = {'requestId': '17117117117117117117117117117117', 'expectedRevision': config['revision'],
                   'parameters': service.request}
        first = client.post(path, json=request)
        assert first.status_code == 200, first.text
        generation = first.json()['generation']
        assert generation['status'] == 'succeeded'
        assert client.post(path, json=request).json()['generation']['attemptId'] == generation['attemptId']
        assert len(service.peer.requests) == 1
        messages = service.sessions.list_messages(f'role:{service.role.id}')
        assert len(messages) == 1 and messages[0].role == 'assistant' and messages[0].runId is None
        assert messages[0].metadata['attachments'] == generation['attachments']
        history = client.get(path).json()['generations']
        assert len(history) == 1 and history[0]['attemptId'] == generation['attemptId']
        assert all(secret not in first.text for secret in ('fixture-secret-171', '"path"', 'base64'))


@pytest.mark.parametrize('status,body,expected', [(401, b'private response', 'failed_response'),
    (402, b'private response', 'failed_response'), (408, b'private response', 'unknown'),
    (429, b'private response', 'unknown'), (500, b'private response', 'unknown'),
    (201, b'not an image archive', 'unknown')])
def test_remote_failure_never_retries_paid_post_and_consumption_stays_honest(novelai_service, status, body, expected):
    service = novelai_service
    service.peer.status, service.peer.body = status, body
    application = FastAPI()
    application.include_router(service.application.router())
    with TestClient(application) as client:
        path = f'/api/roles/{service.role.id}/generations'
        request = {'requestId': '17117117117117117117117117117117', 'expectedRevision': service.application.settings.public()['revision'],
                   'parameters': service.request}
        first = client.post(path, json=request).json()['generation']
        assert first['status'] == expected
        assert first['consumption'] == 'possible'
        assert client.post(path, json=request).json()['generation']['attemptId'] == first['attemptId']
        assert len(service.peer.requests) == 1
        assert service.application.history(service.role.id)['usage']['accountAttempts'] == 1
        assert not service.sessions.list_messages(f'role:{service.role.id}')


def test_bad_later_archive_entry_preserves_verified_images_in_history_and_settles_actual_storage(novelai_service):
    import io
    import zipfile
    service = novelai_service
    with zipfile.ZipFile(io.BytesIO(service.peer.body)) as source:
        png = source.read('image.png')
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        archive.writestr('../../untrusted.png', png)
        archive.writestr('broken.png', b'not an image')
    service.peer.body = body.getvalue()
    application = FastAPI()
    application.include_router(service.application.router())
    with TestClient(application) as client:
        result = client.post(f'/api/roles/{service.role.id}/generations', json={
            'requestId': '17117117117117117117117117117120', 'expectedRevision': service.application.settings.public()['revision'],
            'parameters': service.request}).json()['generation']
        assert result['status'] == 'unknown' and len(result['attachments']) == 1
        assert result['errorCode'] == 'response_not_verifiable'
        usage = service.application.history(service.role.id)['usage']
        assert usage['roleMediaBytes'] == len(png) + len(service.peer.body)
        assert usage['reservedMediaBytes'] == 0
        assert not service.sessions.list_messages(f'role:{service.role.id}')
        assert len(service.peer.requests) == 1


def test_same_img2img_ui_request_reloads_frozen_result_after_original_input_is_removed(novelai_service):
    import io
    import zipfile
    from backend.app.chat_attachments import save_attachment
    service = novelai_service
    with zipfile.ZipFile(io.BytesIO(service.peer.body)) as archive:
        png = archive.read('image.png')
    base = save_attachment(service.roles.workspace_path(service.role.id), 'base.png', 'image/png', png)
    api = FastAPI()
    api.include_router(service.application.router())
    with TestClient(api) as client:
        path = f'/api/roles/{service.role.id}/generations'
        parameters = {key: value for key, value in service.request.items() if key != 'seed'}
        parameters.update(mode='img2img', base_attachment_id=base['id'])
        request = {'requestId': '17117117117117117117117117117122',
            'expectedRevision': service.application.settings.public()['revision'], 'parameters': parameters}
        first = client.post(path, json=request).json()['generation']
        assert first['status'] == 'succeeded'
        import base64
        sent = service.peer.requests[0]['body']
        assert sent['action'] == 'img2img'
        assert base64.b64decode(sent['parameters']['image']) == png
        from pathlib import Path
        Path(base['path']).unlink()
        repeated = client.post(path, json=request)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['generation']['parameters']['seed'] == first['parameters']['seed']
        assert len(service.peer.requests) == 1
        assert len(service.sessions.list_messages(f'role:{service.role.id}')) == 1


def test_unknown_consumes_persistent_finite_allowance_and_same_request_can_still_be_observed(novelai_main):
    client, service = novelai_main
    service.peer.status = 500
    current = service.application.settings.public()
    config = client.put('/api/services/novelai', json={'dailyAttemptLimit': 1, 'confirmManualConsumption': True, 'expectedRevision': current['revision']}).json()
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'consentRevision': config['revision']}}})
    assert response.status_code == 200
    path = f'/api/roles/{service.role.id}/generations'
    request = {'requestId': '17117117117117117117117117117123', 'expectedRevision': config['revision'], 'parameters': service.request}
    first = client.post(path, json=request).json()['generation']
    assert first['status'] == 'unknown'
    assert client.post(path, json=request).json()['generation']['attemptId'] == first['attemptId']
    rejected = client.post(path, json={**request, 'requestId': '17117117117117117117117117117124'})
    assert rejected.status_code == 422
    assert service.application.history(service.role.id)['usage']['accountAttempts'] == 1
    assert len(service.peer.requests) == 1


def test_unexpected_multiple_images_are_all_kept_as_protocol_anomaly_without_normal_reply_attachment(novelai_service):
    import io
    import zipfile
    service = novelai_service
    with zipfile.ZipFile(io.BytesIO(service.peer.body)) as original:
        png = original.read('image.png')
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        archive.writestr('first.png', png)
        archive.writestr('second.png', png)
    service.peer.body = body.getvalue()
    api = FastAPI()
    api.include_router(service.application.router())
    with TestClient(api) as client:
        path = f'/api/roles/{service.role.id}/generations'
        response = client.post(path, json={'requestId': '17117117117117117117117117117125',
            'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request})
        view = response.json()['generation']
        assert len(view['attachments']) == 2 and view['errorCode'] == 'unexpected_multiple_images'
        assert not service.sessions.list_messages(f'role:{service.role.id}')
        assert client.get(path).json()['generations'][0]['deliveryStatus'] == 'history_only'
        assert len(service.peer.requests) == 1
