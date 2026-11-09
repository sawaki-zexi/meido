import asyncio
import base64
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.app import main
from backend.app.agent_runtime.capabilities import CapabilityRegistry
from backend.app.agent_runtime.context_provider import ContextWindowProvider
from backend.app.agent_runtime.loop import run_agent_loop
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.skill_discovery import SkillRun, SkillReadTool
from backend.app.agent_runtime.skills import SkillRegistry, explicit_skill_ids
from backend.app.agent_runtime.tools import ToolContext
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from backend.app.skill_library import SkillLibrary
from test_context_windows import setup_window
from test_skill_discovery import setup, method, enable

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_real_window_compaction_restores_only_actual_guidance_and_respects_revocation(tmp_path):
    sessions, adapter, base, windows, settings = setup_window(tmp_path / 'window')

    class Roles:
        root = tmp_path / 'roles'
        def get(self, role_id):
            return base.role if role_id == 'r' else None

    library = SkillLibrary(Roles(), 'r', tmp_path / 'shared', tmp_path / 'project', tmp_path / 'builtin')
    saved = library.save(method(body='RESTORE_THE_VERIFIED_METHOD_STEPS'))
    library.update_settings(enabled=True)
    allowed = True
    state = SkillRun(library, role_id='r', session_key='role:r', run_id='run', prompt='', authorized=lambda: allowed)
    base.skill_state = state
    result = state.read(saved['ref'])
    sessions.append_agent_message('role:r', AssistantMessage(tool_calls=(ToolCall('method', 'skill_read', {'ref': saved['ref']}),)))
    sessions.append_agent_message('role:r', ToolResultMessage('method', 'skill_read', result['content'],
        details={key: value for key, value in result.items() if key != 'content'}))
    for _ in range(8):
        sessions.append_message('role:r', 'user', 'new work ' * 100)
        sessions.append_message('role:r', 'assistant', 'verified result ' * 100)
    sessions.append_message('role:r', 'user', '继续处理已经读过的方法')
    base.configuration = base.configuration.model_copy(update={'contextWindowTokens': 16000, 'modelAutoCompactTokenLimit': 4000})
    capabilities = CapabilityRegistry([SkillReadTool(state)]).resolve(role_id='r', session_key='role:r', run_id='run')

    async def execute():
        async def covered(ids):
            return True
        provider = ContextWindowProvider(base, settings, windows, role_id='r', session_key='role:r',
            scope='owner', authority='a', authorized=lambda: True, ensure_coverage=covered)
        events = [event async for event in run_agent_loop(provider=provider, model='fixture', system='',
            messages=sessions.runtime_messages('role:r', max_messages=None), capabilities=capabilities)]
        return provider, events

    provider, events = asyncio.run(execute())
    assert events[-1].reason == 'completed' and provider.compactions == 1
    assert 'RESTORE_THE_VERIFIED_METHOD_STEPS' in str(adapter.calls[-1][0])
    assert str(adapter.calls[-1][0]).count('RESTORE_THE_VERIFIED_METHOD_STEPS') == 1
    assert len(state.facts) == 1  # restoration never executes another tool
    removed = set(windows.load('r', 'role:r', 'owner', 'a')['removed'])
    receipt = next(item for item in sessions.runtime_messages('role:r') if isinstance(item, ToolResultMessage))
    assert removed.intersection(receipt.source_message_ids)
    allowed = False
    asyncio.run(execute())
    assert 'RESTORE_THE_VERIFIED_METHOD_STEPS' not in str(adapter.calls[-1][0])
    with pytest.raises(ValueError, match='authorization was revoked'):
        state.read(saved['ref'])


def test_guest_and_untriggered_owner_have_no_new_skill_schemas(tmp_path, monkeypatch):
    _, role_id, roles, _, library = setup(tmp_path, monkeypatch, object())
    library.save(method())
    library.update_settings(enabled=True)
    role = roles.get(role_id)

    async def run():
        external = await main._runtime_capabilities(role, session_key=f'role:{role_id}', run_id='guest',
            external_only=True, inspection=True)
        idle = await main._runtime_capabilities(role, session_key=f'role:{role_id}', run_id='idle',
            available_tools=(), inspection=True)
        for result in (external, idle):
            assert not {'skill_read', 'skill_list'} & {item['function']['name'] for item in result.provider_schemas()}
            assert result._skill_state is None
            await result.close()
    asyncio.run(run())


