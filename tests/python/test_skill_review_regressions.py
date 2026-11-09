"""Public boundary regressions for the #336 independent review repairs."""
import asyncio
import base64
import io
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace

import pytest
from PIL import Image

from backend.app import main
from backend.app.agent_runtime.skill_discovery import SkillRun
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from backend.app.model_adapter import ModelTextDelta, ModelToolCall, ModelResponseDone
from test_skill_discovery import setup, method, enable
from test_spawn_api import api, shell_api  # noqa: F401

pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.mark.parametrize('content,explicit,authorized', [
    ('解释 `$manual` 这个示例', [], False),
    ('解释 ``$manual `literal` `` 这个示例', [], False),
    ('    $manual\n解释上面的例子', [], False),
    ('\t$manual\n解释上面的例子', [], False),
    ('   ```text\n$manual\n   ```\n解释例子', [], False),
    ('````text\n```\n$manual\n````\n解释例子', [], False),
    ('请用 $manual 完成工作', [], True),
    ('解释 `$manual` 这个示例', ['manual'], True),
])
def test_public_manual_activation_ignores_markdown_code_but_preserves_direct_calls(tmp_path, monkeypatch, content, explicit, authorized):
    saved = None
    requests = []

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            if len(requests) == 1:
                assert ('MANUAL_PRIVATE_BODY' in json.dumps(messages)) is authorized
                yield ModelToolCall('manual-read', 'skill_read', {'ref': saved['ref']})
                yield ModelResponseDone('tool_use')
            else:
                result = next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                assert ('manual-only skill requires explicit activation' in result) is not authorized
                yield ModelTextDelta('边界已核验')

    client, role_id, _, _, library = setup(tmp_path, monkeypatch, Adapter())
    saved = library.save(method('manual', manual=True, body='MANUAL_PRIVATE_BODY'))
    enable(client, role_id)
    response = client.post(f'/api/roles/{role_id}/messages', json={'content': content, 'skillIds': explicit})
    assert '边界已核验' in response.text, response.text


def image_state(tmp_path, monkeypatch):
    _, role_id, _, sessions, library = setup(tmp_path, monkeypatch, object())
    buffer = io.BytesIO()
    Image.new('RGB', (3, 2), 'yellow').save(buffer, 'PNG')
    raw = buffer.getvalue()
    saved = library.save(method(body='查看 assets/reference.png'), resources={'assets/reference.png': {
        'encoding': 'base64', 'content': base64.b64encode(raw).decode()}})
    library.update_settings(enabled=True)
    def start(run_id):
        return SkillRun(library, role_id=role_id, session_key=f'role:{role_id}', run_id=run_id, prompt='')
    first = start('first')
    body = first.read(saved['ref'])
    image = first.read(saved['ref'], 'briefing/assets/reference.png')
    receipts = [ToolResultMessage('body', 'skill_read', body['content'], details={'skillRead': body['skillRead']}),
        ToolResultMessage('image', 'skill_read', image['content'], details={'skillRead': image['skillRead']}, images=image['images'])]
    return role_id, sessions, start, saved, receipts, raw


def test_next_turn_persisted_image_receipt_cannot_replace_real_image_delivery(tmp_path, monkeypatch):
    role_id, sessions, start, saved, receipts, raw = image_state(tmp_path, monkeypatch)
    sessions.open_role_session(role_id)
    for receipt in receipts:
        sessions.append_agent_message(f'role:{role_id}', AssistantMessage(tool_calls=(ToolCall(receipt.tool_call_id, 'skill_read'),)))
        sessions.append_agent_message(f'role:{role_id}', receipt)
    history = sessions.runtime_messages(f'role:{role_id}')
    assert all(not item.images for item in history if isinstance(item, ToolResultMessage))
    second = start('second')
    second.commit_window(history)
    second.read(saved['ref'])
    result = second.read(saved['ref'], 'briefing/assets/reference.png')
    assert not result.get('alreadyLoaded')
    assert base64.b64decode(result['images'][0].data_base64) == raw
    assert all('images' not in fact for fact in second.facts)


