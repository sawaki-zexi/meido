"""A completed real owner turn produces one bounded scene observation and image."""
import json
import time
import pytest

from novelai_scene_fixtures import scene_main, hold_candidate_writer
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def owner_turn(client, service, text):
    before = client.get(f'/api/roles/{service.role.id}/generations').json()['generations']
    old_ids = {item['attemptId'] for item in before}
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': text})
    assert 'assistant_completed' in response.text, response.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        page = client.get(f'/api/roles/{service.role.id}/generations').json()
        result = next((item for item in page['generations'] if item['attemptId'] not in old_ids), None)
        if result and (result['deliveryStatus'] == 'sent' or result['status'] in {'completed_without_image', 'failed_response', 'cancelled_before_dispatch'}):
            return result, page
        time.sleep(.02)
    raise AssertionError(page)


def test_completed_owner_turn_observes_once_and_delivers_one_original_image(scene_main):
    client, service, peer, _ = scene_main
    peer.observation.update(prompt='blue sky, CINEMATIC COMPOSITION', negative_prompt='rain, selfie')
    current = client.get(f'/api/roles/{service.role.id}').json()['role']
    current['agentConfig']['novelai']['sceneCgPromptInstructions'] = 'Use warm evening light and a distant composition.'
    saved = client.put(f"/api/roles/{service.role.id}", json={
        'name': current['name'], 'profile': current['profile'], 'agentConfig': current['agentConfig']})
    assert saved.status_code == 200, saved.text
    service.peer.block = True
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'Let us enter the sunny garden.'})
    assert 'assistant_completed' in response.text, response.text
    assert peer.aux_accepted.wait(2), 'completed owner turn never reached its explicit auxiliary model'
    assert service.peer.accepted.wait(2)
    rows = service.sessions.list_messages(f'role:{service.role.id}')
    final = rows[-1]
    assert service.sessions.get_run(final.runId).status == 'completed'
    assert final.content == 'We enter the sunny garden.'
    aux = [request for request in peer.requests if request['model'] == 'fixture-scene']
    assert len(aux) == 1 and aux[0]['max_tokens'] == 600
    assert 'Use warm evening light and a distant composition.' in aux[0]['messages'][0]['content']
    assert 'Use warm evening light and a distant composition.' not in aux[0]['messages'][1]['content']
    assert not aux[0].get('tools')
    service.peer.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        history = client.get(f'/api/roles/{service.role.id}/generations').json()
        if history['generations'] and history['generations'][0]['deliveryStatus'] == 'sent':
            break
        time.sleep(.02)
    assert history['generations'][0]['status'] == 'succeeded', history
    assert history['generations'][0]['purpose'] == 'scene_cg'
    assert history['usage']['cgAttempts'] == 1 and history['usage']['manualAttempts'] == 0
    assert len(service.peer.requests) == 1
    sent = service.peer.requests[0]['body']['parameters']
    expected_prompt = 'blue sky, CINEMATIC COMPOSITION, third-person view, character visible in frame'
    expected_negative = 'rain, selfie, first-person view, pov'
    assert service.peer.requests[0]['body']['input'] == expected_prompt
    assert sent['v4_prompt']['caption']['base_caption'] == expected_prompt
    assert sent['negative_prompt'] == expected_negative
    assert sent['v4_negative_prompt']['caption']['base_caption'] == expected_negative
    normalized_prompt = sent['v4_prompt']['caption']['base_caption'].casefold()
    normalized_negative = sent['v4_negative_prompt']['caption']['base_caption'].casefold()
    assert all(normalized_prompt.count(term) == 1
        for term in ('third-person view', 'cinematic composition', 'character visible in frame'))
    assert all(normalized_negative.count(term) == 1
        for term in ('first-person view', 'pov', 'selfie'))
    rows = service.sessions.list_messages(f'role:{service.role.id}')
    assert len([row for row in rows if row.role == 'user']) == 1
    assert rows[-1].messageType == 'internal_completion' and rows[-1].runId is None
    saved_parameters = history['generations'][0]['parameters']
    current = client.get(f'/api/roles/{service.role.id}').json()['role']
    current['agentConfig']['novelai']['sceneCgPromptInstructions'] = 'A later preference for future CG only.'
    changed = client.put(f"/api/roles/{service.role.id}", json={
        'name': current['name'], 'profile': current['profile'], 'agentConfig': current['agentConfig']})
    assert changed.status_code == 200, changed.text
    reloaded = client.get(f'/api/roles/{service.role.id}/generations').json()['generations'][0]
    assert reloaded['parameters'] == saved_parameters
    peer.observation.update(transition='changed', scene_key='park', visual_key='park-evening')
    next_generation, _ = owner_turn(client, service, 'We have moved to the park.')
    assert next_generation['status'] == 'succeeded'
    auxiliary = [request for request in peer.requests if request['model'] == 'fixture-scene']
    assert len(auxiliary) == 2
    assert 'A later preference for future CG only.' in auxiliary[1]['messages'][0]['content']
    assert 'A later preference for future CG only.' not in auxiliary[1]['messages'][1]['content']


