"""A saved picture is provenance, never a current input or model grant."""
import io
from pathlib import Path

from PIL import Image
import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def _parent(client, service):
    path = f'/api/roles/{service.role.id}/generations'
    revision = service.application.settings.public()['revision']
    response = client.post(path, json={'requestId': '17217217217217217217217217217217',
        'expectedRevision': revision, 'parameters': service.request})
    assert response.status_code == 200, response.text
    parent = response.json()['generation']
    return path + '/' + parent['generationId'] + '/regenerate', {'requestId': '27227227227227227227227227227227',
        'expectedRevision': revision, 'sourceAttemptId': parent['attemptId'],
        'expectedAttachmentId': parent['attachments'][0]['id'], 'parameters': {**service.request, 'seed': 172}}


@pytest.mark.parametrize('source', ['deleted_current_image', 'foreign_role_image'])
def test_regeneration_resolves_current_owned_input_before_reserving_new_consumption(novelai_main, source):
    from backend.app.chat_attachments import resolve_attachment
    from backend.app.models import RoleInput
    client, service = novelai_main
    endpoint, body = _parent(client, service)
    owner = service.role
    if source == 'foreign_role_image':
        owner = service.roles.create(RoleInput(name='foreign', profile={'profile': 'another role'}))
    image = io.BytesIO()
    Image.new('RGB', (64, 64), '#173abc').save(image, format='PNG')
    response = client.post(f'/api/roles/{owner.id}/attachments', content=image.getvalue(),
        headers={'Content-Type': 'image/png', 'x-attachment-filename': 'base.png'})
    assert response.status_code == 200, response.text
    identifier = response.json()['id']
    if source == 'deleted_current_image':
        owned = resolve_attachment(service.roles.workspace_path(owner.id), identifier)
        Path(owned['path']).unlink()
    body['parameters'].update(mode='img2img', base_attachment_id=identifier, strength=.7, noise=.2)
    response = client.post(endpoint, json=body)
    assert response.status_code == 403, response.text
    assert len(service.peer.requests) == 1
    history = service.application.history(service.role.id)
    assert len(history['generations']) == 1 and history['usage']['manualAttempts'] == 1


def test_regeneration_rechecks_current_model_allowlist_after_new_owner_consent(novelai_main):
    client, service = novelai_main
    endpoint, body = _parent(client, service)
    body['parameters']['model'] = 'nai-diffusion-4-5-curated'
    saved = client.put('/api/services/novelai', json={'expectedRevision': body['expectedRevision'],
        'allowedModels': ['nai-diffusion-4-5-full'], 'defaultModel': 'nai-diffusion-4-5-full'})
    assert saved.status_code == 200, saved.text
    revision = saved.json()['revision']
    changed = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'consentRevision': revision}}})
    assert changed.status_code == 200, changed.text
    body['expectedRevision'] = revision
    rejected = client.post(endpoint, json=body)
    assert rejected.status_code == 422, rejected.text
    assert len(service.peer.requests) == 1
    assert len(service.application.history(service.role.id)['generations']) == 1