def test_adopted_live_image_is_read_again_when_compaction_removes_its_blocks(tmp_path, monkeypatch):
    from backend.app.advanced_settings import AdvancedSettings
    from backend.app.agent_runtime.context_provider import ContextWindowProvider
    from backend.app.agent_runtime.meido_provider import MeidoProvider
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.context_windows import ContextWindowStore
    from backend.app.models import ModelConfiguration
    from test_context_windows import Adapter
    role_id, sessions, start, saved, receipts, raw = image_state(tmp_path, monkeypatch)
    second = start('second')
    second.commit_window(receipts)
    second.read(saved['ref'])
    assert second.read(saved['ref'], 'briefing/assets/reference.png')['alreadyLoaded']
    key = f'role:{role_id}'
    sessions.open_role_session(role_id)
    sessions.append_message(key, 'user', '查看方法与图片')
    for receipt in receipts:
        sessions.append_agent_message(key, AssistantMessage(tool_calls=(ToolCall(receipt.tool_call_id, 'skill_read'),)))
        sessions.append_agent_message(key, receipt)
    sessions.append_message(key, 'assistant', '图片已查看')
    for _ in range(8):
        sessions.append_message(key, 'user', 'more context ' * 300)
        sessions.append_message(key, 'assistant', 'verified result ' * 300)
    sessions.append_message(key, 'user', '继续，图片需要重新核验')
    messages = sessions.runtime_messages(key, max_messages=None)
    # The first receipt is live in this run, with real source IDs for eviction.
    messages = [replace(item, images=receipts[1].images) if isinstance(item, ToolResultMessage)
        and item.tool_call_id == 'image' else item for item in messages]
    config = ModelConfiguration(providerId='custom', provider='fixture', baseUrl='http://localhost', model='fixture',
        contextWindowTokens=32000, modelAutoCompactTokenLimit=16000)
    settings = AdvancedSettings(maxTokens=1024, summaryTokenLimit=512, contextSafetyMarginTokens=100,
        contextTriggerRatio=.6, contextTargetRatio=.4)
    base = MeidoProvider(Adapter(), main.store.get(role_id), config, max_response_tokens=1024, skill_state=second)
    windows = ContextWindowStore(sessions.database_path)
    async def compact():
        async def covered(ids):
            return True
        provider = ContextWindowProvider(base, settings, windows, role_id=role_id, session_key=key,
            scope='owner', authority='image-repair', authorized=lambda: True, ensure_coverage=covered)
        _ = [event async for event in provider.stream_response(model='fixture', system='', messages=messages,
            tools=[], signal=CancellationToken())]
        return provider
    provider = asyncio.run(compact())
    assert provider.compactions == 1
    removed = set(windows.load(role_id, key, 'owner', 'image-repair')['removed'])
    assert removed.intersection(next(item.source_message_ids for item in messages
        if isinstance(item, ToolResultMessage) and item.tool_call_id == 'image'))
    result = second.read(saved['ref'], 'briefing/assets/reference.png')
    assert not result.get('alreadyLoaded')
    assert base64.b64decode(result['images'][0].data_base64) == raw


@pytest.mark.parametrize('mode', ['blind', 'stale', 'current', 'edit-stale', 'edit-current', 'resource-stale', 'resource-current'])
def test_chat_skill_updates_use_the_current_run_read_version(tmp_path, monkeypatch, mode):
    client = saved = library = None
    requests = 0
    new = method(body='CHAT_NEW_BODY')
    target = 'skills/briefing/references/input.md' if mode.startswith('resource-') else 'skills/briefing/SKILL.md'

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            nonlocal requests
            requests += 1
            if requests == 1 and mode != 'blind':
                yield ModelToolCall('read-version', 'read_file', {'path': target})
                yield ModelResponseDone('tool_use')
            elif requests == (1 if mode == 'blind' else 2):
                if mode in {'stale', 'edit-stale', 'resource-stale'}:
                    response = client.post(f'/api/roles/{role_id}/skills', json={'ref': saved['ref'],
                        'expectedHash': saved['contentHash'], 'content': method(body='ORIGINAL_BODY\nOWNER_APPEND' if mode == 'edit-stale' else 'OWNER_BODY'),
                        **({'resources': {'references/input.md': 'OWNER_RESOURCE'}} if mode == 'resource-stale' else {})})
                    assert response.status_code == 200, response.text
                if mode.startswith('edit-'):
                    yield ModelToolCall('update', 'edit_file', {'path': target, 'old_text': 'ORIGINAL_BODY', 'new_text': 'CHAT_NEW_BODY'})
                else:
                    yield ModelToolCall('update', 'write_file', {'path': target,
                        'content': 'CHAT_NEW_RESOURCE' if mode.startswith('resource-') else new})
                yield ModelResponseDone('tool_use')
            else:
                result = next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                if mode.endswith('current'):
                    assert 'conflict' not in result.casefold()
                else:
                    assert 'conflict' in result.casefold()
                yield ModelTextDelta('更新边界已核验')

    client, role_id, roles, _, library = setup(tmp_path, monkeypatch, Adapter())
    saved = library.save(method(body='ORIGINAL_BODY'), resources={'references/input.md': 'ORIGINAL_RESOURCE'})
    response = client.post(f'/api/roles/{role_id}/messages', json={'content': '更新已有方法，保留并发修改'})
    assert '更新边界已核验' in response.text, response.text
    actual = (roles.workspace_path(role_id) / target).read_text(encoding='utf-8')
    assert actual == ('OWNER_RESOURCE' if mode == 'resource-stale' else method(body='OWNER_BODY') if mode == 'stale'
        else method(body='ORIGINAL_BODY\nOWNER_APPEND') if mode == 'edit-stale' else 'CHAT_NEW_RESOURCE' if mode == 'resource-current'
        else new if mode in {'current', 'edit-current'} else method(body='ORIGINAL_BODY'))


