"""Ordinary skill behavior through real files, HTTP chat and the runtime seam."""
import asyncio
import hashlib
import json
import re
from dataclasses import replace

import pytest

from backend.app import main
from backend.app.agent_runtime.meido_provider import MeidoProvider, RequestContext
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.skill_discovery import SkillRun, SkillReadTool
from backend.app.agent_runtime.skills import SkillRegistry, validate_skill_text
from backend.app.agent_runtime.tools import ToolContext
from backend.app.agent_runtime.types import ToolResultMessage, UserMessage
from backend.app.model_adapter import ModelTextDelta, ModelToolCall, ModelResponseDone
from test_builtin_skills_api import skill_client

pytestmark = pytest.mark.usefixtures('local_platform_only')


def method(name='briefing', *, manual=False, body='真实简报步骤：先读取 input.txt，再给出核验后的结果。'):
    return f'---\nname: {name}\ndescription: 当主人要求整理工作简报时使用；闲聊不适用。\n' + (
        'allow_implicit_invocation: false\n' if manual else '') + f'---\n{body}\n'


def setup(tmp_path, monkeypatch, adapter):
    client, role_id, roles, sessions = skill_client(tmp_path, monkeypatch, adapter)
    from backend.app.multichannel import MultichannelStore
    monkeypatch.setattr(main, 'multichannel_store', MultichannelStore(sessions.database_path, sessions))
    monkeypatch.setattr(main, 'data_root', tmp_path / 'data')
    monkeypatch.setattr(main, 'project_root', tmp_path / 'project')
    library = main._skill_library(role_id)
    return client, role_id, roles, sessions, library


def enable(client, role_id):
    state = client.get(f'/api/roles/{role_id}/skills').json()
    response = client.put(f'/api/roles/{role_id}/skills/settings', json={'enabled': True, 'expectedRevision': state['revision']})
    assert response.status_code == 200, response.text


def ref_for(messages, name='briefing'):
    raw = json.dumps(messages, ensure_ascii=False)
    return re.search(rf'skill:role:{name}:[a-f0-9]{{16}}', raw).group(0)


def test_current_role_model_selects_unnamed_method_and_uses_existing_tool_without_classifier(tmp_path, monkeypatch):
    requests = []
    body = '真实简报步骤：先读取 input.txt，再给出核验后的结果。'

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            names = {item['function']['name'] for item in tools}
            assert {'skill_list', 'skill_read', 'read_file'} <= names
            if len(requests) == 1:
                initial = json.dumps(messages, ensure_ascii=False)
                assert body not in initial
                assert 'D:' not in initial and 'root_path' not in initial
                yield ModelToolCall('method', 'skill_read', {'ref': ref_for(messages)})
                yield ModelResponseDone('tool_use')
            elif len(requests) == 2:
                assert json.dumps(messages, ensure_ascii=False).count(body) == 1
                yield ModelToolCall('input', 'read_file', {'path': 'input.txt'})
                yield ModelResponseDone('tool_use')
            else:
                assert '已核验的工作事实' in json.dumps(messages, ensure_ascii=False)
                assert json.dumps(messages, ensure_ascii=False).count(body) == 1
                yield ModelTextDelta('简报已依据真实文件完成')

    client, role_id, roles, sessions, library = setup(tmp_path, monkeypatch, Adapter())
    saved = library.save(method(body=body))
    (roles.workspace_path(role_id) / 'input.txt').write_text('已核验的工作事实', encoding='utf-8')
    enable(client, role_id)
    response = client.post(f'/api/roles/{role_id}/messages', json={'content': '帮我整理今天的工作成果'})
    assert '简报已依据真实文件完成' in response.text
    assert len(requests) == 3
    reads = [item for item in sessions.runtime_messages(f'role:{role_id}') if isinstance(item, ToolResultMessage)]
    assert reads[0].details['skillRead']['contentHash'] == saved['contentHash']
    run = sessions.get_run(sessions.list_messages(f'role:{role_id}')[-1].runId)
    assert run.modelSnapshot['capabilities']['skillReads'][0]['ref'] == saved['ref']