def test_migration_does_not_force_keywords_and_rollback_restores_previous_behavior(tmp_path, monkeypatch):
    _, role_id, roles, _, library = setup(tmp_path, monkeypatch, object())
    legacy = '---\nid: old-keyword\nversion: 1\ndescription: 检查日报\nactivation: keyword\n---\nOLD_KEYWORD_INSTRUCTIONS'
    library.save(legacy)
    manual = library.save('---\nid: old-manual\nversion: 1\ndescription: 检查日报\nactivation: explicit\n---\nOLD_MANUAL_INSTRUCTIONS')
    constant = library.save('---\nid: old-constant\nversion: 1\ndescription: 常驻\nactivation: role_default\n---\nCONSTANT_INSTRUCTIONS')
    preview = library.describe()
    assert {item['from']: item['to'] for item in preview['migration'] if item['name'] in {'old-keyword', 'old-manual', 'old-constant'}} == {
        'keyword': 'automatic', 'explicit': 'manual', 'role_default': 'constant'}

    async def resolve():
        return await main._runtime_capabilities(roles.get(role_id), session_key=f'role:{role_id}', run_id='',
            prompt_text='检查日报', inspection=True)
    before = asyncio.run(resolve())
    assert 'OLD_KEYWORD_INSTRUCTIONS' in before.prompt_context()
    library.update_settings(enabled=True)
    after = asyncio.run(resolve())
    prompt = after._skill_state.prompt_for([], after._skill_state.capture())
    assert 'OLD_KEYWORD_INSTRUCTIONS' not in prompt and 'OLD_MANUAL_INSTRUCTIONS' not in prompt
    assert 'CONSTANT_INSTRUCTIONS' in prompt
    with pytest.raises(ValueError, match='manual-only'):
        after._skill_state.read(manual['ref'])
    library.update_settings(enabled=False)
    rollback = asyncio.run(resolve())
    assert 'OLD_KEYWORD_INSTRUCTIONS' in rollback.prompt_context()
    assert 'OLD_MANUAL_INSTRUCTIONS' not in rollback.prompt_context()
    for resolution in (before, after, rollback):
        asyncio.run(resolution.close())


def test_skill_changes_disabled_stop_and_body_budget_return_concrete_failures(tmp_path, monkeypatch):
    _, role_id, roles, _, library = setup(tmp_path, monkeypatch, object())
    huge = library.save(method('huge', body='具体步骤' * 2000))
    script = library.save(method('safe', body='脚本仅作资料 scripts/example.py'), resources={'scripts/example.py': 'raise RuntimeError("must never execute")'})
    library.update_settings(enabled=True)
    allowed = True
    state = SkillRun(library, role_id=role_id, session_key=f'role:{role_id}', run_id='run', prompt='', capacity=4000,
        authorized=lambda: allowed)
    with pytest.raises(ValueError, match='read budget'):
        state.read(huge['ref'])
    state.read(script['ref'])
    assert 'must never execute' in state.read(script['ref'], 'safe/scripts/example.py')['content']
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RuntimeError, match='cancelled'):
        asyncio.run(SkillReadTool(state).execute({'ref': script['ref']}, ToolContext(role_id, f'role:{role_id}', 'run', token)))
    path = roles.workspace_path(role_id) / 'skills/safe/scripts/example.py'
    path.write_text('modified', encoding='utf-8')
    with pytest.raises(ValueError, match='changed after discovery'):
        state.read(script['ref'])
    allowed = False
    assert not state.capture().guidance


def test_quoted_samples_and_attachments_cannot_create_manual_activation():
    assert explicit_skill_ids('使用 $direct 完成工作\n> 他写了 $quoted\n```md\n$sample\n```\n“$quotation”') == {'direct'}


