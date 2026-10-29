"""Auxiliary protocol failures never invoke tools or repeat either paid request."""
import time

import pytest

from novelai_scene_fixtures import scene_main
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
from test_novelai_scene import owner_turn


@pytest.mark.parametrize('fault', ['http_503', 'tool_call', 'duplicate_json', 'oversized', 'non_ascii', 'timeout', 'truncated', 'invalid_prompt'])
def test_auxiliary_failure_keeps_attempt_and_never_dispatches_generation(scene_main, monkeypatch, fault):
    from backend.app.novelai_generation import scene
    client, service, peer, _ = scene_main
    if fault == 'http_503': peer.aux_status = 503
    elif fault == 'tool_call': peer.aux_tool = True
    elif fault == 'duplicate_json': peer.aux_text = '{"transition":"none","transition":"started"}'
    elif fault == 'oversized': peer.aux_text = 'x' * 17000
    elif fault == 'non_ascii': peer.observation['prompt'] = '花园'
    elif fault == 'truncated': peer.finish_reason = 'length'
    elif fault == 'invalid_prompt': peer.prompt_text = '{"prompt":"missing required fields"}'
    elif fault == 'timeout':
        monkeypatch.setattr(scene, 'AUXILIARY_TIMEOUT_SECONDS', 2.0)
        peer.aux_block = True
    try:
        result, page = owner_turn(client, service, 'A real owner turn, with an invalid auxiliary response.')
        assert result['status'] == 'failed_response' and result['auxiliaryStatus'] == 'failed'
        assert result['errorCode'] == 'scene_observation_failed' and result['auxiliaryConsumption'] == 'possible'
        assert len([request for request in peer.requests if request['model'] == 'fixture-scene']) == (2 if fault in {'non_ascii', 'invalid_prompt'} else 1)
        assert not service.peer.requests
        assert page['usage']['accountAttempts'] == page['usage']['cgAttempts'] == 1
        assert page['usage']['manualAttempts'] == 0
    finally:
        peer.aux_release.set()


def test_disabling_cg_during_prompt_conversion_never_dispatches_image(scene_main):
    client, service, peer, _ = scene_main
    peer.prompt_block = True
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'Enter the garden.'})
    assert 'assistant_completed' in response.text and peer.prompt_accepted.wait(3)
    changed = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'sceneCgEnabled': False}}})
    assert changed.status_code == 200, changed.text
    peer.aux_release.set()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        page = client.get(f'/api/roles/{service.role.id}/generations').json()
        if page['generations'] and page['generations'][0]['status'] not in {'prepared', 'dispatching'}:
            break
        time.sleep(.02)
    assert not service.peer.requests
    assert page['generations'][0]['status'] in {'cancelled_before_dispatch', 'failed_response'}


def test_disabled_txt2img_mode_blocks_scene_before_auxiliary_consumption(scene_main):
    client, service, peer, _ = scene_main
    public = service.application.settings.public()
    disabled = client.put('/api/services/novelai', json={'allowTxt2Img': False, 'expectedRevision': public['revision']})
    assert disabled.status_code == 200, disabled.text
    assert service.roles.get(service.role.id).agentConfig.novelai.sceneCgEnabled

    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'Do not start a CG while txt2img is disabled.'})
    assert 'assistant_completed' in response.text, response.text
    time.sleep(.2)
    assert not [request for request in peer.requests if request['model'] == 'fixture-scene']
    assert client.get(f'/api/roles/{service.role.id}/generations').json()['generations'] == []