def test_smalltalk_is_one_request_and_explicit_preload_reports_specific_errors(tmp_path, monkeypatch):
    requests = []

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta('直接回复')

    client, role_id, _, _, library = setup(tmp_path, monkeypatch, Adapter())
    saved = library.save(method(manual=True, body='手动专用正文'))
    disabled = library.save(method('disabled', body='不可出现的停用正文'))
    library.update_settings(ref=disabled['ref'], active=False)
    library.save(method('missing', body='必须读取 references/not-provided.md'))
    enable(client, role_id)
    response = client.post(f'/api/roles/{role_id}/messages', json={'content': '你好'})
    assert '直接回复' in response.text and len(requests) == 1
    assert '手动专用正文' not in json.dumps(requests[0], ensure_ascii=False)
    client.post(f'/api/roles/{role_id}/messages', json={'content': '$briefing 请按方法做', 'skillIds': ['disabled', 'unknown', 'missing']})
    current = json.dumps(requests[1], ensure_ascii=False)
    assert '手动专用正文' in current
    assert 'skill is disabled' in current and 'skill is not registered' in current
    assert 'missing supporting resources' in current and '不可出现的停用正文' not in current


def test_manual_limit_and_cross_role_refs_are_host_enforced_in_real_tool_loop(tmp_path, monkeypatch):
    ref = None
    calls = 0

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield ModelToolCall('forged', 'skill_read', {'ref': ref})
                yield ModelResponseDone('tool_use')
            else:
                assert 'manual-only skill requires explicit activation' in next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('需要主人明确指定')

    client, role_id, roles, sessions, library = setup(tmp_path, monkeypatch, Adapter())
    ref = library.save(method(manual=True))['ref']
    enable(client, role_id)
    result = client.post(f'/api/roles/{role_id}/messages', json={'content': '随便聊聊'})
    assert '需要主人明确指定' in result.text
    another = client.post('/api/roles', json={'name': '另一角色', 'profile': {'profile': '独立角色'}}).json()['role']['id']
    enable(client, another)
    listing = client.get(f'/api/roles/{another}/skills').json()
    assert ref not in json.dumps(listing)
    state = SkillRun(main._skill_library(another), role_id=another, session_key=f'role:{another}', run_id='other', prompt='')
    with pytest.raises(ValueError, match='unknown skill reference'):
        state.read(ref)
    assert client.get(f'/api/roles/{another}/skills/document', params={'ref': ref}).status_code == 422


def test_chat_import_saves_reads_back_and_next_turn_reuses_without_reinstalling_upload(tmp_path, monkeypatch):
    calls = 0
    raw = method('meeting-notes', body='会议方法：从已提供的议题提取决定与负责人。')

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                assert '会议方法' in json.dumps(messages, ensure_ascii=False)
                yield ModelToolCall('import', 'write_file', {'path': 'skills/meeting-notes/SKILL.md', 'content': raw})
                yield ModelResponseDone('tool_use')
            elif calls == 2:
                yield ModelToolCall('verify', 'read_file', {'path': 'skills/meeting-notes/SKILL.md'})
                yield ModelResponseDone('tool_use')
            elif calls == 3:
                assert 'saved-file validation: verified' in next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('已实际保存并读回')
            elif calls == 4:
                yield ModelToolCall('reuse', 'skill_read', {'ref': ref_for(messages, 'meeting-notes')})
                yield ModelResponseDone('tool_use')
            else:
                assert '会议方法：' in next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('复用会议方法完成整理')

    client, role_id, roles, sessions, library = setup(tmp_path, monkeypatch, Adapter())
    enable(client, role_id)
    response = client.post(f'/api/roles/{role_id}/attachments', content=raw.encode(),
        headers={'content-type': 'text/markdown', 'x-attachment-filename': 'meeting.md'})
    assert response.status_code == 200, response.text
    assert not library.discover().skills or all(item.skill_id != 'meeting-notes' for item in library.discover().skills)
    attachment_id = response.json()['id']
    first = client.post(f'/api/roles/{role_id}/messages', json={'content': '请把附件保存成当前角色的可复用技能', 'attachments': [attachment_id]})
    assert '已实际保存并读回' in first.text
    results = [item for item in sessions.runtime_messages(f'role:{role_id}') if isinstance(item, ToolResultMessage)]
    assert results[-1].details['skillValidation']['verified'] is True
    second = client.post(f'/api/roles/{role_id}/messages', json={'content': '整理这次讨论的议题和决定'})
    assert '复用会议方法完成整理' in second.text


