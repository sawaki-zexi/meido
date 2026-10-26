"""Public HTTP/model tool discovery and source gates with a controlled executor."""
import asyncio
import json
import time

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_coding_application import ControlledExecutor, GrantedConfiguration

pytestmark = pytest.mark.usefixtures('local_platform_only')


class Configuration(GrantedConfiguration):
    def public(self):
        grant, profile, build = self.resolve(None, None, 'repo-a')
        return {'enabled': True, 'repositories': [grant | {'allowedRoleIds': [self.role_id]}],
                'profiles': [profile | {'allowedRoleIds': [self.role_id], 'verification': {'status': 'verified', 'revision': profile['revision']}}], 'nativeBuild': build}

    def availability(self, *args):
        return {'status': 'available', 'reason': 'controlled fixture policy, no real authentication'}


def test_public_owner_chat_discovers_starts_and_receives_coding_without_second_model_call(api, tmp_path, monkeypatch):
    from backend.app import main
    client, roles, sessions, tasks = api
    configuration = Configuration()
    configuration.role_id = ''
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda role_id: roles.get(role_id).agentConfig.coding if roles.get(role_id) else None,
        authorized=main._coding_origin_authorized, config=configuration, executor_factory=ControlledExecutor,
        lock_for=main._runtime_role_lock)
    monkeypatch.setattr(main, '_coding', application)
    requests, accepted = [], []
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append((messages, tools))
            last_user = max(i for i, item in enumerate(messages) if item['role'] == 'user')
            results = [item for item in messages[last_user + 1:] if item['role'] == 'tool']
            names = {item['function']['name'] for item in tools}
            if not results:
                assert 'herdr_start_task' not in names and 'tool_search' in names
                yield ModelToolCall('discover-coding', 'tool_search', {'query': 'select:herdr_start_task'})
                yield ModelResponseDone("tool_use")
            elif len(results) == 1:
                assert 'herdr_start_task' in names
                yield ModelToolCall('start-coding', 'herdr_start_task', {'repository': 'repo-a', 'requirement': '完整需求🙂'})
                yield ModelResponseDone("tool_use")
            else:
                accepted.append(json.loads(results[-1]['content']))
                yield ModelTextDelta('已交给后台')
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    try:
        role = client.post('/api/roles', json={'name': '编程角色', 'profile': {'profile': '测试'},
            'agentConfig': {'enabledTools': ['tool_search'], 'coding': {'enabled': True,
                'repoGrantIds': ['repo-a'], 'profileIds': ['profile-a'], 'defaultHerdrProfileId': 'profile-a'}}}).json()['role']
        configuration.role_id = role['id']
        response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '请完成完整需求'})
        assert 'assistant_completed' in response.text, response.text
        assert accepted[0]['status'] == 'accepted', accepted
        assert len(requests) == 3
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            asyncio.run(application.tick())
            messages = sessions.list_messages(f"role:{role['id']}")
            completion = [item for item in messages if item.metadata.get('internalEvent') == 'coding_completion']
            if completion:
                break
            time.sleep(.01)
        assert len(completion) == 1
        assert completion[0].runId is None and completion[0].metadata['deliveryStatus'] == 'sent'
        assert len(requests) == 3
        assert len([item for item in messages if item.role == 'user']) == 1
        listing = client.get(f"/api/coding/roles/{role['id']}/tasks").json()
        assert listing['tasks'][0]['id'] == accepted[0]['id']
        assert listing['tasks'][0]['cleanupState'] == 'complete'
        catalog = client.get(f"/api/roles/{role['id']}/tools").json()
        assert {item['name'] for item in catalog['tools'] if item['source'] == 'builtin:coding'} == {
            'herdr_start_task', 'herdr_list_tasks', 'herdr_get_task', 'herdr_continue_task', 'herdr_cancel_task'}
    finally:
        asyncio.run(application.close())
