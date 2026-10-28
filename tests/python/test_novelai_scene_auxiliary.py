"""Auxiliary protocol failures never invoke tools or repeat either paid request."""
import time

import pytest

from novelai_scene_fixtures import scene_main
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
from test_novelai_scene import owner_turn


@pytest.mark.parametrize('fault', ['http_503', 'tool_call', 'duplicate_json', 'oversized', 'non_ascii', 'timeout'])
def test_auxiliary_failure_keeps_attempt_and_never_dispatches_generation(scene_main, monkeypatch, fault):
    from backend.app.novelai_generation import scene
    client, service, peer, _ = scene_main
    if fault == 'http_503': peer.aux_status = 503
    elif fault == 'tool_call': peer.aux_tool = True
    elif fault == 'duplicate_json': peer.aux_text = '{"transition":"none","transition":"started"}'
    elif fault == 'oversized': peer.aux_text = 'x' * 17000
    elif fault == 'non_ascii': peer.observation['prompt'] = '花园'
    elif fault == 'timeout':
        monkeypatch.setattr(scene, 'AUXILIARY_TIMEOUT_SECONDS', 2.0)
        peer.aux_block = True
    try:
        result, page = owner_turn(client, service, 'A real owner turn, with an invalid auxiliary response.')
        assert result['status'] == 'failed_response' and result['auxiliaryStatus'] == 'failed'
        assert result['errorCode'] == 'scene_observation_failed' and result['auxiliaryConsumption'] == 'possible'
        assert len([request for request in peer.requests if request['model'] == 'fixture-scene']) == 1
        assert not service.peer.requests
        assert page['usage']['accountAttempts'] == page['usage']['cgAttempts'] == 1
        assert page['usage']['manualAttempts'] == 0
    finally:
        peer.aux_release.set()


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
