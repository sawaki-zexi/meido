"""Weighted role tags reach both chat and CG unchanged through the public flows."""
import asyncio
from datetime import datetime, timezone

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
from novelai_scene_fixtures import scene_main  # noqa: F401
from test_novelai_scene import owner_turn


def enable_weighted_tag(client, service):
    path = f'/api/roles/{service.role.id}'
    saved = client.post(path + '/novelai/prompt-tags', json={
        'name': 'Garden', 'keywords': ['garden'], 'rating': 'general', 'enabled': True,
        'prompt': '1.8::green dress, blue eyes::, smile',
        'negative_prompt': '[[clouds, night]], blur',
    })
    assert saved.status_code == 200, saved.text
    enabled = client.put(path, json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert enabled.status_code == 200, enabled.text


def test_chat_generation_freezes_complete_weighted_prompts_in_request_and_history(novelai_main):
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun

    client, service = novelai_main
    enable_weighted_tag(client, service)
    role_id = service.role.id
    session_key = f'role:{role_id}'
    service.sessions.create_run_with_messages(AgentRun(runId='weighted-chat', roleId=role_id,
        sessionKey=session_key, status='running', startedAt=datetime.now(timezone.utc),
        modelSnapshot={'humanInputSource': 'desktop_owner'}), 'Please draw the garden.')
    tool = service.application.tool_for(role_id, is_owner=True)

    result = asyncio.run(tool.execute({'prompt': '1.2::red hair, blue eyes::',
        'negative_prompt': '[[rain, night]]'},
        ToolContext(role_id, session_key, 'weighted-chat', CancellationToken(), 'weighted-call')))

    assert not result.is_error
    assert len(service.peer.requests) == 1
    payload = service.peer.requests[0]['body']
    assert payload['input'] == '1.2::red hair, blue eyes::, 1.8::green dress, blue eyes::, smile'
    assert payload['parameters']['negative_prompt'] == '[[rain, night]], [[clouds, night]], blur'
    saved = client.get(f'/api/roles/{role_id}/generations').json()['generations'][0]
    assert saved['parameters']['prompt'] == payload['input']
    assert saved['parameters']['negative_prompt'] == payload['parameters']['negative_prompt']


def test_scene_generation_keeps_weighted_groups_closed_before_adding_composition_tags(scene_main):
    client, service, peer, _ = scene_main
    enable_weighted_tag(client, service)
    peer.observation.update(prompt='1.2::red hair, blue eyes::', negative_prompt='[[rain, night]]')

    result, _ = owner_turn(client, service, 'The garden')

    assert result['status'] == 'succeeded'
    assert len(service.peer.requests) == 1
    payload = service.peer.requests[0]['body']
    assert payload['input'] == ('1.2::red hair, blue eyes::, 1.8::green dress, blue eyes::, smile, '
                                'third-person view, cinematic composition, character visible in frame')
    assert payload['parameters']['negative_prompt'] == ('[[rain, night]], [[clouds, night]], blur, '
                                                        'first-person view, pov, selfie')
    assert result['parameters']['prompt'] == payload['input']
    assert result['parameters']['negative_prompt'] == payload['parameters']['negative_prompt']
