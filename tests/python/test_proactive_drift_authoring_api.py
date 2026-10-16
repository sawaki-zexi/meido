"""Normal owner chat authors the exact artifacts consumed by later Drift runs."""
import asyncio
import json
import re
import time

import pytest

from backend.app import main
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401


pytestmark = pytest.mark.usefixtures('local_platform_only')


class NotesAuthor:
    def __init__(self):
        self.files, self.step = None, 0

    async def stream_structured_messages(self, messages, configuration, tools):
        system = '\n'.join(str(item['content']) for item in messages if item['role'] == 'system')
        if self.files is None:
            self.files = [(path, text + '\n') for path, text in re.findall(
                r'### `([^`]+)`\s+```(?:json|markdown|text)\n(.*?)\n```', system, re.S)]
            assert len(self.files) == 3, 'S06 must supply its three real, runnable author templates'
        if self.step < len(self.files):
            path, content = self.files[self.step]
            self.step += 1
            yield ModelToolCall(f's06-write-{self.step}', 'write_file', {'path': path, 'content': content})
        else:
            yield ModelTextDelta('角色长期技能与工作文件已经实际保存；运行仍需独立许可。')


@pytest.mark.parametrize('with_scope', [False, True])
def test_explicit_s06_uses_its_real_body_and_file_tools_to_create_three_role_private_artifacts(api, monkeypatch, with_scope):
    client, roles, sessions, _ = api
    created = client.post('/api/roles', json={'name': '长期小任务作者', 'profile': {'profile': '只用本地夹具'},
        'agentConfig': {'enabledTools': ['write_file', 'read_file', 'list_dir']}})
    assert created.status_code == 201, created.text
    role = created.json()['role']
    identifier = role['id']
    path = f'/api/roles/{identifier}/autonomy'
    if with_scope:
        saved = client.put(path, json={'expectedRevision': 0, 'config': {'driftAuthoring': {'enabled': True,
            'allowedTools': ['read_file', 'write_file', 'edit_file', 'finish_drift'], 'allowMcp': True}}})
        assert saved.status_code == 200, saved.text
        assert saved.json()['config']['driftAuthoring']['scopeId']

    adapter = NotesAuthor()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    try:
        response = client.post(f'/api/roles/{identifier}/messages', json={'content': '$create-drift-skill 创建读书笔记长期小任务'})
        assert 'assistant_completed' in response.text, response.text
        root = roles.workspace_path(identifier) / 'drift'
        assert 'finish_drift' in (root / 'skills/reading-notes/SKILL.md').read_text(encoding='utf-8')
        assert json.loads((root / 'skills/reading-notes/references/drift.json').read_text())['skillId'] == 'reading-notes'
        assert (root / 'work/reading-notes/notes.md').is_file()
        user = next(item for item in sessions.list_messages(f'role:{identifier}') if item.role == 'user')
        run = sessions.get_run(user.runId)
        activated = run.modelSnapshot['capabilities']['skills']
        assert any(item['id'] == 'create-drift-skill' and item['source'] == 'builtin' and item['contentHash'] for item in activated)
        assert adapter.step == 3
        if with_scope:
            response = client.get(path + '/drift-skills')
            assert response.status_code == 200, response.text
            skill = next(item for item in response.json()['skills'] if item['id'] == 'reading-notes')
            assert skill['provenance']['status'] == 'verified'
            assert skill['provenance']['runId'] == run.runId
            assert skill['provenance']['authorSkillHash'] == next(item['contentHash'] for item in activated if item['id'] == 'create-drift-skill')
            assert len(skill['bundleSha256']) == 64
            assert skill['status'] == 'pending_dependency' and set(skill['missingTools']) == {'read_file', 'write_file', 'edit_file'}
            assert client.get(path).json()['runs'] == []
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())