def test_scene_candidate_freezes_quality_preferences_before_auxiliary_model_returns(scene_main):
    client, service, peer, _ = scene_main
    public = service.application.settings.public()
    updated = service.application.settings.save({'addQualityTags': True, 'undesiredContentPreset': 2},
        expected_revision=public['revision'])
    role = service.roles.get(service.role.id)
    response = client.put(f'/api/roles/{service.role.id}', json={'name': role.name,
        'profile': role.profile.model_dump(), 'agentConfig': {'novelai': {'consentRevision': updated['revision']}}})
    assert response.status_code == 200, response.text
    peer.aux_block = True
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'Freeze the current image preferences.'})
    assert 'assistant_completed' in response.text, response.text
    assert peer.aux_accepted.wait(2), 'completed owner turn never reached its explicit auxiliary model'
    with service.application.ledger.connect() as connection:
        row = connection.execute('SELECT parameters FROM novelai_attempts WHERE purpose=? ORDER BY created_at DESC LIMIT 1',
            ('scene_cg',)).fetchone()
    parameters = json.loads(row['parameters'])
    assert parameters['qualityToggle'] is True and parameters['ucPreset'] == 2
    peer.aux_release.set()


def test_scene_cg_explicit_tags_require_both_service_nsfw_and_role_grant(scene_main):
    client, service, peer, _ = scene_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    saved = client.post(path, json={'name': 'Adult scene', 'keywords': ['garden', 'beach'],
        'prompt': 'explicit detail', 'negative_prompt': '', 'rating': 'explicit', 'enabled': True})
    assert saved.status_code == 200, saved.text
    role = service.roles.get(service.role.id)
    updated = client.put(f'/api/roles/{service.role.id}', json={'name': role.name,
        'profile': role.profile.model_dump(), 'agentConfig': {'novelai': {
            'promptTagsEnabled': True, 'allowedPromptTagRatings': ['general', 'explicit'],
            'sceneCgEnabled': True, 'scenePromptModelConfigurationId': service.application.roles.get(service.role.id).agentConfig.novelai.scenePromptModelConfigurationId,
            'consentRevision': service.application.settings.public()['revision']}}})
    assert updated.status_code == 200, updated.text

    generation, _ = owner_turn(client, service, 'The owner enters the garden.')
    assert generation['status'] == 'succeeded'
    assert 'explicit detail' not in service.peer.requests[0]['body']['input']

    # A fresh service revision and role consent are required before enabling
    # the global NSFW gate for the second independent owner turn.
    public = service.application.settings.public()
    enabled = service.application.settings.save({
        'allowedModels': ['nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full'],
        'nsfwEnabled': True,
    }, expected_revision=public['revision'])
    role = service.roles.get(service.role.id)
    updated = client.put(f'/api/roles/{service.role.id}', json={'name': role.name,
        'profile': role.profile.model_dump(), 'agentConfig': {'novelai': {
            'sceneCgEnabled': True, 'scenePromptModelConfigurationId': role.agentConfig.novelai.scenePromptModelConfigurationId,
            'promptTagsEnabled': True, 'allowedPromptTagRatings': ['general', 'explicit'],
            'consentRevision': enabled['revision']}}})
    assert updated.status_code == 200, updated.text
    peer.observation.update({'transition': 'changed', 'scene_key': 'beach', 'visual_key': 'beach-day',
        'visual_description': 'A sunny beach', 'prompt': 'blue sky, beach'})
    generation, _ = owner_turn(client, service, 'The owner walks to the beach.')
    assert generation['status'] == 'succeeded'
    assert 'explicit detail' in service.peer.requests[-1]['body']['input']


@pytest.mark.parametrize('result', ['none', 'malformed'])
def test_auxiliary_without_generation_still_consumes_cg_and_account_attempt(scene_main, result):
    client, service, peer, _ = scene_main
    peer.observation.update(transition='none', scene_key='', visual_key='', visual_description='', prompt='', negative_prompt='')
    if result == 'malformed':
        peer.observation['identity'] = 'owner'
    generation, history = owner_turn(client, service, 'No scene picture is required.')
    assert generation['status'] == ('completed_without_image' if result == 'none' else 'failed_response')
    assert generation['auxiliaryConsumption'] == 'possible' and generation['consumption'] == 'not_dispatched'
    assert history['usage']['accountAttempts'] == 1 and history['usage']['cgAttempts'] == 1
    assert history['usage']['manualAttempts'] == 0 and not service.peer.requests
    assert len([request for request in peer.requests if request['model'] == 'fixture-scene']) == 1


