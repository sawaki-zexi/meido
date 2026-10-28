"""Scene transitions, visual tombstones and cooldown use actual owner turns."""
import time

from novelai_scene_fixtures import scene_main
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
from test_novelai_scene import owner_turn


def test_same_scene_waits_five_real_owner_turns_but_changed_scene_cannot_reuse_dispatched_visual(scene_main):
    client, service, peer, _ = scene_main
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'sceneCgDailyAttempts': 20}}})
    assert response.status_code == 200, response.text
    first, _ = owner_turn(client, service, 'We enter the garden.')
    assert first['status'] == 'succeeded' and len(service.peer.requests) == 1
    peer.observation.update(transition='same', visual_key='garden-evening')
    for number in range(4):
        result, _ = owner_turn(client, service, f'Owner turn {number + 1} after the first CG.')
        assert result['status'] == 'completed_without_image' and result['errorCode'] == 'scene_cooldown'
        assert len(service.peer.requests) == 1
    fifth, _ = owner_turn(client, service, 'The fifth real owner turn after the CG.')
    assert fifth['status'] == 'succeeded' and len(service.peer.requests) == 2
    peer.observation.update(transition='changed', scene_key='house', visual_key='garden-day')
    duplicate, _ = owner_turn(client, service, 'A changed scene cannot reuse a dispatched visual.')
    assert duplicate['errorCode'] == 'scene_visual_duplicate' and len(service.peer.requests) == 2
    peer.observation.update(transition='changed', scene_key='beach', visual_key='beach-day')
    changed, history = owner_turn(client, service, 'A genuinely new scene bypasses same-scene cooldown.')
    assert changed['status'] == 'succeeded' and len(service.peer.requests) == 3
    assert history['usage']['cgAttempts'] == 8


def test_actual_cg_uses_the_same_role_and_rating_filtered_expansion_as_owner_preview(scene_main):
    client, service, _, _ = scene_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    saved = client.post(path, json={'name': 'Flowers', 'keywords': ['garden'], 'prompt': 'flowers',
        'negative_prompt': 'fog', 'rating': 'general', 'enabled': True})
    assert saved.status_code == 200, saved.text
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert response.status_code == 200, response.text
    preview = client.post(path + '/preview', json={'text': 'The garden', 'prompt': 'blue sky, garden', 'negative_prompt': 'rain'})
    assert preview.status_code == 200, preview.text
    result, _ = owner_turn(client, service, 'The garden')
    expected_prompt = 'blue sky, garden, flowers'
    expected_negative = 'rain, fog'
    assert preview.json()['prompt'] == expected_prompt
    assert preview.json()['negative_prompt'] == expected_negative
    assert result['parameters']['prompt'].startswith(expected_prompt + ', ')
    assert result['parameters']['negative_prompt'].startswith(expected_negative + ', ')
    assert all(tag in result['parameters']['prompt'] for tag in ('third-person view', 'cinematic composition', 'character visible in frame'))
    assert all(tag in result['parameters']['negative_prompt'] for tag in ('first-person view', 'pov', 'selfie'))
    assert service.peer.requests[0]['body']['input'] == result['parameters']['prompt']


def test_real_same_turn_manual_unknown_claim_suppresses_auxiliary_cg(scene_main):
    client, service, peer, _ = scene_main
    peer.manual = True
    service.peer.body = b'a controlled unverifiable response after a paid request'
    response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'Please generate one image manually.'})
    assert 'assistant_completed' in response.text, response.text
    from backend.app.task_runtime import TaskOrigin
    origin = TaskOrigin(service.role.id, f'role:{service.role.id}', source_thread_id=f'desktop:{service.role.id}')
    for _ in range(200):
        task = next(iter(service.tasks.list(origin, kind='novelai_image')), None)
        if task is not None and task.ended_at is not None:
            break
        time.sleep(.02)
    history = client.get(f'/api/roles/{service.role.id}/generations').json()
    assert len(history['generations']) == 1 and history['generations'][0]['status'] == 'unknown'
    assert history['usage']['manualAttempts'] == 1 and history['usage']['cgAttempts'] == 0
    assert len(service.peer.requests) == 1
    assert not [request for request in peer.requests if request['model'] == 'fixture-scene']
