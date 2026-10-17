"""Explicit new owner actions regenerate one selected image through HTTP."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401


def test_owner_regeneration_uses_new_attempt_seed_and_only_replaces_the_selected_image(novelai_service):
    service = novelai_service
    app = FastAPI()
    app.include_router(service.application.router())
    with TestClient(app) as client:
        path = f'/api/roles/{service.role.id}/generations'
        revision = client.get('/api/services/novelai').json()['revision']
        created = client.post(path, json={'requestId': '17217217217217217217217217217217',
            'expectedRevision': revision, 'parameters': service.request})
        assert created.status_code == 200, created.text
        original = created.json()['generation']
        original_message = service.sessions.list_messages(f'role:{service.role.id}')[-1]
        selected = original['attachments'][0]
        other = {'id': 'keep-image', 'name': 'keep.png', 'mediaType': 'image/png', 'size': 10, 'kind': 'image'}
        file = {'id': 'keep-file', 'name': 'notes.txt', 'mediaType': 'text/plain', 'size': 10, 'kind': 'file'}
        service.sessions.update_message(original_message.id, original_message.content, original_message.status,
            metadata={**original_message.metadata, 'attachments': [other, selected, file], 'custom': {'keep': 172}})
        parameters = {key: value for key, value in service.request.items() if key != 'seed'}
        request = {'requestId': '27227227227227227227227227227227', 'expectedRevision': revision,
            'sourceAttemptId': original['attemptId'], 'expectedAttachmentId': selected['id'], 'parameters': parameters}
        endpoint = path + '/' + original['generationId'] + '/regenerate'
        response = client.post(endpoint, json=request)
        assert response.status_code == 200, response.text
        result = response.json()['generation']
        assert result['generationId'] == original['generationId']
        assert result['attemptId'] != original['attemptId']
        assert result['parentAttemptId'] == original['attemptId']
        assert result['parameters']['seed'] != original['parameters']['seed']
        assert result['status'] == 'succeeded' and result['deliveryStatus'] == 'sent'
        assert len(service.peer.requests) == 2
        saved = service.sessions.list_messages(f'role:{service.role.id}')
        assert len(saved) == 1 and saved[0].id == original_message.id
        assert saved[0].metadata['attachments'] == [other, result['attachments'][0], file]
        assert saved[0].metadata['custom'] == {'keep': 172}
        lookup = client.get(path + '/attachment/' + result['attachments'][0]['id'], params={'messageId': saved[0].id})
        assert lookup.status_code == 200, lookup.text
        assert lookup.json()['generation']['attemptId'] == result['attemptId']
        assert client.get(path + '/attachment/' + selected['id'], params={'messageId': saved[0].id}).status_code == 404
        assert client.get(path + '/attachment/' + result['attachments'][0]['id'], params={'messageId': 'foreign'}).status_code == 404
        exact = client.get(path + '/' + original['generationId'], params={'attemptId': original['attemptId']})
        assert exact.status_code == 200 and exact.json()['generation']['attemptId'] == original['attemptId']
        replay = client.post(endpoint, json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()['generation']['attemptId'] == result['attemptId']
        assert len(service.peer.requests) == 2
        history = client.get(path).json()
        assert len(history['generations']) == 2 and history['usage']['manualAttempts'] == 2


@pytest.mark.parametrize('seed,expected_status', [(172, 200), (171, 422)])
def test_regeneration_keeps_an_explicit_new_seed_and_rejects_reusing_the_old_seed(novelai_service, seed, expected_status):
    service = novelai_service
    app = FastAPI()
    app.include_router(service.application.router())
    with TestClient(app) as client:
        path = f'/api/roles/{service.role.id}/generations'
        revision = service.application.settings.public()['revision']
        parent = client.post(path, json={'requestId': '17217217217217217217217217217217',
            'expectedRevision': revision, 'parameters': service.request}).json()['generation']
        response = client.post(path + '/' + parent['generationId'] + '/regenerate', json={
            'requestId': '27227227227227227227227227227227', 'expectedRevision': revision,
            'sourceAttemptId': parent['attemptId'], 'expectedAttachmentId': parent['attachments'][0]['id'],
            'parameters': {**service.request, 'seed': seed}})
        assert response.status_code == expected_status, response.text
        if expected_status == 200:
            assert response.json()['generation']['parameters']['seed'] == seed
        assert len(service.peer.requests) == (2 if expected_status == 200 else 1)