def test_finite_cg_allowance_also_limits_no_image_observations(scene_main):
    client, service, peer, _ = scene_main
    peer.observation.update(transition='none', scene_key='', visual_key='', visual_description='', prompt='', negative_prompt='')
    for number in range(3):
        _, history = owner_turn(client, service, f'An ordinary owner turn {number}.')
        assert history['usage']['cgAttempts'] == number + 1
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'No fourth auxiliary request.'})
    assert 'assistant_completed' in response.text
    # Querying the original facts cannot turn a refused candidate into another attempt.
    for _ in range(3):
        history = client.get(f'/api/roles/{service.role.id}/generations').json()
    assert len(history['generations']) == 3 and history['usage']['cgAttempts'] == 3
    assert len([request for request in peer.requests if request['model'] == 'fixture-scene']) == 3
    assert not service.peer.requests


def test_legacy_model_save_revokes_an_inflight_explicit_auxiliary_binding(scene_main):
    client, service, peer, connections = scene_main
    peer.aux_block = True
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'An old auxiliary request is in flight.'})
    assert 'assistant_completed' in response.text and peer.aux_accepted.wait(2)
    activated = client.post('/api/model/configurations/' + connections[1] + '/activate')
    assert activated.status_code == 200, activated.text
    changed = client.put('/api/model/configuration', json={'providerId': 'custom', 'provider': 'fixture',
        'baseUrl': next(request for request in peer.requests if request['model'] == 'fixture-scene').get('baseUrl',
            client.get('/api/model/configurations').json()['configurations'][1]['baseUrl']),
        'model': 'changed-scene-model', 'api': 'openai-completions'})
    assert changed.status_code == 200, changed.text
    peer.aux_release.set()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        history = client.get(f'/api/roles/{service.role.id}/generations').json()
        if history['generations'] and history['generations'][0]['status'] not in {'prepared', 'dispatching'}:
            break
        time.sleep(.02)
    assert not service.peer.requests, history
    assert history['usage']['cgAttempts'] == 1


def test_cg_permission_off_on_does_not_revive_an_inflight_auxiliary_result(scene_main):
    client, service, peer, _ = scene_main
    peer.aux_block = True
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'An old CG permission is in flight.'})
    assert 'assistant_completed' in response.text and peer.aux_accepted.wait(2)
    for enabled in (False, True):
        changed = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
            'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'sceneCgEnabled': enabled}}})
        assert changed.status_code == 200, changed.text
    peer.aux_release.set()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        history = client.get(f'/api/roles/{service.role.id}/generations').json()
        if history['generations'] and history['generations'][0]['status'] not in {'prepared', 'dispatching'}:
            break
        time.sleep(.02)
    assert not service.peer.requests, history
    assert history['usage']['cgAttempts'] == 1 and history['usage']['accountAttempts'] == 1
    assert not [message for message in service.sessions.list_messages(f'role:{service.role.id}')
        if message.messageType == 'internal_completion']


@pytest.mark.parametrize('change', ['update_aba', 'delete'])
def test_explicit_auxiliary_model_aba_or_deletion_does_not_revive_old_result(scene_main, change):
    client, service, peer, connections = scene_main
    peer.aux_block = True
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'An observation using the previous model epoch.'})
    assert 'assistant_completed' in response.text and peer.aux_accepted.wait(2)
    if change == 'delete':
        changed = client.delete('/api/model/configurations/' + connections[1])
        assert changed.status_code == 200, changed.text
    else:
        current = next(item for item in client.get('/api/model/configurations').json()['configurations']
            if item['id'] == connections[1])
        for model in ('replacement-scene', 'fixture-scene'):
            changed = client.put('/api/model/configurations/' + connections[1], json={'providerId': 'custom',
                'provider': 'fixture', 'baseUrl': current['baseUrl'], 'model': model, 'api': 'openai-completions'})
            assert changed.status_code == 200, changed.text
    peer.aux_release.set()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        history = client.get(f'/api/roles/{service.role.id}/generations').json()
        if history['generations'] and history['generations'][0]['status'] not in {'prepared', 'dispatching'}:
            break
        time.sleep(.02)
    assert not service.peer.requests, history
    assert history['usage']['cgAttempts'] == 1 and history['usage']['accountAttempts'] == 1
    assert len([request for request in peer.requests if request['model'] == 'fixture-scene']) == 1


def test_optional_cg_candidate_sqlite_failure_preserves_the_completed_chat_event(scene_main, monkeypatch):
    client, service, peer, _ = scene_main
    observed = hold_candidate_writer(service, monkeypatch)
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'Keep the real reply when optional CG storage is busy.'})
    assert observed == ['held_real_sqlite_writer']
    assert 'assistant_completed' in response.text and 'assistant_failed' not in response.text, response.text
    rows = service.sessions.list_messages(f'role:{service.role.id}')
    reply = next(item for item in reversed(rows) if item.role == 'assistant' and item.messageType == 'text')
    assert reply.status == 'completed' and service.sessions.get_run(reply.runId).status == 'completed'
    assert not [item for item in peer.requests if item['model'] == 'fixture-scene']
    assert not service.peer.requests
    assert not client.get(f'/api/roles/{service.role.id}/generations').json()['generations']