def test_management_roundtrip_crlf_resources_overrides_publish_and_rollback(tmp_path, monkeypatch):
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            yield ModelTextDelta('完成')

    client, role_id, roles, _, library = setup(tmp_path, monkeypatch, Adapter())
    raw = method('weather', body='角色天气说明，参考 references/source.md。').replace('\n', '\r\n')
    response = client.post(f'/api/roles/{role_id}/skills', json={'content': raw,
        'resources': {'references/source.md': {'encoding': 'base64', 'content': 'aGVsbG8='}}})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['content'] == raw and saved['contentHash'] == hashlib.sha256(raw.encode()).hexdigest()
    assert saved['saved'] and saved['verified'] and saved['problems'] == []
    listed = client.get(f'/api/roles/{role_id}/skills').json()
    weather = [item for item in listed['skills'] if item['id'] == 'weather']
    assert [(item['source'], item['status']) for item in weather] == [('role', 'available'), ('builtin', 'shadowed')]
    edit = client.post(f'/api/roles/{role_id}/skills', json={'content': raw + '\r\n新核验。', 'ref': saved['ref'], 'expectedHash': saved['contentHash']})
    assert edit.status_code == 200 and edit.json()['problems'] == []
    conflict = client.post(f'/api/roles/{role_id}/skills', json={'content': raw, 'ref': saved['ref'], 'expectedHash': saved['contentHash']})
    assert conflict.status_code == 409
    saved = edit.json()
    published = client.post(f'/api/roles/{role_id}/skills/publish', json={'ref': saved['ref'], 'expectedHash': saved['contentHash']})
    assert published.status_code == 200 and published.json()['source'] == 'shared'
    assert (tmp_path / 'data/shared-skills/weather/references/source.md').read_bytes() == b'hello'
    assert not (tmp_path / 'project/.agents/skills').exists()
    another = client.post('/api/roles', json={'name': '公共复用者', 'profile': {'profile': '独立'}}).json()['role']['id']
    other = client.get(f'/api/roles/{another}/skills').json()
    assert next(item for item in other['skills'] if item['id'] == 'weather' and item['effective'])['source'] == 'shared'
    library.update_settings(ref=saved['ref'], active=False)
    effective = next(item for item in library.describe()['skills'] if item['id'] == 'weather' and item['effective'])
    assert effective['source'] == 'shared'
    enable(client, role_id)
    state = library.settings()
    rollback = client.put(f'/api/roles/{role_id}/skills/settings', json={'enabled': False, 'expectedRevision': state['revision']})
    assert rollback.status_code == 200
    assert (roles.workspace_path(role_id) / 'skills/weather/SKILL.md').read_bytes() == saved['content'].encode()