def test_prior_run_file_receipts_cannot_authorize_current_chat_skill_updates(tmp_path, monkeypatch):
    requests = 0
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield ModelToolCall('read-old-run', 'read_file', {'path': 'skills/briefing/SKILL.md'})
                yield ModelResponseDone('tool_use')
            elif requests == 2:
                yield ModelTextDelta('本轮已读取')
            elif requests == 3:
                assert 'ORIGINAL_BODY' in json.dumps(messages)
                yield ModelToolCall('write-new-run', 'write_file', {'path': 'skills/briefing/SKILL.md', 'content': method(body='BLIND_NEW_BODY')})
                yield ModelResponseDone('tool_use')
            else:
                assert 'conflict' in next(item['content'] for item in reversed(messages) if item['role'] == 'tool').casefold()
                yield ModelTextDelta('本轮需要重新读取')
    client, role_id, roles, _, library = setup(tmp_path, monkeypatch, Adapter())
    library.save(method(body='ORIGINAL_BODY'))
    first = client.post(f'/api/roles/{role_id}/messages', json={'content': '先读取方法'})
    assert '本轮已读取' in first.text, first.text
    second = client.post(f'/api/roles/{role_id}/messages', json={'content': '用旧历史直接覆盖方法'})
    assert '本轮需要重新读取' in second.text, second.text
    assert (roles.workspace_path(role_id) / 'skills/briefing/SKILL.md').read_text(encoding='utf-8') == method(body='ORIGINAL_BODY')


def test_skill_chat_commit_and_management_save_share_one_version_transaction(tmp_path, monkeypatch):
    from backend.app import skill_library
    saved = None
    requests = 0
    ready, resume, owner_entered = threading.Event(), threading.Event(), threading.Event()
    new = method(body='CHAT_COMMITTED_BODY')

    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield ModelToolCall('read', 'read_file', {'path': 'skills/briefing/SKILL.md'})
                yield ModelResponseDone('tool_use')
            elif requests == 2:
                yield ModelToolCall('commit', 'write_file', {'path': 'skills/briefing/SKILL.md', 'content': new})
                yield ModelResponseDone('tool_use')
            else:
                yield ModelTextDelta('聊天版本已提交')

    client, role_id, roles, _, library = setup(tmp_path, monkeypatch, Adapter())
    saved = library.save(method(body='ORIGINAL_BODY'))
    replace_file, save = skill_library.os.replace, skill_library.SkillLibrary.save

    def paused_replace(source, destination):
        if main.Path(source).name.startswith('.meido-') and main.Path(destination).name == 'SKILL.md':
            ready.set()
            assert resume.wait(10)
        return replace_file(source, destination)

    def management_save(self, *args, **kwargs):
        owner_entered.set()
        return save(self, *args, **kwargs)

    monkeypatch.setattr(skill_library.os, 'replace', paused_replace)
    monkeypatch.setattr(skill_library.SkillLibrary, 'save', management_save)
    with ThreadPoolExecutor(max_workers=2) as executor:
        chat = executor.submit(client.post, f'/api/roles/{role_id}/messages', json={'content': '更新技能'})
        try:
            assert ready.wait(10)
            owner = executor.submit(client.post, f'/api/roles/{role_id}/skills', json={
                'ref': saved['ref'], 'expectedHash': saved['contentHash'], 'content': method(body='OWNER_NEW_BODY')})
            assert owner_entered.wait(10)
            try:
                owner.result(timeout=.2)
            except TimeoutError:
                pass
        finally:
            resume.set()
        assert '聊天版本已提交' in chat.result(timeout=10).text
        assert owner.result(timeout=10).status_code == 409
    assert (roles.workspace_path(role_id) / 'skills/briefing/SKILL.md').read_text(encoding='utf-8') == new


