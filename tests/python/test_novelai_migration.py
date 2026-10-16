"""Existing #171 request identities still retrieve their original paid attempt."""
import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401


def test_legacy_foreground_request_fingerprint_replays_without_new_consumption(novelai_service):
    from backend.app.novelai_generation.ledger import canonical
    service = novelai_service
    app = FastAPI()
    app.include_router(service.application.router())
    with TestClient(app) as client:
        endpoint = f'/api/roles/{service.role.id}/generations'
        request = {'requestId': '17217217217217217217217217217217',
            'expectedRevision': service.application.settings.public()['revision'], 'parameters': service.request}
        original = client.post(endpoint, json=request)
        assert original.status_code == 200, original.text
        saved = original.json()['generation']
        # #171 hashed the exact parameter object and had no deliveryMode input.
        # Preserve that real persisted contract instead of re-buying after upgrade.
        with service.application.ledger.connect() as connection:
            connection.execute('UPDATE novelai_attempts SET fingerprint=? WHERE attempt_id=?',
                (hashlib.sha256(canonical(service.request).encode()).hexdigest(), saved['attemptId']))
        replay = client.post(endpoint, json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()['generation']['attemptId'] == saved['attemptId']
        assert len(service.peer.requests) == 1
        assert len(service.sessions.list_messages(f'role:{service.role.id}')) == 1


def test_a_new_action_on_an_already_replaced_slot_is_rejected_before_paid_post(novelai_service):
    service = novelai_service
    app = FastAPI()
    app.include_router(service.application.router())
    with TestClient(app) as client:
        endpoint = f'/api/roles/{service.role.id}/generations'
        revision = service.application.settings.public()['revision']
        original = client.post(endpoint, json={'requestId': '17217217217217217217217217217217',
            'expectedRevision': revision, 'parameters': service.request}).json()['generation']
        endpoint += '/' + original['generationId'] + '/regenerate'
        request = {'requestId': '27227227227227227227227227227227', 'expectedRevision': revision,
            'sourceAttemptId': original['attemptId'], 'expectedAttachmentId': original['attachments'][0]['id'],
            'parameters': {**service.request, 'seed': 172}}
        response = client.post(endpoint, json=request)
        assert response.status_code == 200, response.text
        assert client.post(endpoint, json=request).status_code == 200
        assert len(service.peer.requests) == 2
        rejected = client.post(endpoint, json={**request, 'requestId': '37237237237237237237237237237237'})
        assert rejected.status_code == 409, rejected.text
        assert len(service.peer.requests) == 2