def test_catalog_pagination_manual_omission_dependency_and_resource_errors(tmp_path, monkeypatch):
    client, role_id, _, _, library = setup(tmp_path, monkeypatch, object())
    for index in range(35):
        library.save(method(f'method-{index:02}', body='不要预先注入正文'))
    manual = library.save(method('private-manual', manual=True))
    valid = library.save(method('resource-method', body='先读 references/input.md'), resources={'references/input.md': '精确资源正文'})
    enable(client, role_id)
    state = SkillRun(library, role_id=role_id, session_key=f'role:{role_id}', run_id='test', prompt='', capacity=8000)
    first = state.page(initial=True, limit=30)
    assert first['incomplete'] and first['nextOffset'] < 30
    assert manual['ref'] not in json.dumps(first)
    all_ids = []
    offset = 0
    while True:
        page = state.page(offset=offset, limit=10)
        all_ids.extend(item['id'] for item in page['skills'])
        if page['nextOffset'] is None:
            break
        offset = page['nextOffset']
    assert len(set(all_ids)) == len(all_ids) and {f'method-{index:02}' for index in range(35)} <= set(all_ids)
    state.read(valid['ref'])
    assert '精确资源正文' == state.read(valid['ref'], 'resource-method/references/input.md')['content']
    for forbidden in ('../SKILL.md', 'D:/private.key', 'resource-method/references/../../private.key'):
        with pytest.raises(ValueError, match='authorized resource index'):
            state.read(valid['ref'], forbidden)


def test_real_loaded_receipts_recover_after_window_removal_without_business_replay(tmp_path, monkeypatch):
    _, role_id, roles, _, library = setup(tmp_path, monkeypatch, object())
    saved = library.save(method(body='窗口恢复必需的具体步骤'))
    library.update_settings(enabled=True)
    state = SkillRun(library, role_id=role_id, session_key=f'role:{role_id}', run_id='test', prompt='')
    result = state.read(saved['ref'])
    receipt = ToolResultMessage('read', 'skill_read', result['content'], details={key: value for key, value in result.items() if key != 'content'})
    captured = state.capture()
    assert '窗口恢复必需的具体步骤' not in state.prompt_for([receipt], captured)
    restored = state.prompt_for([UserMessage('工作摘要：已使用技能')], captured)
    assert restored.count('窗口恢复必需的具体步骤') == 1
    assert state.read(saved['ref'])['alreadyLoaded'] is True
    assert len(state.facts) == 1
    # A new run can reuse an unchanged authorized real receipt still visible.
    next_state = SkillRun(library, role_id=role_id, session_key=f'role:{role_id}', run_id='next', prompt='')
    next_state.commit_window([receipt])
    assert next_state.read(saved['ref'])['alreadyLoaded'] is True
    assert next_state.facts[0]['mode'] == 'reused'
    # Changing the file cannot replace the old loaded body in this run.
    path = roles.workspace_path(role_id) / 'skills/briefing/SKILL.md'
    path.write_text(method(body='下一轮的新版本'), encoding='utf-8')
    with pytest.raises(ValueError, match='changed after discovery'):
        state.read(saved['ref'])
    assert '窗口恢复必需的具体步骤' not in state.prompt_for([], state.capture())
    fresh = SkillRun(library, role_id=role_id, session_key=f'role:{role_id}', run_id='fresh', prompt='')
    assert '下一轮的新版本' in fresh.read(saved['ref'])['content']
    library.update_settings(ref=saved['ref'], active=False)
    with pytest.raises(ValueError, match='disabled, removed or overridden'):
        fresh.read(saved['ref'])
    assert '下一轮的新版本' not in fresh.prompt_for([], fresh.capture())


@pytest.mark.parametrize('header,error', [
    ('name: simple\ndescription: 简单方法\nallow_implicit_invocation: yes', None),
    ('name: simple\ndescription: 简单方法\nactivation: explicit\nallow_implicit_invocation: true', 'conflicts'),
    ('name: simple\ndescription: 简单方法\nallow_implicit_invocation: "false"', 'boolean'),
    ('name: simple\ndescription: 简单方法\nmetadata: {tools: [shell], mcp: secret}', None),
])
def test_codex_format_is_typed_and_external_metadata_never_grants_permissions(header, error):
    raw = f'---\n{header}\n---\n具体输入、步骤与核验。'
    if error:
        with pytest.raises(ValueError, match=error):
            validate_skill_text(raw)
    else:
        parsed = validate_skill_text(raw)
        assert parsed['id'] == 'simple'
        if 'metadata:' in raw:
            assert parsed['ignoredMetadata'] == ['metadata']
