"""Shiori-style optional vision binding through real tool and local vision HTTP."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.models import ModelConfigurationInput
from test_screen_perception import vision_server
from test_screen_perception_api import setup_screen_api


def use_chat_model(tmp_path, monkeypatch, url, *, role_binding, code=None):
    service, _, context, roles, models, sessions, processes = setup_screen_api(tmp_path, monkeypatch, url, code=code)
    original = models.get(roles.get(context.role_id).agentConfig.screenPerception.visionModelConfigurationId)
    if role_binding:
        models.create(ModelConfigurationInput(providerId='custom', provider='other', baseUrl='http://127.0.0.1:9/v1', model='other'))
        roles.set_model_configuration(context.role_id, original.id)
    monkeypatch.setattr(service, '_role', main._plugin_role)
    client = TestClient(main.app)
    state = client.get('/api/plugin-management/screen').json()
    response = client.put('/api/plugin-management/screen/configuration', json={
        'values': {'visionModelConfigurationId': None}, 'expectedRevision': state['revision'],
    })
    assert response.status_code == 200, response.text
    return service, context, roles, models, sessions, processes, original


def screen_tool(role):
    return next(tool for tool in main._runtime_tool_candidates(role, preview=True) if tool.definition.name == 'observe_screen')


@pytest.mark.parametrize('role_binding', [False, True])
def test_unset_vision_uses_effective_chat_model_without_persisting_binding(tmp_path, monkeypatch, role_binding):
    with vision_server() as (url, requests):
        service, context, roles, _, _, processes, original = use_chat_model(tmp_path, monkeypatch, url, role_binding=role_binding)
        before = roles.get(context.role_id).model_dump()
        result = asyncio.run(screen_tool(roles.get(context.role_id)).execute({}, context))
        assert not result.is_error, result.content
        assert json.loads(result.content)['interface_summary'] == 'fixture editor'
        assert len(requests) == 1 and requests[0][1]['model'] == original.model
        assert processes[0].returncode == 0
        assert roles.get(context.role_id).model_dump() == before
        assert main.plugin_configuration.get('screen').values['visionModelConfigurationId'] is None
        asyncio.run(service.close())


@pytest.mark.parametrize('role_binding,mutation', [(True, 'role'), (False, 'activate'), (False, 'create'), (False, 'update')])
def test_chat_binding_change_reaps_capture_and_rejects_frozen_tool(tmp_path, monkeypatch, role_binding, mutation):
    async def run():
        service, context, roles, models, _, processes, original = use_chat_model(
            tmp_path, monkeypatch, 'http://127.0.0.1:9/v1', role_binding=role_binding, code='import time;time.sleep(30)')
        other = models.create(ModelConfigurationInput(providerId='custom', provider='next', baseUrl='http://127.0.0.1:9/v1', model='next'))
        models.activate(original.id)
        tool = screen_tool(roles.get(context.role_id))
        task = asyncio.create_task(tool.execute({}, context))
        async with asyncio.timeout(5):
            while not processes:
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://meido') as client:
            if role_binding:
                response = await client.put(f'/api/roles/{context.role_id}/model-configuration', json={'configurationId': other.id})
            elif mutation == 'activate':
                response = await client.post(f'/api/model/configurations/{other.id}/activate')
            elif mutation == 'create':
                response = await client.post('/api/model/configurations', json=other.model_dump(exclude={'id'}))
            else:
                response = await client.put(f'/api/model/configurations/{other.id}', json=other.model_dump(exclude={'id'}))
        assert response.status_code == 200, response.text
        assert processes[0].returncode is not None
        with pytest.raises(asyncio.CancelledError):
            await task
        result = await tool.execute({}, context)
        assert result.is_error and 'configuration_changed' in result.content
        assert len(processes) == 1
        await service.close()
    asyncio.run(run())


def test_explicit_missing_vision_never_uses_valid_chat_model(tmp_path, monkeypatch):
    with vision_server() as (url, requests):
        service, _, context, roles, models, _, processes = setup_screen_api(tmp_path, monkeypatch, url)
        explicit = roles.get(context.role_id).agentConfig.screenPerception.visionModelConfigurationId
        models.create(ModelConfigurationInput(providerId='custom', provider='chat', baseUrl=url, model='valid-chat'))
        models.delete(explicit)
        monkeypatch.setattr(service, '_role', main._plugin_role)
        result = asyncio.run(screen_tool(roles.get(context.role_id)).execute({}, context))
        assert result.is_error and 'configuration_missing' in result.content
        assert not requests and not processes
        asyncio.run(service.close())