def test_inflight_policy_can_revoke_but_cannot_expand_original_manual_scope(tmp_path, monkeypatch):
    _, role_id, _, _, library = setup(tmp_path, monkeypatch, object())
    saved = library.save(method(manual=True))
    library.update_settings(enabled=True)
    def start(run_id, explicit=()):
        return SkillRun(library, role_id=role_id, session_key=f'role:{role_id}', run_id=run_id, prompt='', explicit_ids=explicit)
    manual = start('manual')
    library.update_settings(ref=saved['ref'], policy='automatic')
    with pytest.raises(ValueError, match='manual-only'):
        manual.read(saved['ref'])
    assert saved['ref'] not in json.dumps(manual.page())
    automatic = start('automatic')
    assert '真实简报步骤' in automatic.read(saved['ref'])['content']
    library.update_settings(ref=saved['ref'], policy='manual')
    with pytest.raises(ValueError, match='manual-only'):
        automatic.read(saved['ref'])
    assert not automatic.capture().guidance
    explicit = start('explicit', ['briefing'])
    assert explicit.read(saved['ref'])['alreadyLoaded'] is True


def test_invalid_save_preserves_previous_content_and_settings_conflicts_are_specific(tmp_path, monkeypatch):
    client, role_id, roles, _, library = setup(tmp_path, monkeypatch, object())
    saved = library.save(method())
    previous = (roles.workspace_path(role_id) / 'skills/briefing/SKILL.md').read_bytes()
    result = client.post(f'/api/roles/{role_id}/skills', json={'ref': saved['ref'], 'expectedHash': saved['contentHash'],
        'content': '---\nname: broken\n---\n缺少描述'})
    assert result.status_code == 422 and 'description' in result.json()['detail']
    assert (roles.workspace_path(role_id) / 'skills/briefing/SKILL.md').read_bytes() == previous
    library.update_settings(enabled=True)
    result = client.put(f'/api/roles/{role_id}/skills/settings', json={'enabled': False, 'expectedRevision': 0})
    assert result.status_code == 409 and '冲突' in result.json()['detail']
    assert library.settings()['enabled'] is True


def test_management_and_model_use_same_role_aware_dependency_diagnostics(tmp_path, monkeypatch):
    client, role_id, roles, _, library = setup(tmp_path, monkeypatch, object())
    monkeypatch.setattr('shutil.which', lambda name: 'available-fixture-bin')
    listed = client.get(f'/api/roles/{role_id}/skills').json()
    codex = next(item for item in listed['skills'] if item['id'] == 'codex-delegate')
    assert codex['status'] == 'unavailable' and '编程委托尚未启用' in str(codex['problems'])
    library.update_settings(enabled=True)
    async def run():
        result = await main._runtime_capabilities(roles.get(role_id), session_key=f'role:{role_id}', run_id='', inspection=True)
        page = result._skill_state.page(query='codex')
        item = next(item for item in page['skills'] if item['id'] == 'codex-delegate')
        assert item['status'] == 'unavailable' and '编程委托尚未启用' in str(item['problems'])
        await result.close()
    asyncio.run(run())


def test_controlled_resource_reader_returns_real_image_and_requires_reread_after_eviction(tmp_path, monkeypatch):
    from PIL import Image
    _, role_id, _, _, library = setup(tmp_path, monkeypatch, object())
    buffer = io.BytesIO()
    Image.new('RGB', (3, 2), 'yellow').save(buffer, 'PNG')
    saved = library.save(method(body='查看 assets/reference.png'), resources={'assets/reference.png': {
        'encoding': 'base64', 'content': base64.b64encode(buffer.getvalue()).decode()}})
    library.update_settings(enabled=True)
    state = SkillRun(library, role_id=role_id, session_key=f'role:{role_id}', run_id='image', prompt='')
    state.read(saved['ref'])
    image = state.read(saved['ref'], 'briefing/assets/reference.png')
    assert image['images'][0].media_type == 'image/png'
    assert base64.b64decode(image['images'][0].data_base64) == buffer.getvalue()
    receipt = ToolResultMessage('image', 'skill_read', image['content'], details={'skillRead': image['skillRead']}, images=image['images'])
    state.commit_window([receipt])
    assert state.read(saved['ref'], 'briefing/assets/reference.png')['alreadyLoaded']
    state.commit_window([UserMessage('只有工作摘要，没有图片')])
    reread = state.read(saved['ref'], 'briefing/assets/reference.png')
    assert reread['images'][0].data_base64 == image['images'][0].data_base64


