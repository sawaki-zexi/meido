import asyncio
from datetime import datetime, timezone

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


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
