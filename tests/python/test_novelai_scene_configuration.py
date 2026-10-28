"""CG keeps an explicit role switch and auxiliary model, without version consent."""
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
import hashlib
import json


def test_owner_can_enable_cg_with_auxiliary_connection_without_version_confirmation(novelai_main):
    client, service = novelai_main
    connection = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'fixture',
        'baseUrl': 'http://127.0.0.1:9/v1', 'model': 'scene-fixture', 'api': 'openai-completions'})
    assert connection.status_code == 200, connection.text
    identifier = connection.json()['configuration']['id']
    body = {'name': service.role.name, 'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {
        'manualEnabled': False, 'sceneCgEnabled': True,
        'scenePromptModelConfigurationId': identifier,
        'consentRevision': 0}}}
    accepted = client.put(f'/api/roles/{service.role.id}', json=body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['role']['agentConfig']['novelai']['sceneCgEnabled'] is True
    assert not service.peer.requests
    body['agentConfig']['novelai']['scenePromptModelConfigurationId'] = None
    rejected = client.put(f'/api/roles/{service.role.id}', json=body)
    assert rejected.status_code == 422, rejected.text
    assert not service.peer.requests


def test_role_cg_prompt_instructions_persist_independently_with_a_strict_limit(novelai_main):
    client, service = novelai_main
    other = client.post('/api/roles', json={'name': '另一个角色', 'profile': {'profile': 'other'}}).json()['role']
    current = client.get(f'/api/roles/{service.role.id}').json()['role']
    agent_config = current['agentConfig']
    agent_config['novelai']['sceneCgPromptInstructions'] = '暖色调，远景构图'
    saved = client.put(f'/api/roles/{service.role.id}', json={
        'name': current['name'], 'profile': current['profile'], 'agentConfig': agent_config})
    assert saved.status_code == 200, saved.text
    assert saved.json()['role']['agentConfig']['novelai']['sceneCgPromptInstructions'] == '暖色调，远景构图'
    assert client.get(f"/api/roles/{other['id']}").json()['role']['agentConfig']['novelai']['sceneCgPromptInstructions'] == ''

    agent_config['novelai']['sceneCgPromptInstructions'] = 'x' * 2001
    rejected = client.put(f'/api/roles/{service.role.id}', json={
        'name': current['name'], 'profile': current['profile'], 'agentConfig': agent_config})
    assert rejected.status_code == 422

    agent_config['novelai']['sceneCgPromptInstructions'] = 123
    rejected = client.put(f'/api/roles/{service.role.id}', json={
        'name': current['name'], 'profile': current['profile'], 'agentConfig': agent_config})
    assert rejected.status_code == 422
    agent_config['novelai']['sceneCgPromptInstructions'] = ''
    cleared = client.put(f'/api/roles/{service.role.id}', json={
        'name': current['name'], 'profile': current['profile'], 'agentConfig': agent_config})
    assert cleared.status_code == 200, cleared.text
    assert client.get(f'/api/roles/{service.role.id}').json()['role']['agentConfig']['novelai']['sceneCgPromptInstructions'] == ''


def test_default_new_tag_permissions_do_not_revoke_saved_172_manual_request(novelai_main):
    client, service = novelai_main
    body = {'requestId': 'e' * 32, 'expectedRevision': service.application.settings.public()['revision'],
        'parameters': service.request}
    first = client.post(f'/api/roles/{service.role.id}/generations', json=body)
    assert first.status_code == 200, first.text
    saved = first.json()['generation']
    # Exact serialized permission contract used by #172, before default-off tag
    # fields existed. This is an on-disk compatibility fixture, not a new grant.
    legacy_policy = {'manualEnabled': True, 'manualDailyAttempts': 10, 'sceneCgEnabled': False,
        'sceneCgDailyAttempts': 3, 'scenePromptModelConfigurationId': None, 'autoWritebackRoleAssets': False,
        'consentRevision': body['expectedRevision']}
    legacy_revision = hashlib.sha256(json.dumps([service.role.createdAt.isoformat(), 0, legacy_policy],
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    with service.application.ledger.connect() as db:
        db.execute('UPDATE novelai_attempts SET role_revision=? WHERE attempt_id=?', (legacy_revision, saved['attemptId']))
    replay = client.post(f'/api/roles/{service.role.id}/generations', json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()['generation']['attemptId'] == saved['attemptId']
    assert len(service.peer.requests) == 1
