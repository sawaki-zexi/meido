"""Explicitly permitted autonomous S06 authoring proves its own true origin."""
import asyncio
import json
import re

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_delivery import prepare, settled

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_explicit_autonomous_builtin_s06_authors_real_private_bundle_for_a_later_tick(api, monkeypatch):
    client, roles, _, _ = api
    role, path, now = prepare(client, monkeypatch, authoring_extra={'allowAutonomousAuthoring': True})
    catalog = client.get(path + '/drift-skills').json()['skills']
    assert any(item['id'] == 'create-drift-skill' and item['status'] == 'available' for item in catalog)
    meme = next(item for item in catalog if item['id'] == 'meme-manage')
    assert meme['status'] == 'pending_dependency' and '图片核验' in meme['reason'] and '主人聊天' in meme['reason']

    class Author:
        requests = 0
        files = []
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'create-drift-skill'}
            elif self.requests <= 4:
                if not self.files:
                    instructions = json.loads(messages[-1].content[0].text)['instructions']
                    self.files = [(path.removeprefix('drift/').replace('reading-notes', 'autonomous-notes'),
                        content.replace('reading-notes', 'autonomous-notes') + '\n') for path, content in re.findall(
                        r'### `([^`]+)`\s+```(?:json|markdown|text)\n(.*?)\n```', instructions, re.S)]
                    assert len(self.files) == 3
                target, content = self.files[self.requests - 2]
                name, arguments = 'write_file', {'path': target, 'content': content}
            else:
                assert self.requests == 5
                name, arguments = 'finish_drift', {'skill_used': 'create-drift-skill', 'one_line': '真实创建下一轮笔记技能',
                    'next_action': '下次使用新技能', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'auto-author-{self.requests}', name, arguments),)))
    adapter = Author()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert adapter.requests == 5 and state['runs'][0]['status'] == 'finished_silent', state
        skill = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'autonomous-notes')
        assert skill['status'] == 'available' and skill['provenance']['status'] == 'verified'
        assert skill['provenance']['runId'] == state['runs'][0]['id']
        assert (roles.workspace_path(role) / 'drift/skills/autonomous-notes/SKILL.md').is_file()
        assert not (main.Path(__file__).resolve().parents[2] / 'backend/app/builtin_skills/create-drift-skill/state.json').exists()
    finally:
        asyncio.run(main._proactive.close())
