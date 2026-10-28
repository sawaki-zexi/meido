"""Owner-visible readiness follows plugin configuration, without a second consent gate."""
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
import pytest


@pytest.mark.parametrize('field', ['manualEnabled', 'confirmManualConsumption'])
def test_legacy_boolean_fields_still_reject_null(novelai_main, field):
    client, _ = novelai_main
    current = client.get('/api/services/novelai').json()
    response = client.put('/api/services/novelai', json={field: None, 'expectedRevision': current['revision']})
    assert response.status_code == 422
    assert client.get('/api/services/novelai').json() == current


def test_enabled_configured_plugin_generates_without_version_confirmation(novelai_main):
    client, service = novelai_main
    current = client.get('/api/services/novelai').json()
    # Reproduce the reported state: plugin and token configured, but no current
    # version confirmation. A fresh role has no legacy per-role manual grant.
    saved = client.put('/api/services/novelai', json={
        'enabled': True, 'confirmManualConsumption': False,
        'maxSteps': 27, 'expectedRevision': current['revision']})
    assert saved.status_code == 200, saved.text
    role = client.post('/api/roles', json={'name': 'Ready owner role',
        'profile': {'profile': 'A test role'}}).json()['role']
    tool = next(item for item in client.get(f"/api/roles/{role['id']}/tools").json()['tools']
                if item['name'] == 'generate_image')
    assert tool['modelVisible'], tool['reason']
    response = client.post(f"/api/roles/{role['id']}/generations", json={
        'requestId': 'a' * 32, 'expectedRevision': saved.json()['revision'],
        'parameters': service.request})
    assert response.status_code == 200, response.text
    assert response.json()['generation']['status'] == 'succeeded'
    assert len(service.peer.requests) == 1


def test_plugin_switch_controls_manual_generation_and_legacy_pause_still_works(novelai_main):
    client, service = novelai_main
    current = client.get('/api/services/novelai').json()
    paused = client.put('/api/services/novelai', json={
        'manualEnabled': False, 'expectedRevision': current['revision']}).json()
    assert paused['enabled'] is False
    enabled = client.put('/api/services/novelai', json={
        'enabled': True, 'expectedRevision': paused['revision']}).json()
    assert enabled['manualEnabled'] is True  # Old clients receive the effective switch.
    tool = next(item for item in client.get(f'/api/roles/{service.role.id}/tools').json()['tools']
                if item['name'] == 'generate_image')
    assert tool['modelVisible'], tool['reason']
    assert not service.peer.requests


@pytest.mark.parametrize('status,fragment', [(401, 'Token'), (402, 'Anlas'), (429, '频繁'), (503, '暂不可用')])
def test_generation_failures_explain_the_cause_without_exposing_provider_body(novelai_main, status, fragment):
    client, service = novelai_main
    service.peer.status = status
    service.peer.body = b'provider-body-with-secret-and-private-prompt'
    response = client.post(f'/api/roles/{service.role.id}/generations', json={
        'requestId': 'c' * 32, 'expectedRevision': client.get('/api/services/novelai').json()['revision'],
        'parameters': service.request})
    assert response.status_code == 200, response.text
    generation = response.json()['generation']
    assert generation['errorCode'] == f'http_{status}'
    assert fragment in generation['errorMessage']
    assert 'provider-body' not in response.text
    original = client.get(f"/api/roles/{service.role.id}/generations/{generation['generationId']}?attemptId={generation['attemptId']}")
    assert original.json()['generation']['errorMessage'] == generation['errorMessage']
    assert len(service.peer.requests) == 1


def test_exhausted_budget_keeps_tool_visible_and_allows_original_request_lookup(novelai_main):
    client, service = novelai_main
    current = client.get('/api/services/novelai').json()
    saved = client.put('/api/services/novelai', json={'dailyAttemptLimit': 1, 'expectedRevision': current['revision']}).json()
    body = {'requestId': 'd' * 32, 'expectedRevision': saved['revision'], 'parameters': service.request}
    path = f'/api/roles/{service.role.id}/generations'
    first = client.post(path, json=body)
    assert first.status_code == 200, first.text
    tool = next(item for item in client.get(f'/api/roles/{service.role.id}/tools').json()['tools'] if item['name'] == 'generate_image')
    assert tool['modelVisible'], tool['reason']
    denied = client.post(path, json={**body, 'requestId': 'e' * 32})
    assert denied.status_code == 429
    assert '次数' in denied.json()['detail']
    assert client.post(path, json=body).json()['generation']['attemptId'] == first.json()['generation']['attemptId']
    assert len(service.peer.requests) == 1


@pytest.mark.parametrize('condition,code', [('token', 'credential_missing'), ('modes', 'generation_modes_disabled')])
def test_unready_plugin_stays_discoverable_and_explains_the_block_without_dispatch(novelai_main, condition, code):
    client, service = novelai_main
    if condition == 'token':
        changed = client.delete('/api/services/novelai/token')
    else:
        current = client.get('/api/services/novelai').json()
        changed = client.put('/api/services/novelai', json={'allowTxt2Img': False,
            'allowImg2Img': False, 'expectedRevision': current['revision']})
    assert changed.status_code == 200
    tool = next(item for item in client.get(f'/api/roles/{service.role.id}/tools').json()['tools']
                if item['name'] == 'generate_image')
    assert tool['modelVisible'], tool['reason']
    availability = client.get('/api/services/novelai').json()['availability']
    assert availability['code'] == code and not availability['available']
    rejected = client.post(f'/api/roles/{service.role.id}/generations', json={
        'requestId': 'b' * 32, 'expectedRevision': changed.json()['revision'],
        'parameters': service.request})
    assert rejected.status_code == 403
    assert rejected.json()['detail'] == availability['message']
    assert not service.peer.requests
