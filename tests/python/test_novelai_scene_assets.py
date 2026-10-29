"""Optional CG writeback changes only the current role's card image."""
from novelai_scene_fixtures import scene_main
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
from test_novelai_scene import owner_turn
import io
import time
import hashlib
import json
import pytest
from PIL import Image


@pytest.mark.parametrize('after_fault', ['recover', 'restart', 'owner_card', 'permission_aba', 'reservation'])
def test_asset_admission_failure_retains_its_own_duty_after_media_and_runtime_ack(scene_main, monkeypatch, after_fault):
    from backend.app import main
    from backend.app.novelai_generation.application import NovelaiApplication
    from backend.app.novelai_generation.context import GenerationContext
    client, service, model, _ = scene_main
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'autoWritebackRoleAssets': True}}})
    assert response.status_code == 200, response.text
    app = service.application
    with app.ledger.connect() as db:
        table = 'novelai_asset_usage' if after_fault == 'reservation' else 'novelai_asset_writes'
        db.execute(f"CREATE TRIGGER fail_asset_admission BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'controlled asset admission failure'); END")
    generation, _ = owner_turn(client, service, 'Keep the original permitted card duty through a local admission fault.')
    attempt_id = generation['attemptId']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        client.portal.call(app.recover)
        row = app.ledger.attempt(attempt_id)
        with app.ledger.connect() as db:
            outbox = dict(db.execute('SELECT * FROM novelai_media_outbox WHERE attempt_id=?', (attempt_id,)).fetchone())
        if outbox['state'] == 'sent' and outbox['message_synced'] and not row['runtime_ack_pending']:
            break
        time.sleep(.02)
    assert outbox['state'] == 'sent' and outbox['message_synced'] and row['runtime_ack_pending'] == 0
    assert attempt_id in {item['attempt_id'] for item in app.ledger.recovery_batch()}, 'asset responsibility disappeared after media/Runtime ACK'
    assert row['asset_writeback_pending'] == 1
    with app.ledger.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM novelai_asset_writes WHERE attempt_id=?', (attempt_id,)).fetchone()[0] == 0
    assert app.ledger.usage(service.role.id)['reservedMediaBytes'] == 0
    origin = GenerationContext.from_dict(json.loads(row['origin'])).origin
    service.tasks.retention_seconds = 0
    with pytest.raises(KeyError):
        service.tasks.query(generation['taskId'], origin)
    chosen = None
    if after_fault == 'restart':
        client.portal.call(app.close)
        app = NovelaiApplication(root=app.root, tasks=service.tasks, sessions=service.sessions,
            roles=service.roles, channels=app.channels, credentials=app.settings.credentials,
            http=app.http, lock_for=app.lock_for, model_configuration=app.model_configuration, model_adapter=app.model_adapter)
        service.application = app
        monkeypatch.setattr(main, '_novelai', app)
        assert attempt_id in {item['attempt_id'] for item in app.ledger.recovery_batch()}
        client.portal.call(app.start)
    elif after_fault == 'owner_card':
        selected = io.BytesIO()
        Image.new('RGB', (64, 64), '#ddee33').save(selected, format='PNG')
        chosen = selected.getvalue()
        response = client.post(f'/api/roles/{service.role.id}/card-image', content=chosen, headers={'Content-Type': 'image/png'})
        assert response.status_code == 200, response.text
    elif after_fault == 'permission_aba':
        for enabled in (False, True):
            response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
                'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'autoWritebackRoleAssets': enabled}}})
            assert response.status_code == 200, response.text
    with app.ledger.connect() as db:
        db.execute('DROP TRIGGER fail_asset_admission')
    expected = 'failed' if after_fault in {'owner_card', 'permission_aba'} else 'applied'
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        client.portal.call(app.recover)
        page = client.get(f'/api/roles/{service.role.id}/generations').json()
        actual = next(item for item in page['generations'] if item['attemptId'] == attempt_id)
        if actual['writebackStatus'] == expected:
            break
        time.sleep(.02)
    assert actual['writebackStatus'] == expected, page
    assert actual['status'] == 'succeeded' and actual['deliveryStatus'] == 'sent'
    assert app.ledger.attempt(attempt_id)['asset_writeback_pending'] == 0
    assert attempt_id not in {item['attempt_id'] for item in app.ledger.recovery_batch()}
    assert page['usage']['reservedMediaBytes'] == 0
    if expected == 'applied':
        image = client.get(f'/api/roles/{service.role.id}/card-image')
        generated = client.get(f'/api/roles/{service.role.id}/attachments/{generation["attachments"][0]["id"]}')
        assert image.status_code == 200 and image.content == generated.content
        assert page['usage']['roleAssetBytes'] == len(image.content)
    else:
        assert page['usage']['roleAssetBytes'] == 0
        if chosen is None:
            assert service.roles.get(service.role.id).cardImageUrl is None
        else:
            assert client.get(f'/api/roles/{service.role.id}/card-image').content == chosen
    assert len(service.peer.requests) == 1
    assert len([request for request in model.requests if request['model'] == 'fixture-scene']) == 2
    assert len([message for message in service.sessions.list_messages(origin.session_key) if message.messageType == 'internal_completion']) == 1


