"""A paid response cannot overwrite a changed slot or buy a second response."""
import base64
import io
import threading
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


@pytest.mark.parametrize('change', ['replace_slot', 'delete_message'])
def test_message_changes_during_real_regeneration_keep_new_image_only_in_history(novelai_service, change):
    service = novelai_service
    app = FastAPI()
    app.include_router(service.application.router())
    with TestClient(app) as client:
        path = f'/api/roles/{service.role.id}/generations'
        revision = service.application.settings.public()['revision']
        original = client.post(path, json={'requestId': '17217217217217217217217217217217',
            'expectedRevision': revision, 'parameters': service.request}).json()['generation']
        old_message = service.sessions.list_messages(f'role:{service.role.id}')[-1]
        service.peer.block = True
        service.peer.accepted.clear()
        endpoint = path + '/' + original['generationId'] + '/regenerate'
        body = {'requestId': '27227227227227227227227227227227', 'expectedRevision': revision,
            'sourceAttemptId': original['attemptId'], 'expectedAttachmentId': original['attachments'][0]['id'],
            'parameters': {key: value for key, value in service.request.items() if key != 'seed'}}
        responses = []
        thread = threading.Thread(target=lambda: responses.append(client.post(endpoint, json=body)))
        thread.start()
        try:
            assert service.peer.accepted.wait(3)
            if change == 'delete_message':
                with service.application.ledger.connect() as connection:
                    connection.execute('DELETE FROM messages WHERE message_id=?', (old_message.id,))
            else:
                changed = {'id': 'changed-by-owner', 'name': 'other.png', 'mediaType': 'image/png', 'size': 10, 'kind': 'image'}
                service.sessions.update_message(old_message.id, old_message.content, old_message.status,
                    metadata={**old_message.metadata, 'attachments': [changed], 'custom': {'owner': 172}})
        finally:
            service.peer.release.set()
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(responses) == 1 and responses[0].status_code == 200, responses
        result = responses[0].json()['generation']
        assert result['status'] == 'succeeded' and result['deliveryStatus'] == 'history_only'
        assert result['errorCode'] == 'replacement_conflict' and result['attachments']
        messages = service.sessions.list_messages(f'role:{service.role.id}')
        if change == 'delete_message':
            assert not messages
        else:
            assert len(messages) == 1 and messages[0].metadata['attachments'] == [changed]
            assert messages[0].metadata['custom'] == {'owner': 172}
        assert client.post(endpoint, json=body).json()['generation']['attemptId'] == result['attemptId']
        assert len(service.peer.requests) == 2


def test_regeneration_multiple_image_protocol_error_preserves_diagnostic_and_old_slot(novelai_service):
    service = novelai_service
    app = FastAPI()
    app.include_router(service.application.router())
    with TestClient(app) as client:
        path = f'/api/roles/{service.role.id}/generations'
        revision = service.application.settings.public()['revision']
        parent = client.post(path, json={'requestId': '17217217217217217217217217217217',
            'expectedRevision': revision, 'parameters': service.request}).json()['generation']
        old = service.sessions.list_messages(f'role:{service.role.id}')[-1]
        image = io.BytesIO()
        Image.new('RGB', (64, 64), '#172172').save(image, format='PNG')
        response = io.BytesIO()
        with zipfile.ZipFile(response, 'w') as archive:
            archive.writestr('one.png', image.getvalue())
            archive.writestr('two.png', image.getvalue())
        service.peer.body = response.getvalue()
        result = client.post(path + '/' + parent['generationId'] + '/regenerate', json={
            'requestId': '27227227227227227227227227227227', 'expectedRevision': revision,
            'sourceAttemptId': parent['attemptId'], 'expectedAttachmentId': parent['attachments'][0]['id'],
            'parameters': {**service.request, 'seed': 172}}).json()['generation']
        assert result['status'] == 'succeeded' and len(result['attachments']) == 2
        assert result['errorCode'] == 'unexpected_multiple_images' and result['deliveryStatus'] == 'history_only'
        assert service.sessions.list_messages(old.sessionKey)[-1].metadata == old.metadata
        assert len(service.peer.requests) == 2


def test_regeneration_img2img_reads_the_new_current_owned_attachment(novelai_main):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/generations'
    revision = service.application.settings.public()['revision']
    parent = client.post(path, json={'requestId': '17217217217217217217217217217217',
        'expectedRevision': revision, 'parameters': service.request}).json()['generation']
    image = io.BytesIO()
    Image.new('RGB', (64, 64), '#bb172a').save(image, format='PNG')
    uploaded = client.post(f'/api/roles/{service.role.id}/attachments', content=image.getvalue(),
        headers={'Content-Type': 'image/png', 'x-attachment-filename': 'new-base.png'})
    assert uploaded.status_code == 200, uploaded.text
    identifier = uploaded.json()['id']
    request = {'requestId': '27227227227227227227227227227227', 'expectedRevision': revision,
        'sourceAttemptId': parent['attemptId'], 'expectedAttachmentId': parent['attachments'][0]['id'],
        'parameters': {**service.request, 'mode': 'img2img', 'seed': 172, 'base_attachment_id': identifier,
            'strength': .6, 'noise': .1}}
    response = client.post(path + '/' + parent['generationId'] + '/regenerate', json=request)
    assert response.status_code == 200, response.text
    assert response.json()['generation']['deliveryStatus'] == 'sent'
    assert len(service.peer.requests) == 2
    assert service.peer.requests[-1]['body']['action'] == 'img2img'
    assert base64.b64decode(service.peer.requests[-1]['body']['parameters']['image']) == image.getvalue()