@pytest.mark.parametrize('boundary', ['licensed', 'unlicensed', 'revoked', 'scope-revoked', 'private-shadow'])
def test_model_selected_builtin_author_records_only_current_licensed_provenance(api, tmp_path, monkeypatch, boundary):
    from backend.app.plugin_configuration import PluginConfigurationStore
    client, roles, sessions, _ = api
    monkeypatch.setattr(main, 'data_root', tmp_path / 'data')
    monkeypatch.setattr(main, '_proactive', None)
    plugins = PluginConfigurationStore(tmp_path / 'plugins.json')
    state = plugins.prepare('autonomy', expected_revision=0, enabled=True)
    plugins.finish('autonomy', state.revision)
    monkeypatch.setattr(main, 'plugin_configuration', plugins)
    role = client.post('/api/roles', json={'name': '按需长期作者', 'profile': {'profile': '本地夹具'}}).json()['role']
    role_id = role['id']
    path = f'/api/roles/{role_id}/autonomy'
    if boundary != 'unlicensed':
        response = client.put(path, json={'expectedRevision': 0, 'config': {'driftAuthoring': {'enabled': True,
            'allowedTools': ['read_file', 'write_file', 'edit_file', 'finish_drift'], 'allowMcp': True}}})
        assert response.status_code == 200, response.text
    library = main._skill_library(role_id)
    if boundary == 'private-shadow':
        original = (main.Path(__file__).parents[2] / 'backend/app/builtin_skills/create-drift-skill/SKILL.md').read_text(encoding='utf-8')
        private = library.save(original)
        library.update_settings(ref=private['ref'], policy='automatic')
    library.update_settings(enabled=True)

    class Author:
        requests = 0
        files = None
        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests += 1
            if self.requests == 1:
                ref = re.search(r'skill:(?:builtin|role):create-drift-skill:[a-f0-9]{16}', json.dumps(messages)).group()
                yield ModelToolCall('choose-method', 'skill_read', {'ref': ref})
                yield ModelResponseDone('tool_use')
            else:
                if self.files is None:
                    instructions = next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                    self.files = [(name, content + '\n') for name, content in re.findall(
                        r'### `([^`]+)`\s+```(?:json|markdown|text)\n(.*?)\n```', instructions, re.S)]
                    assert len(self.files) == 3
                    if boundary == 'revoked':
                        library.update_settings(enabled=False)
                    if boundary == 'scope-revoked':
                        config = client.get(path).json()['config']
                        revoked = client.put(path, json={'expectedRevision': config['revision'],
                            'config': {**config, 'driftAuthoring': {**config['driftAuthoring'], 'enabled': False}}})
                        assert revoked.status_code == 200, revoked.text
                index = self.requests - 2
                if index < len(self.files):
                    name, content = self.files[index]
                    yield ModelToolCall(f'author-{index}', 'write_file', {'path': name, 'content': content})
                    yield ModelResponseDone('tool_use')
                else:
                    yield ModelTextDelta('产物已实际写入')

    monkeypatch.setattr(main, 'model_adapter', Author())
    try:
        response = client.post(f'/api/roles/{role_id}/messages', json={'content': '帮我保存一个能在以后空闲时逐次推进的读书笔记任务'})
        assert '产物已实际写入' in response.text, response.text
        catalog = client.get(path + '/drift-skills').json()['skills']
        skill = next(item for item in catalog if item['id'] == 'reading-notes')
        assert (skill.get('provenance', {}).get('status') == 'verified') is (boundary == 'licensed'), skill
        run_id = sessions.list_messages(f'role:{role_id}')[-1].runId
        if boundary == 'licensed':
            assert skill['provenance']['runId'] == run_id
            assert skill['provenance']['authorSkillHash']
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())


@pytest.mark.parametrize('requested_path', ['skills/SKILL.md',
    pytest.param('skills/skill.md', marks=pytest.mark.skipif(os.name != 'nt', reason='Windows case alias')),
    pytest.param('SKILLS/Skill.Md', marks=pytest.mark.skipif(os.name != 'nt', reason='Windows case alias'))])