def test_same_owner_authored_artifact_runs_in_the_original_tick_and_keeps_real_progress_for_the_next_run(api, monkeypatch):
    client, roles, sessions, tasks = api
    monkeypatch.setattr(main, '_proactive', None)
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    role = client.post('/api/roles', json={'name': '持久长期笔记', 'profile': {'profile': '合成笔记'},
        'agentConfig': {'enabledTools': ['write_file', 'read_file', 'edit_file', 'list_dir']}}).json()['role']
    identifier = role['id']
    path = f'/api/roles/{identifier}/autonomy'
    saved = client.put(path, json={'expectedRevision': 0, 'config': {'driftAuthoring': {'enabled': True,
        'allowedTools': ['read_file', 'write_file', 'edit_file', 'finish_drift']}}})
    assert saved.status_code == 200, saved.text
    monkeypatch.setattr(main, 'model_adapter', NotesAuthor())
    assert 'assistant_completed' in client.post(f'/api/roles/{identifier}/messages', json={'content': '$create-drift-skill 创建长期笔记'}).text
    before = client.get(path + '/drift-skills').json()['skills'][0]['bundleSha256']
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-drift-only', 'apiKey': 'fixture'}).json()['configuration']

    class Drift:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            step = (self.requests - 1) % 4
            assert 0 < max_tokens <= 1024
            if step == 0:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif step == 1:
                assert '读书笔记' in messages[-1].content[0].text
                name, arguments = 'read_file', {'path': 'work/reading-notes/notes.md'}
            elif step == 2:
                expected = '尚未开始' if self.requests < 5 else '真实笔记第1轮'
                assert expected in messages[-1].content[0].text
                name, arguments = 'write_file', {'path': 'work/reading-notes/notes.md',
                    'content': f'真实笔记第{1 if self.requests < 5 else 2}轮'}
            else:
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '真实推进一条笔记',
                    'next_action': '下次读取并再写一条', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'drift-{self.requests}', name, arguments),)))

    adapter = Drift()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    config = client.get(path).json()['config']
    enabled = client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
        'driftEnabled': True, 'driftModelConfigurationId': model['id'], 'consentRevision': 1,
        'driftToolGrants': ['read_file', 'write_file', 'edit_file']}})
    assert enabled.status_code == 200, enabled.text

    async def tick():
        await main._proactive.on_clock()
        async with asyncio.timeout(20):
            while True:
                await main._proactive.recover()
                state = main._proactive.query(identifier)
                if state['runs'] and all(run['runtimeAcknowledged'] for run in state['runs']):
                    return state
                await asyncio.sleep(.01)

    try:
        now[0] += 1800
        first = asyncio.run(tick())
        assert adapter.requests == 4, first
        assert first['runs'][0]['mode'] == 'drift' and first['runs'][0]['status'] == 'finished_silent'
        first_id = first['runs'][0]['taskId']
        assert first_id.startswith('autonomy_tick_') and first['runs'][0]['timeoutSeconds'] == 600
        skill = client.get(path + '/drift-skills').json()['skills'][0]
        assert skill['bundleSha256'] == before
        assert skill['progress']['runCount'] == 1 and skill['progress']['nextAction'] == '下次读取并再写一条'
        projection = roles.workspace_path(identifier) / 'drift/state/reading-notes.json'
        assert json.loads(projection.read_text(encoding='utf-8'))['runCount'] == 1
        assert not any(message.messageType == 'internal_completion' for message in sessions.list_messages(f'role:{identifier}'))
        asyncio.run(main._proactive.close())
        monkeypatch.setattr(main, '_proactive', None)
        app = main._proactive_application()
        now[0] += 10800
        second = asyncio.run(tick())
        assert adapter.requests == 8 and len(second['runs']) == 2
        assert client.get(path + '/drift-skills').json()['skills'][0]['progress']['runCount'] == 2
        assert json.loads(projection.read_text(encoding='utf-8'))['runCount'] == 2
        assert all(run['runtimeAcknowledged'] for run in second['runs'])
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())
