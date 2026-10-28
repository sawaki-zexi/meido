"""Owner-enabled ordinary images enter the role library without new generation."""
import asyncio
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401


@pytest.mark.parametrize('outcome', ['applied', 'owner_changed_background', 'lost_local_ack'])
def test_ordinary_generation_applies_first_image_and_preserves_later_owner_choices(novelai_service, monkeypatch, outcome):
    service = novelai_service
    app = service.application
    settings = app.settings.save({'autoWritebackRoleAssets': True, 'confirmManualConsumption': True}, expected_revision=app.settings.public()['revision'])
    api = FastAPI()
    api.include_router(app.router())
    with TestClient(api) as client:
        created = client.post(f'/api/roles/{service.role.id}/generations', json={
            'requestId': '26126126126126126126126126126126', 'expectedRevision': settings['revision'],
            'parameters': service.request, 'deliveryMode': 'history_only'})
        assert created.status_code == 200, created.text
        generation = created.json()['generation']
        if outcome == 'owner_changed_background':
            image = io.BytesIO()
            Image.new('RGB', (64, 64), '#ddcc22').save(image, format='PNG')
            chosen = service.roles.add_illustration(service.role.id, image.getvalue(), 'image/png', set_background=True)
            expected_background = chosen.chatBackgroundId
        if outcome == 'lost_local_ack':
            original = service.roles.add_illustration
            def write_then_lose_ack(*args, **kwargs):
                original(*args, **kwargs)
                raise OSError('controlled lost local acknowledgement')
            monkeypatch.setattr(service.roles, 'add_illustration', write_then_lose_ack)
        asyncio.run(app.recover())
        if outcome == 'lost_local_ack':
            assert app.owner_generation(service.role.id, generation['generationId'])['writebackStatus'] == 'uncertain'
            service.roles.set_chat_background(service.role.id, None)
            # Recovery reads the committed image; it does not reapply the old background.
        asyncio.run(app.recover())
        saved = service.roles.get(service.role.id)
        result = app.owner_generation(service.role.id, generation['generationId'])
        assert result['status'] == 'succeeded'
        assert len(saved.illustrations) == 1
        if outcome == 'owner_changed_background':
            assert result['writebackStatus'] == 'failed'
            assert saved.chatBackgroundId == expected_background
            assert app.ledger.usage(service.role.id)['roleAssetBytes'] == 0
        else:
            assert result['writebackStatus'] == 'applied'
            assert saved.chatBackgroundId == (None if outcome == 'lost_local_ack' else saved.illustrations[0].id)
            assert app.ledger.usage(service.role.id)['roleAssetBytes'] == saved.illustrations[0].size
            assert saved.cardImageUrl is None
        assert len(service.peer.requests) == 1
        assert result['attachments'] == generation['attachments']
        assert app.ledger.usage(service.role.id)['reservedMediaBytes'] == 0