def test_root_skill_headers_and_windows_aliases_keep_version_and_format_guards(tmp_path, monkeypatch, requested_path):
    requests = 0
    before, after = method('root-method', body='ROOT_ORIGINAL'), method('root-method', body='ROOT_UPDATED')
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield ModelToolCall('blind', 'write_file', {'path': requested_path, 'content': after})
                yield ModelResponseDone('tool_use')
            elif requests == 2:
                assert 'conflict' in next(item['content'] for item in reversed(messages) if item['role'] == 'tool').casefold()
                yield ModelToolCall('read-root', 'read_file', {'path': requested_path})
                yield ModelResponseDone('tool_use')
            elif requests == 3:
                assert 'saved-file validation: verified' in next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelToolCall('invalid-header', 'write_file', {'path': requested_path, 'content': 'not a skill'})
                yield ModelResponseDone('tool_use')
            elif requests == 4:
                assert 'front matter' in next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelToolCall('current-update', 'write_file', {'path': requested_path, 'content': after})
                yield ModelResponseDone('tool_use')
            else:
                assert 'Wrote' in next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('根级文件边界已核验')
    client, role_id, roles, _, _ = setup(tmp_path, monkeypatch, Adapter())
    root = roles.workspace_path(role_id) / 'skills'
    root.mkdir()
    (root / 'SKILL.md').write_text(before, encoding='utf-8')
    response = client.post(f'/api/roles/{role_id}/messages', json={'content': '核验根级技能更新'})
    assert '根级文件边界已核验' in response.text, response.text
    assert (root / 'SKILL.md').read_text(encoding='utf-8') == after


def test_paired_channel_owner_code_keeps_its_original_indentation_before_manual_auth(api, tmp_path, monkeypatch):
    from backend.app.channels import ChannelHost
    from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
    from backend.app.channels.group_tools import MultichannelGroupRepository
    client, roles, sessions, _ = api
    monkeypatch.setattr(main, 'data_root', tmp_path / 'data')
    role = client.post('/api/roles', json={'name': '渠道代码边界', 'profile': {'profile': '本地夹具'}}).json()['role']
    library = main._skill_library(role['id'])
    saved = library.save(method('manual', manual=True, body='CHANNEL_MANUAL_BODY'))
    library.update_settings(enabled=True)
    channels = main.multichannel_store
    account = channels.register_channel_account(role['id'], 'qq', account_id='code-owner', enabled=True)
    code = channels.create_pairing_code(role['id'], 'qq')
    assert channels.consume_pairing_code(code, role_id=role['id'], channel='qq', scope='platform',
        platform_user_id='11', account_id=account.account_id, chat_id='11')
    delivered, requests = [], []
    async def transport(operation, params):
        assert operation == 'send_private_msg'
        delivered.append(params)
        return {'message_id': 'delivered-code-result'}
    async def no_avatar(*args, **kwargs):
        return None
    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
    host = ChannelHost(channels)
    host.register(adapter)
    monkeypatch.setattr(main, 'channel_host', host)
    monkeypatch.setattr(main, 'channel_adapters', {account.account_id: adapter})
    monkeypatch.setattr(main, 'channel_event_locks', {})
    monkeypatch.setattr(main, 'group_repository', MultichannelGroupRepository(channels))
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            if len(requests) == 1:
                assert 'CHANNEL_MANUAL_BODY' not in json.dumps(messages)
                yield ModelToolCall('code-is-not-consent', 'skill_read', {'ref': saved['ref']})
                yield ModelResponseDone('tool_use')
            else:
                assert 'manual-only skill requires explicit activation' in next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('代码示例没有授予手动技能许可')
    monkeypatch.setattr(main, 'model_adapter', Model())
    async def scenario():
        try:
            await main._dispatch_channel_payload(account.account_id, {'post_type': 'message', 'message_type': 'private',
                'self_id': 99, 'user_id': 11, 'message_id': 'code-sample', 'sender': {'user_id': 11, 'nickname': '主人'},
                'message': [{'type': 'text', 'data': {'text': '    $manual\n解释这个示例'}}]}, source_adapter=adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    assert len(requests) == 2 and delivered
    thread = channels.get_thread_by_chat(role['id'], account.account_id, '11', chat_type='private')
    user = next(item for item in sessions.list_messages(thread.session_key) if item.role == 'user')
    assert '_skillInvocationText' not in user.metadata and 'hostSkillInvocationText' not in user.metadata