@pytest.mark.parametrize('result', ['not_permitted', 'no_image', 'aux_failed'])
def test_cg_without_a_permitted_card_result_has_no_pending_writeback_duty(scene_main, result):
    client, service, model, _ = scene_main
    app = service.application
    if result != 'not_permitted':
        response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
            'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'autoWritebackRoleAssets': True}}})
        assert response.status_code == 200, response.text
        model.observation.update(transition='none', scene_key='', visual_key='', visual_description='', prompt='', negative_prompt='')
        if result == 'aux_failed':
            model.observation['forbidden_field'] = 'not a valid observation'
    generation, _ = owner_turn(client, service, 'Only the originally permitted successful card result may be written.')
    attempt_id = generation['attemptId']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        client.portal.call(app.recover)
        row = app.ledger.attempt(attempt_id)
        if not row['runtime_ack_pending']:
            break
        time.sleep(.02)
    assert row['runtime_ack_pending'] == 0 and row['asset_writeback_pending'] == 0
    assert attempt_id not in {item['attempt_id'] for item in app.ledger.recovery_batch()}
    with app.ledger.connect() as db:
        assert not db.execute('SELECT 1 FROM novelai_asset_writes WHERE attempt_id=?', (attempt_id,)).fetchone()
    assert client.get(f'/api/roles/{service.role.id}/generations').json()['generations'][0]['writebackStatus'] == 'not_requested'
    if result == 'not_permitted':
        response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
            'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'autoWritebackRoleAssets': True}}})
        assert response.status_code == 200, response.text
        client.portal.call(app.recover)
        assert client.get(f'/api/roles/{service.role.id}/generations').json()['generations'][0]['writebackStatus'] == 'not_requested'
    assert app.ledger.usage(service.role.id)['reservedMediaBytes'] == 0
    assert service.roles.get(service.role.id).cardImageUrl is None
    assert len(service.peer.requests) == (1 if result == 'not_permitted' else 0)
    assert len([request for request in model.requests if request['model'] == 'fixture-scene']) == (2 if result == 'not_permitted' else 1)


def test_explicit_asset_consent_sets_only_current_card_and_accounts_for_its_extra_copy(scene_main):
    client, service, _, _ = scene_main
    before = service.roles.get(service.role.id)
    response = client.put(f'/api/roles/{service.role.id}', json={'name': before.name, 'profile': before.profile.model_dump(),
        'agentConfig': {'novelai': {'autoWritebackRoleAssets': True}}})
    assert response.status_code == 200, response.text
    generation, _ = owner_turn(client, service, 'Enter the garden and keep its CG as the role card.')
    updated = service.roles.get(service.role.id)
    assert updated.cardImageUrl and updated.cardImageMediaType == 'image/png'
    assert updated.avatarUrl == before.avatarUrl and updated.avatarOriginalUrl == before.avatarOriginalUrl
    image = client.get(f'/api/roles/{service.role.id}/card-image')
    generated = client.get(f'/api/roles/{service.role.id}/attachments/{generation["attachments"][0]["id"]}')
    assert image.status_code == 200 and image.content == generated.content
    history = client.get(f'/api/roles/{service.role.id}/generations').json()
    assert history['usage']['roleAssetBytes'] == len(image.content)
    assert history['usage']['roleMediaBytes'] == 2 * len(image.content)
    assert len(service.peer.requests) == 1


