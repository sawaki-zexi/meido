import asyncio
from datetime import datetime, timezone

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def test_tool_explains_missing_credentials_and_new_tool_works_after_token_save(novelai_main):
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun
    client, service = novelai_main
    app, role = service.application, service.role
    key = f'role:{role.id}'
    service.sessions.create_run_with_messages(AgentRun(runId='missing-token-run', roleId=role.id, sessionKey=key,
        status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={'humanInputSource': 'desktop_owner'}), 'Draw a sky')
    client.delete('/api/services/novelai/token')
    tool = app.tool_for(role.id, is_owner=True)
    assert '尚未配置 NovelAI 凭据' in tool.definition.description
    context = ToolContext(role.id, key, 'missing-token-run', CancellationToken(), 'missing-token-call')
    denied = asyncio.run(tool.execute(service.request, context))
    assert denied.is_error and denied.details['errorCode'] == 'credential_missing'
    assert not service.peer.requests
    saved = client.put('/api/services/novelai/token', json={'token': 'new-fixture-only-secret'})
    assert saved.status_code == 200, saved.text
    assert saved.json()['availability']['available']
    # New credentials do not grant an old tool the new configuration revision.
    assert asyncio.run(tool.execute(service.request, context)).is_error
    assert not service.peer.requests
    fresh = app.tool_for(role.id, is_owner=True)
    result = asyncio.run(fresh.execute(service.request, context))
    assert not result.is_error
    assert len(service.peer.requests) == 1


def test_chat_tool_schema_explains_host_defaults_for_expression_calls():
    from backend.app.novelai_generation.tools import GenerateImageTool
    schema = GenerateImageTool.definition.input_schema
    assert schema['required'] == ['prompt']
    assert {'prompt', 'negative_prompt', 'mode', 'samples', 'model', 'seed'} <= set(schema['properties'])
    assert 'do not call it every turn' in GenerateImageTool.definition.description
    assert 'default' not in schema['properties']['samples']
    assert '宿主固定为一张' in schema['properties']['samples']['description']


def test_frozen_tool_cannot_use_later_broader_generation_grant(novelai_service):
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun, RoleUpdateInput
    service = novelai_service
    app, role = service.application, service.role
    key = f'role:{role.id}'
    service.sessions.create_run_with_messages(AgentRun(runId='frozen-run', roleId=role.id, sessionKey=key,
        status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={'humanInputSource': 'desktop_owner'}), 'draw a sky')
    tool = app.tool_for(role.id, is_owner=True)
    settings = app.settings.save({'allowedModels': ['nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full']}, expected_revision=app.settings.public()['revision'])
    service.roles.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
        agentConfig={'novelai': {'manualEnabled': True, 'consentRevision': settings['revision']}}))
    result = asyncio.run(tool.execute({**service.request, 'model': 'nai-diffusion-4-5-full'},
        ToolContext(role.id, key, 'frozen-run', CancellationToken(), 'frozen-call')))
    assert result.is_error
    assert service.peer.requests == []


def test_role_permission_off_on_does_not_revive_an_unused_frozen_tool(novelai_main):
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun
    client, service = novelai_main
    role = service.role
    key = f'role:{role.id}'
    service.sessions.create_run_with_messages(AgentRun(runId='aba-run', roleId=role.id, sessionKey=key,
        status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={'humanInputSource': 'desktop_owner'}), 'draw a sky')
    tool = service.application.tool_for(role.id, is_owner=True)
    for enabled in (False, True):
        response = client.put(f'/api/roles/{role.id}', json={'name': role.name,
            'profile': role.profile.model_dump(), 'agentConfig': {'novelai': {'manualEnabled': enabled}}})
        assert response.status_code == 200
    result = asyncio.run(tool.execute(service.request, ToolContext(role.id, key, 'aba-run', CancellationToken(), 'aba-call')))
    assert result.is_error
    assert not service.peer.requests


def test_owner_tool_expands_role_tags_from_the_current_owner_message(novelai_service):
    from datetime import datetime, timezone
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun, RoleUpdateInput

    service = novelai_service
    app, role = service.application, service.role
    app.tags.save(role.id, {'name': 'Garden', 'keywords': ['garden'],
        'prompt': 'flowers, sunlight', 'negative_prompt': 'night', 'rating': 'general', 'enabled': True})
    service.roles.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
        agentConfig={'novelai': {'manualEnabled': True, 'promptTagsEnabled': True,
            'consentRevision': app.settings.public()['revision']}}))
    session_key = f'role:{role.id}'
    service.sessions.create_run_with_messages(AgentRun(runId='tag-run', roleId=role.id,
        sessionKey=session_key, status='running', startedAt=datetime.now(timezone.utc),
        modelSnapshot={'humanInputSource': 'desktop_owner'}), 'Please draw the garden.')
    tool = app.tool_for(role.id, is_owner=True)

    result = asyncio.run(tool.execute(
        # An optional compatibility intent cannot relabel this chat tool.
        {'prompt': 'blue sky', 'negative_prompt': 'rain', 'intent': 'user_requested'},
        ToolContext(role.id, session_key, 'tag-run', CancellationToken(), 'tag-call')))

    assert not result.is_error
    assert len(service.peer.requests) == 1
    payload = service.peer.requests[0]['body']
    assert payload['input'] == 'blue sky, flowers, sunlight'
    assert payload['action'] == 'generate'
    assert payload['model'] == 'nai-diffusion-4-5-curated'
    assert payload['parameters']['width'] == 1024
    assert payload['parameters']['height'] == 1024
    assert payload['parameters']['negative_prompt'] == 'rain, night'
    assert payload['parameters']['n_samples'] == 1
    history = service.application.ledger.list(role.id)
    assert history['generations'][0]['purpose'] == 'chat_expression'
