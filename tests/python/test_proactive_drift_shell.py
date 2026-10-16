"""A finite preset owns a real process inside the original tick, never a child task."""
import asyncio
import json
from pathlib import Path
import sys
import hashlib

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock, ModelTextBlock
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_authoring_api import NotesAuthor
from test_proactive_drift_delivery import prepare, settled
from test_background_shell import process_alive

pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.mark.parametrize('allow_shell', [False, True])
def test_missing_skill_shell_rule_is_not_satisfied_by_an_unrelated_owner_preset(api, monkeypatch, allow_shell):
    client, _, _, _ = api
    role, path, _ = prepare(client, monkeypatch, extra_tools=('shell',),
        authoring_extra={'allowShell': allow_shell},
        agent_extra={'shell': {'enabled': True, 'allowedCommands': [str(Path(sys.executable).resolve())]}},
        config_extra={'shellRuleIds': ['python-version']})
    try:
        item = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'reading-notes')
        assert item['status'] == ('pending_dependency' if allow_shell else 'untrusted')
        assert 'skill-readonly-preset' in item['missingShellRules']
    finally:
        asyncio.run(main._proactive.close())


def test_declared_shell_rule_is_checked_even_when_skill_uses_no_shell_tool(api, monkeypatch):
    client, _, _, _ = api
    class Author(NotesAuthor):
        async def stream_structured_messages(self, messages, configuration, tools):
            async for event in super().stream_structured_messages(messages, configuration, tools):
                if getattr(event, 'name', None) == 'write_file' and event.arguments['path'].endswith('drift.json'):
                    value = json.loads(event.arguments['content'])
                    value['shellRuleIds'] = ['unavailable-preset']
                    event.arguments['content'] = json.dumps(value)
                yield event
    role, path, _ = prepare(client, monkeypatch, author=Author(), authoring_extra={'allowShell': True},
        config_extra={'shellRuleIds': ['unavailable-preset']})
    try:
        item = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'reading-notes')
        assert item['status'] == 'pending_dependency' and 'unavailable-preset' in item['missingShellRules']
    finally:
        asyncio.run(main._proactive.close())


def test_fixed_readonly_shell_executes_real_version_in_original_task_and_rejects_arbitrary_command(api, monkeypatch):
    client, _, _, _ = api
    class Author(NotesAuthor):
        async def stream_structured_messages(self, messages, configuration, tools):
            async for event in super().stream_structured_messages(messages, configuration, tools):
                if getattr(event, 'name', None) == 'write_file':
                    if event.arguments['path'].endswith('drift.json'):
                        value = json.loads(event.arguments['content'])
                        value['shellRuleIds'] = ['python-version']
                        event.arguments['content'] = json.dumps(value)
                    elif event.arguments['path'].endswith('SKILL.md'):
                        event.arguments['content'] = event.arguments['content'].replace('finish_drift]', 'shell, finish_drift]')
                yield event
    role, path, now = prepare(client, monkeypatch, extra_tools=('shell',), author=Author(),
        authoring_extra={'allowShell': True}, agent_extra={'shell': {'enabled': True, 'allowedCommands': [str(Path(sys.executable).resolve())]}},
        config_extra={'shellRuleIds': ['python-version']})

    class Shell:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests == 2:
                name, arguments = 'shell', {'command': 'python -c "raise SystemExit(99)"'}
            elif self.requests == 3:
                assert messages[-1].is_error
                name, arguments = 'shell', {'rule_id': 'python-version'}
            else:
                assert self.requests == 4
                result = json.loads(messages[-1].content[0].text)
                assert result['status'] == 'succeeded' and result['exitCode'] == 0 and 'Python ' in result['output']
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '实际完成有限版本核验',
                    'next_action': '继续本地工作', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'shell-{self.requests}', name, arguments),)))
    adapter = Shell()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    try:
        options = client.get(path + '/drift-grant-options').json()
        assert next(item for item in options['shellRules'] if item['id'] == 'python-version')['available']
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert adapter.requests == 4, state
        assert len(state['runs']) == 1 and state['runs'][0]['status'] == 'finished_silent'
        assert state['runs'][0]['timeoutSeconds'] == 600
        assert main._proactive._drift_owners == {}
    finally:
        asyncio.run(main._proactive.close())