def test_extra_card_copy_is_refused_at_its_own_quota_without_losing_the_generated_history(scene_main, monkeypatch):
    import zipfile
    from backend.app.novelai_generation import assets
    client, service, _, _ = scene_main
    with zipfile.ZipFile(io.BytesIO(service.peer.body)) as archive:
        size = len(archive.read('image.png'))
    # Admission keeps the real 96MiB reservation. Only the additional card-copy
    # boundary is made small, so the finished attachment fits and the copy does not.
    monkeypatch.setattr(assets, 'ROLE_MEDIA_BYTES', 2 * size - 1)
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'autoWritebackRoleAssets': True}}})
    assert response.status_code == 200, response.text
    generation, _ = owner_turn(client, service, 'Keep the image when its additional card copy exceeds quota.')
    page = client.get(f'/api/roles/{service.role.id}/generations').json()
    assert generation['status'] == 'succeeded' and page['generations'][0]['deliveryStatus'] == 'sent'
    assert page['generations'][0]['writebackStatus'] == 'failed'
    assert page['usage']['roleMediaBytes'] == size and page['usage']['roleAssetBytes'] == 0
    assert page['usage']['reservedMediaBytes'] == 0 and service.roles.get(service.role.id).cardImageUrl is None
    assert len(service.peer.requests) == 1


def test_local_recovery_restores_previous_card_when_a_crash_preceded_manifest_commit(novelai_service, monkeypatch):
    import os
    from pathlib import Path
    from backend.app.role_store import RoleStore
    service = novelai_service
    original_image, next_image = io.BytesIO(), io.BytesIO()
    Image.new('RGB', (64, 64), '#112233').save(original_image, format='PNG')
    Image.new('RGB', (64, 64), '#778899').save(next_image, format='PNG')
    before = service.roles.set_avatar(service.role.id, original_image.getvalue(), 'image/png', card=True)
    replace = os.replace
    class SimulatedCrash(RuntimeError): pass
    def crash_before_manifest(source, destination, *args, **kwargs):
        if Path(destination) == service.roles.manifest_path:
            raise SimulatedCrash('controlled process-loss boundary before role manifest commit')
        return replace(source, destination, *args, **kwargs)
    with monkeypatch.context() as cut:
        cut.setattr(os, 'replace', crash_before_manifest)
        with pytest.raises(SimulatedCrash):
            service.roles.set_avatar(service.role.id, next_image.getvalue(), 'image/png', card=True,
                expected_card_url=before.cardImageUrl, authorized=lambda: True, operation_id='a' * 32)
    reopened = RoleStore(service.roles.root)
    assert reopened.get(service.role.id).cardImageUrl == before.cardImageUrl
    result = reopened.reconcile_generated_card(service.role.id, operation_id='a' * 32,
        expected_card_url=before.cardImageUrl, digest=hashlib.sha256(next_image.getvalue()).hexdigest(), size=len(next_image.getvalue()))
    assert result is False
    assert (reopened.avatar_path(service.role.id).parent / 'card-image').read_bytes() == original_image.getvalue()
    assert not list((service.roles.root / service.role.id).glob('.avatar-*.bak'))
    assert not service.peer.requests


def test_owner_card_change_during_auxiliary_request_wins_over_automatic_writeback(scene_main):
    client, service, peer, _ = scene_main
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'autoWritebackRoleAssets': True}}})
    assert response.status_code == 200, response.text
    peer.aux_block = True
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'A garden image is pending.'})
    assert 'assistant_completed' in response.text and peer.aux_accepted.wait(2)
    image = io.BytesIO()
    Image.new('RGB', (64, 64), '#cc8811').save(image, format='PNG')
    uploaded = client.post(f'/api/roles/{service.role.id}/card-image', content=image.getvalue(), headers={'Content-Type': 'image/png'})
    assert uploaded.status_code == 200, uploaded.text
    chosen_url = uploaded.json()['role']['cardImageUrl']
    peer.aux_release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        page = client.get(f'/api/roles/{service.role.id}/generations').json()
        if page['generations'] and page['generations'][0]['writebackStatus'] == 'failed':
            break
        time.sleep(.02)
    assert page['generations'][0]['writebackStatus'] == 'failed', page
    assert service.roles.get(service.role.id).cardImageUrl == chosen_url
    assert client.get(f'/api/roles/{service.role.id}/card-image').content == image.getvalue()
    assert page['usage']['roleAssetBytes'] == 0 and page['usage']['reservedMediaBytes'] == 0
    assert page['generations'][0]['deliveryStatus'] == 'sent' and len(service.peer.requests) == 1