def test_builtin_and_project_choices_survive_a_new_release_checkout(tmp_path, monkeypatch):
    _, role_id, roles, _, library = setup(tmp_path, monkeypatch, object())
    from backend.app.skill_library import SkillLibrary
    for release in ('release-one', 'release-two'):
        for source in ('project', 'builtin'):
            directory = tmp_path / release / source / source
            directory.mkdir(parents=True)
            (directory / 'SKILL.md').write_text(method(source), encoding='utf-8')
    first = SkillLibrary(roles, role_id, tmp_path / 'shared', tmp_path / 'release-one/project', tmp_path / 'release-one/builtin')
    refs = {item['source']: item['ref'] for item in first.describe()['skills']}
    first.update_settings(ref=refs['builtin'], active=False)
    first.update_settings(ref=refs['project'], policy='manual')
    second = SkillLibrary(roles, role_id, tmp_path / 'shared', tmp_path / 'release-two/project', tmp_path / 'release-two/builtin')
    rows = {item['source']: item for item in second.describe()['skills']}
    assert rows['builtin']['ref'] == refs['builtin'] and rows['builtin']['enabled'] is False
    assert rows['project']['ref'] == refs['project'] and rows['project']['policy'] == 'manual'


@pytest.mark.parametrize('root_name', ['skills', 'SKILLS'])
def test_root_level_bundle_cannot_remove_the_role_skill_library(tmp_path, monkeypatch, root_name):
    _, role_id, _, _, library = setup(tmp_path, monkeypatch, object())
    root = library.private_root.parent / root_name
    library.private_root = root
    library.roots = ((root, 'role'), *library.roots[1:])
    root.mkdir(parents=True)
    (root / 'SKILL.md').write_text(method('root-skill'), encoding='utf-8')
    child = root / 'another'
    child.mkdir()
    (child / 'SKILL.md').write_text(method('another'), encoding='utf-8')
    row = next(item for item in library.describe()['skills'] if item['id'] == 'root-skill')
    with pytest.raises(ValueError, match='移除路径无效'):
        library.remove(row['ref'], row['contentHash'])
    assert (root / 'SKILL.md').is_file() and (child / 'SKILL.md').is_file()


@pytest.mark.parametrize('fail_publication', [False, True])
def test_concurrent_listing_never_sees_a_paused_or_failed_publication(tmp_path, monkeypatch, fail_publication):
    from backend.app import skill_library
    client, role_id, _, _, library = setup(tmp_path, monkeypatch, object())
    saved = library.save(method('uncommitted-method', body='读取 references/input.md'),
        resources={'references/input.md': '完整公共资源'})
    other_id = client.post('/api/roles', json={'name': '并发读者', 'profile': {'profile': '另一角色'}}).json()['role']['id']
    ready, resume = threading.Event(), threading.Event()
    staged = []
    write = skill_library._atomic_text

    def pause_after_header(path, content):
        write(path, content)
        if path.name == 'SKILL.md' and path.parent.name.startswith('.publish-'):
            staged.append(path.parent)
            ready.set()
            if not resume.wait(10):
                raise AssertionError('publication test did not release its owned staging worker')
            if fail_publication:
                raise OSError('simulated failure before publication')

    monkeypatch.setattr(skill_library, '_atomic_text', pause_after_header)
    with ThreadPoolExecutor(max_workers=1) as executor:
        publication = executor.submit(library.publish, saved['ref'], saved['contentHash'])
        try:
            assert ready.wait(10)
            # A valid header already exists, but it is outside every discovery
            # root; correctness does not depend on hiding a special basename.
            assert staged[0].parent == library.shared_root.parent
            assert not staged[0].is_relative_to(library.shared_root)
            during = client.get(f'/api/roles/{other_id}/skills').json()
            assert not any(row['source'] == 'shared' for row in during['skills'])
            assert not (library.shared_root / 'uncommitted-method').exists()
        finally:
            resume.set()
        if fail_publication:
            with pytest.raises(OSError, match='simulated failure'):
                publication.result(timeout=10)
        else:
            assert publication.result(timeout=10)['published'] is True
    after = client.get(f'/api/roles/{other_id}/skills').json()
    public = [row for row in after['skills'] if row['source'] == 'shared']
    if fail_publication:
        assert not public
    else:
        assert [row['id'] for row in public] == ['uncommitted-method']
        assert public[0]['status'] == 'available'
        assert (library.shared_root / 'uncommitted-method/references/input.md').read_text(encoding='utf-8') == '完整公共资源'
    assert all(not path.exists() for path in staged)
    assert not list(library.shared_root.parent.glob('.publish-*'))