def test_real_owner_chat_seals_drift_and_joins_shell_process_tree_without_mcp_host(api, monkeypatch, tmp_path):
    from backend.app.proactive_runtime import drift_shell
    from backend.app.model_adapter import ModelTextDelta
    client, _, _, _ = api
    # A trusted test-host preset exercises the real ProcessExecutor/Job/group
    # seam. Production's finite version presets accept no script/argv input.
    script = tmp_path / 'finite-owned-fixture.py'
    script.write_text('import subprocess,sys,time,os,json\n'
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
        'print(json.dumps({"pid":os.getpid(),"child":child.pid}),flush=True)\n'
        'time.sleep(60)\n', encoding='utf-8')
    executable = str(Path(sys.executable).resolve())
    preset = drift_shell.Preset('fixture-finite-wait', 'owned local process fixture', (executable, str(script)),
        hashlib.sha256(Path(executable).read_bytes()).hexdigest())
    monkeypatch.setattr(drift_shell, 'presets', lambda: {preset.identifier: preset})
    class Author(NotesAuthor):
        async def stream_structured_messages(self, messages, configuration, tools):
            async for event in super().stream_structured_messages(messages, configuration, tools):
                if getattr(event, 'name', None) == 'write_file':
                    if event.arguments['path'].endswith('drift.json'):
                        value = json.loads(event.arguments['content'])
                        value['shellRuleIds'] = [preset.identifier]
                        event.arguments['content'] = json.dumps(value)
                    elif event.arguments['path'].endswith('SKILL.md'):
                        event.arguments['content'] = event.arguments['content'].replace('finish_drift]', 'shell, finish_drift]')
                yield event
    role, _, now = prepare(client, monkeypatch, extra_tools=('shell',), author=Author(),
        authoring_extra={'allowShell': True}, agent_extra={'shell': {'enabled': True, 'allowedCommands': [executable]}},
        config_extra={'shellRuleIds': [preset.identifier]})
    app = main._proactive
    # No MCP transport is involved; resource cleanup must not return early.
    app.mcp_host = None
    class Waiting:
        requests = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            yield ModelTextDelta('主人输入已经真实接受')
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            if not any('这是获准的自主Drift' in getattr(block, 'text', '') for message in messages for block in message.content):
                yield ModelStreamDone(ModelAssistantMessage(content=(ModelTextBlock('主人输入已经真实接受'),)))
                return
            self.requests += 1
            assert self.requests <= 2
            name, arguments = ('activate_skill', {'skill_id': 'reading-notes'}) if self.requests == 1 else ('shell', {'rule_id': preset.identifier})
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'held-shell-{self.requests}', name, arguments),)))
    model = Waiting()
    monkeypatch.setattr(main, 'model_adapter', model)
    pids = []
    async def exercise():
        now[0] += 1800
        await app.on_clock()
        async with asyncio.timeout(15):
            while not pids:
                state = app.query(role)
                if state['runs']:
                    run = state['runs'][0]
                    executor = app._executors.get(run['id'])
                    if run['taskId'] and executor is not None:
                        snapshot = app.tasks.query(run['taskId'], executor.origin.task_origin())
                        for line in snapshot.output.splitlines():
                            if line.startswith('{'):
                                value = json.loads(line)
                                pids.extend([value['pid'], value['child']])
                await asyncio.sleep(.01)
        assert all(process_alive(pid) for pid in pids)
        response = await asyncio.to_thread(client.post, f'/api/roles/{role}/messages', json={'content': '真实主人现在接管'})
        assert 'assistant_completed' in response.text and '主人输入已经真实接受' in response.text
        state = await settled(app, role, clock=False)
        assert model.requests == 2 and state['runs'][0]['status'] == 'aborted'
        assert app._drift_owners == {} and all(not process_alive(pid) for pid in pids)
        assert not state['cleanupPending'] and not state['humanBusy']
    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(app.close())
        assert all(not process_alive(pid) for pid in pids)