def test_asset_manifest_failure_rolls_back_but_preserves_normal_history_without_regeneration(scene_main, monkeypatch):
    import os
    from pathlib import Path
    client, service, _, _ = scene_main
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'autoWritebackRoleAssets': True}}})
    assert response.status_code == 200, response.text
    original = os.replace
    failures = []
    def fail_once(source, destination, *args, **kwargs):
        if Path(destination) == service.roles.manifest_path and not failures:
            failures.append(str(destination))
            raise OSError('controlled role manifest filesystem failure')
        return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'replace', fail_once)
    result, _ = owner_turn(client, service, 'Keep the generation even if writing the role card fails.')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        page = client.get(f'/api/roles/{service.role.id}/generations').json()
        if page['generations'][0]['writebackStatus'] == 'failed':
            break
        time.sleep(.02)
    assert failures and page['generations'][0]['writebackStatus'] == 'failed', page
    assert result['status'] == 'succeeded' and page['generations'][0]['deliveryStatus'] == 'sent'
    assert service.roles.get(service.role.id).cardImageUrl is None
    assert page['usage']['roleAssetBytes'] == 0 and page['usage']['reservedMediaBytes'] == 0
    assert len(service.peer.requests) == 1


def test_asset_receipt_sqlite_failure_recovers_after_host_rebuild_without_copy_or_generation_again(scene_main, monkeypatch):
    from backend.app import main
    from backend.app.novelai_generation.application import NovelaiApplication
    client, service, peer, _ = scene_main
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'autoWritebackRoleAssets': True}}})
    assert response.status_code == 200, response.text
    old = service.application
    with old.ledger.connect() as db:
        db.execute("CREATE TRIGGER fail_asset_receipt BEFORE UPDATE OF status ON novelai_asset_writes WHEN NEW.status='applied' BEGIN SELECT RAISE(ABORT,'controlled asset receipt failure'); END")
    generation, _ = owner_turn(client, service, 'A card image whose local receipt cannot commit yet.')
    original_url = service.roles.get(service.role.id).cardImageUrl
    assert original_url
    page = client.get(f'/api/roles/{service.role.id}/generations').json()
    assert page['generations'][0]['writebackStatus'] in {'writing', 'uncertain'}
    assert page['usage']['reservedMediaBytes'] == generation['attachments'][0]['size']
    client.portal.call(old.close)
    reopened = NovelaiApplication(root=old.root, tasks=service.tasks, sessions=service.sessions,
        roles=service.roles, channels=old.channels, credentials=old.settings.credentials,
        http=old.http, lock_for=old.lock_for, model_configuration=old.model_configuration, model_adapter=old.model_adapter)
    service.application = reopened
    monkeypatch.setattr(main, '_novelai', reopened)
    with reopened.ledger.connect() as db:
        db.execute('DROP TRIGGER fail_asset_receipt')
    client.portal.call(reopened.start)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        page = client.get(f'/api/roles/{service.role.id}/generations').json()
        if page['generations'][0]['writebackStatus'] == 'applied':
            break
        time.sleep(.02)
    assert page['generations'][0]['writebackStatus'] == 'applied', page
    assert service.roles.get(service.role.id).cardImageUrl == original_url
    assert page['usage']['roleAssetBytes'] == generation['attachments'][0]['size']
    assert page['usage']['reservedMediaBytes'] == 0
    assert len(service.peer.requests) == 1
    assert len([request for request in peer.requests if request['model'] == 'fixture-scene']) == 2
