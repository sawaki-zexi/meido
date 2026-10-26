"""Approved S04 Drift behavior through owner HTTP and the real managed tick."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from contextvars import ContextVar
from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import threading
import time
import sys
import sysconfig
from typing import Any

from PIL import Image
import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelImageBlock, ModelStreamDone, ModelTextBlock, ModelThinkingBlock, ModelToolCallBlock
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api as spawn_api


pytestmark = pytest.mark.usefixtures('local_platform_only')
_REBUILD_IMPORT_PATHS = json.dumps(list(dict.fromkeys((sysconfig.get_path('purelib'), sysconfig.get_path('platlib')))))


@pytest.fixture
def api(spawn_api, monkeypatch):
    client, roles, sessions, tasks = spawn_api
    monkeypatch.setattr(main, '_proactive', None)
    proof: dict[str, Any] = {'memory': False, 'channels': False, 'errors': []}
    if not hasattr(main, '_drift191_owner_proofs'):
        main._drift191_owner_proofs = []
    main._drift191_owner_proofs.append(proof)
    callbacks = []
    for callback in main.app.router.on_shutdown:
        async def close(callback=callback):
            try:
                await callback()
                if callback is main._close_memory_workers:
                    proof['memory'] = True
                if callback is main._close_channel_workers:
                    proof['channels'] = True
            except BaseException as error:
                proof['errors'].append(type(error).__name__)
                raise
        callbacks.append(close)
    monkeypatch.setattr(main.app.router, 'on_shutdown', callbacks)
    with client:
        yield client, roles, sessions, tasks
    assert proof == {'memory': True, 'channels': True, 'errors': []}


def test_owner_can_explicitly_enable_builtin_meme_maintenance_without_shell(api):
    client, roles, _, _ = api
    role = client.post('/api/roles', json={'name': '自主表情维护', 'profile': {'profile': '本地合成素材'},
        'agentConfig': {'enabledTools': ['read_file', 'list_dir', 'write_file', 'edit_file'],
            'shell': {'enabled': False, 'allowedCommands': []}}}).json()['role']
    path = f"/api/roles/{role['id']}/autonomy"
    initial = client.get(path).json()['config']
    assert initial['driftMeme']['enabled'] is False
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    saved = client.put(path, json={'expectedRevision': 0, 'config': {
        'driftEnabled': True, 'driftModelConfigurationId': model['id'], 'consentRevision': 1,
        'driftToolGrants': ['read_file', 'list_dir', 'write_file', 'edit_file'],
        'driftAuthoring': {'enabled': True, 'allowedTools': ['read_file', 'list_dir', 'write_file', 'edit_file', 'finish_drift']},
        'driftMeme': {'enabled': True, 'visionModelConfigurationId': model['id'], 'readEnabled': True,
            'editEnabled': True, 'copyEnabled': False, 'allowedCategories': ['happy'], 'sourceAttachmentIds': []}}})
    assert saved.status_code == 200, saved.text
    grant = saved.json()['config']['driftMeme']
    assert grant['managedEpoch'] and grant['revision'] == 1
    catalog = client.get(path + '/drift-skills')
    assert catalog.status_code == 200, catalog.text
    skill = next(item for item in catalog.json()['skills'] if item['id'] == 'meme-manage')
    assert skill['status'] == 'available', skill
    assert set(skill['tools']) == {'read_file', 'list_dir', 'write_file', 'edit_file', 'meme_copy_image', 'finish_drift'}
    assert skill['missingTools'] == skill['missingShellRules'] == []
    assert client.get(path).json()['runs'] == []
    assert roles.get(role['id']).agentConfig.shell.enabled is False


def test_builtin_remains_available_after_a_real_fresh_git_checkout_with_windows_text_conversion(api, monkeypatch, record_property, tmp_path_factory):
    from backend.app.proactive_runtime import drift_skills, meme_profile
    root = Path(__file__).resolve().parents[2]
    installed = tmp_path_factory.mktemp('r31')
    environment = {**os.environ, 'GIT_INDEX_FILE': str(installed / 'index')}
    executable = shutil.which('git')
    if executable is None and os.name == 'nt':
        fixed_git = Path('C:/Program Files/Git/cmd/git.exe')
        executable = str(fixed_git) if fixed_git.is_file() else None
    assert executable is not None, 'This installation fixture requires an actual Git executable'
    git_program = executable
    def git(*arguments):
        return subprocess.run([git_program, '-c', 'core.autocrlf=true', '-C', str(root), *arguments],
            env=environment, capture_output=True, text=True, encoding='utf-8', check=True).stdout.strip()
    git('read-tree', '--empty')
    paths = ['.gitattributes', 'backend/app/builtin_skills/meme-manage/SKILL.md',
        'backend/app/builtin_skills/meme-manage/references/drift-meme.json']
    for relative in paths:
        blob = git('hash-object', '-w', '--path=' + relative, str(root / relative))
        git('update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + relative)
    git('checkout-index', '--all', '--prefix=' + (installed / 'checkout').as_posix() + '/')
    fixed = installed / 'checkout/backend/app/builtin_skills'
    raw = (fixed / 'meme-manage/SKILL.md').read_bytes()
    record_property('installed_skill_sha256', hashlib.sha256(raw).hexdigest())
    record_property('installed_skill_crlf_count', raw.count(b'\r\n'))
    monkeypatch.setattr(drift_skills, 'BUILTIN_ROOT', fixed)
    monkeypatch.setattr(meme_profile, 'BUILTIN_ROOT', fixed)
    client, _, _, _ = api
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    _, path = configure_meme(client, model)
    catalog = client.get(path + '/drift-skills').json()['skills']
    skill = next(item for item in catalog if item['id'] == 'meme-manage')
    assert skill['status'] == 'available', skill
    assert client.get(path).json()['runs'] == []


def configure_meme(client, model, *, sources=()):
    role = client.post('/api/roles', json={'name': '旧库视觉维护', 'profile': {'profile': '本地合成素材'},
        'agentConfig': {'enabledTools': ['read_file', 'list_dir', 'write_file', 'edit_file']}}).json()['role']
    path = f"/api/roles/{role['id']}/autonomy"
    response = client.put(path, json={'expectedRevision': 0, 'config': {
        'driftEnabled': True, 'driftModelConfigurationId': model['id'], 'consentRevision': 1,
        'driftToolGrants': ['read_file', 'list_dir', 'write_file', 'edit_file'],
        'driftAuthoring': {'enabled': True, 'allowedTools': ['read_file', 'list_dir', 'write_file', 'edit_file', 'finish_drift']},
        'driftMeme': {'enabled': True, 'visionModelConfigurationId': model['id'], 'readEnabled': True,
            'editEnabled': True, 'copyEnabled': bool(sources), 'allowedCategories': ['happy'], 'sourceAttachmentIds': list(sources)}}})
    assert response.status_code == 200, response.text
    return role['id'], path


async def settled(application, role):
    await application.on_clock()
    async with asyncio.timeout(20):
        while True:
            await application.recover()
            state = application.query(role)
            if state['runs'] and not state['cleanupPending'] and all(row['runtimeAcknowledged'] for row in state['runs']):
                return state
            await asyncio.sleep(.01)


@pytest.mark.parametrize('authoring_enabled', [True, False])
def test_legacy_library_image_reaches_next_drift_request_and_actual_category_is_maintained(api, monkeypatch, authoring_enabled):
    client, roles, sessions, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    if not authoring_enabled:
        config = client.get(path).json()['config']
        assert client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
            'driftAuthoring': {**config['driftAuthoring'], 'enabled': False}}}).status_code == 200
    library = roles.workspace_path(role) / 'memes'
    (library / 'happy').mkdir(parents=True)
    (library / 'other').mkdir()
    image = Image.new('RGB', (640, 480), '#467fa8')
    image.save(library / 'happy/legacy.png')
    (library / 'other/owner.txt').write_text('owner-kept-191', encoding='utf-8')
    before: dict[str, Any] = {'version': 1, 'categories': {'happy': {'desc': '旧说明', 'aliases': ['旧名'], 'enabled': True},
        'other': {'desc': '保留类别', 'aliases': [], 'enabled': True}}}
    (library / 'manifest.json').write_text(json.dumps(before, ensure_ascii=False), encoding='utf-8')
    after = {'version': 1, 'categories': {**before['categories'], 'happy': {'desc': '看过真实图片后的说明', 'aliases': ['新别名'], 'enabled': True}}}

    class Maintenance:
        requests = 0
        actual_image_requests = 0
        encoded_images = []
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            assert configuration.id == model['id']
            assert 'shell' not in {item['function']['name'] for item in tools}
            images = [block for message in messages for block in message.content if isinstance(block, ModelImageBlock)]
            if images:
                self.actual_image_requests += 1
                self.encoded_images.append(images[0].data_base64)
                assert self.requests == 4 and len(images) == 1
                with Image.open(io.BytesIO(base64.b64decode(images[0].data_base64))) as actual:
                    assert actual.size == (640, 480)
                    assert actual.getpixel((10, 10)) == (70, 127, 168)
            steps = [('activate_skill', {'skill_id': 'meme-manage'}),
                ('read_file', {'path': '@memes/manifest.json'}),
                ('read_file', {'path': '@memes/happy/legacy.png'}),
                ('write_file', {'path': '@memes/manifest.json', 'content': json.dumps(after, ensure_ascii=False)}),
                ('read_file', {'path': '@memes/manifest.json'}),
                ('finish_drift', {'skill_used': 'meme-manage', 'one_line': '真实图片核验后维护类别',
                    'next_action': '下次继续查看同一库', 'message_result': 'silent'})]
            assert self.requests <= len(steps), 'A rejected real operation must not loop forever'
            name, arguments = steps[self.requests - 1]
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('meme-' + str(self.requests), name, arguments),)))
    adapter = Maintenance()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert state['runs'][0]['status'] == 'finished_silent', state
    assert adapter.requests == 6 and adapter.actual_image_requests == 1
    assert json.loads((library / 'manifest.json').read_text(encoding='utf-8')) == after
    assert (library / 'other/owner.txt').read_text() == 'owner-kept-191'
    skill = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'meme-manage')
    assert skill['progress']['runCount'] == 1 and skill['progress']['nextAction'] == '下次继续查看同一库'
    assert sessions.list_messages('role:' + role) == []
    assert all(encoded.encode('ascii') not in Path(sessions.database_path).read_bytes() for encoded in adapter.encoded_images)


def test_managed_owner_file_edit_requires_its_own_read_and_disable_restores_ordinary_contract(api, monkeypatch):
    client, roles, _, _ = api
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    library = roles.workspace_path(role) / 'memes'
    library.mkdir(exist_ok=True)
    original = {'version': 1, 'categories': {'happy': {'desc': '主人原内容', 'aliases': [], 'enabled': True}}}
    target = library / 'manifest.json'
    target.write_text(json.dumps(original), encoding='utf-8')
    desired = {'version': 1, 'categories': {'happy': {'desc': '主人新内容', 'aliases': ['新'], 'enabled': True}}}
    class Owner:
        calls = 0
        results = []
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls % 2:
                yield ModelToolCall('owner-direct-write', 'write_file', {'path': 'memes/manifest.json', 'content': json.dumps(desired)})
            else:
                self.results.append(next(item['content'] for item in reversed(messages) if item['role'] == 'tool'))
                yield ModelTextDelta('已核对真实写入结果')
    adapter = Owner()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    assert 'assistant_completed' in client.post(f'/api/roles/{role}/messages', json={'content': '直接编辑类别'}).text
    assert 'needs_read' in adapter.results[-1]
    assert json.loads(target.read_text()) == original
    config = client.get(path).json()['config']
    disabled = client.put(path, json={'expectedRevision': config['revision'], 'config': {
        **config, 'driftMeme': {**config['driftMeme'], 'enabled': False}}})
    assert disabled.status_code == 200, disabled.text
    assert 'assistant_completed' in client.post(f'/api/roles/{role}/messages', json={'content': '停用后继续普通编辑'}).text
    assert 'needs_read' not in adapter.results[-1]
    assert json.loads(target.read_text()) == desired


@pytest.mark.parametrize('category_edit_enabled', [True, False])
def test_builtin_copies_only_the_frozen_uploaded_source_as_real_bytes_without_shell(api, monkeypatch, category_edit_enabled):
    client, roles, _, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    original = io.BytesIO()
    Image.new('RGB', (720, 540), '#d76b39').save(original, 'PNG')
    raw = original.getvalue()
    uploaded = client.post(f'/api/roles/{role}/attachments', content=raw,
        headers={'x-attachment-filename': 'fictional-orange.png', 'content-type': 'image/png'})
    assert uploaded.status_code == 200, uploaded.text
    source = uploaded.json()
    config = client.get(path).json()['config']
    assert client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
        'driftMeme': {**config['driftMeme'], 'editEnabled': category_edit_enabled, 'copyEnabled': True, 'sourceAttachmentIds': [source['id']]}}}).status_code == 200
    library = roles.workspace_path(role) / 'memes'
    (library / 'manifest.json').write_text(json.dumps({'version': 1,
        'categories': {'happy': {'desc': '主人类别', 'aliases': [], 'enabled': True}}}), encoding='utf-8')
    class Copy:
        requests = 0
        actual_target = None
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'meme-manage'}
            elif self.requests == 2:
                assert 'meme_copy_image' in {item['function']['name'] for item in tools}
                name, arguments = 'read_file', {'path': '@memes/manifest.json'}
            elif self.requests == 3:
                source_block = next(block.text for message in messages for block in message.content
                    if hasattr(block, 'text') and block.text.startswith('本run获准表情素材：'))
                identifier = json.loads(source_block.removeprefix('本run获准表情素材：'))[0]['id']
                name, arguments = 'meme_copy_image', {'attachment_id': identifier, 'category': 'happy'}
            elif self.requests == 4:
                result = json.loads(messages[-1].content[0].text)
                assert result['status'] == 'recorded' and result['sourcePublicId'] == source['id']
                self.actual_target = result['path']
                name, arguments = 'read_file', {'path': result['path']}
            else:
                assert self.requests == 5
                assert any(isinstance(block, ModelImageBlock) for message in messages for block in message.content)
                name, arguments = 'finish_drift', {'skill_used': 'meme-manage', 'one_line': '实际复制并查看原素材',
                    'next_action': '下次维护同一副本', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('copy-' + str(self.requests), name, arguments),)))
    adapter = Copy()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert state['runs'][0]['status'] == 'finished_silent', state
    assert isinstance(adapter.actual_target, str) and adapter.actual_target.startswith('@memes/happy/')
    actual = library / adapter.actual_target.removeprefix('@memes/')
    assert actual.read_bytes() == raw
    assert len(list((library / 'happy').iterdir())) == 1
    metadata = list((library.parent / '.meido-memes/metadata').glob('*.json'))
    assert len(metadata) == 1 and json.loads(metadata[0].read_text())['sourcePublicId'] == source['id']
    assert client.get(path).json()['deliveries'] == []


@pytest.mark.parametrize('held_phase', ['staging_fsync', 'staging_hash_read', 'image_decode', 'image_resize'])
def test_disable_allows_owner_edit_while_original_staging_worker_is_still_owned_and_blocks_reenable(api, monkeypatch, held_phase):
    client, roles, _, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    library = roles.workspace_path(role) / 'memes'
    original = {'version': 1, 'categories': {'happy': {'desc': '原类别', 'aliases': [], 'enabled': True}}}
    stale = {'version': 1, 'categories': {'happy': {'desc': '旧自主编辑不得晚到覆盖', 'aliases': [], 'enabled': True}}}
    owner = {'version': 1, 'categories': {'happy': {'desc': '停用后的主人新内容', 'aliases': ['主人'], 'enabled': True}}}
    target = library / 'manifest.json'
    target.write_text(json.dumps(original), encoding='utf-8')
    (library / 'happy').mkdir()
    Image.new('RGB', (2000, 1600), '#523970').save(library / 'happy/legacy.png')
    started, release = threading.Event(), threading.Event()
    real_fsync = os.fsync
    def slow_actual_stage(fd):
        real_fsync(fd)
        info = os.fstat(fd)
        stages = list((library.parent / '.meido-memes/staging').glob('*.tmp'))
        if held_phase == 'staging_fsync' and any((item.stat().st_dev, item.stat().st_ino) == (info.st_dev, info.st_ino) for item in stages):
            started.set()
            release.wait()
    monkeypatch.setattr(os, 'fsync', slow_actual_stage)
    real_read = os.read
    def slow_actual_staging_read(fd, count):
        raw = real_read(fd, count)
        info = os.fstat(fd)
        stages = list((library.parent / '.meido-memes/staging').glob('*.tmp'))
        if held_phase == 'staging_hash_read' and not started.is_set() and any(
                (item.stat().st_dev, item.stat().st_ino) == (info.st_dev, info.st_ino) for item in stages):
            started.set()
            release.wait()
        return raw
    monkeypatch.setattr(os, 'read', slow_actual_staging_read)
    real_load, real_thumbnail = Image.Image.load, Image.Image.thumbnail
    def slow_real_decode(image, *args, **kwargs):
        result = real_load(image, *args, **kwargs)
        if held_phase == 'image_decode' and result is not None and image.size == (2000, 1600) and not started.is_set():
            assert result[0, 0] == (82, 57, 112)
            started.set()
            release.wait()
        return result
    def slow_real_resize(image, *args, **kwargs):
        result = real_thumbnail(image, *args, **kwargs)
        if held_phase == 'image_resize' and not started.is_set():
            assert image.size == (1568, 1254)
            started.set()
            release.wait()
        return result
    monkeypatch.setattr(Image.Image, 'load', slow_real_decode)
    monkeypatch.setattr(Image.Image, 'thumbnail', slow_real_resize)
    class MaintenanceAndOwner:
        requests = 0
        owner_calls = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            if any('停用后继续编辑，不等待旧文件worker' in getattr(block, 'text', '') for message in messages for block in message.content):
                self.owner_calls += 1
                block = ModelToolCallBlock('owner-after-disable', 'write_file', {'path': 'memes/manifest.json', 'content': json.dumps(owner)}) if self.owner_calls == 1 else ModelTextBlock('主人真实编辑已完成')
                yield ModelStreamDone(ModelAssistantMessage((block,)))
                return
            self.requests += 1
            final_tool = ('read_file', {'path': '@memes/happy/legacy.png'}) if held_phase.startswith('image_') else (
                'write_file', {'path': '@memes/manifest.json', 'content': json.dumps(stale)})
            steps = [('activate_skill', {'skill_id': 'meme-manage'}), ('read_file', {'path': '@memes/manifest.json'}), final_tool]
            assert self.requests <= 3
            name, arguments = steps[self.requests - 1]
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('slow-' + str(self.requests), name, arguments),)))
        async def stream_structured_messages(self, messages, configuration, tools):
            self.owner_calls += 1
            if self.owner_calls == 1:
                yield ModelToolCall('owner-after-disable', 'write_file', {'path': 'memes/manifest.json', 'content': json.dumps(owner)})
            else:
                yield ModelTextDelta('主人真实编辑已完成')
    adapter = MaintenanceAndOwner()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    config = client.get(path).json()['config']
    watchdog_fired = threading.Event()
    def release_fixture_on_timeout():
        if not release.wait(20):
            watchdog_fired.set()
            release.set()
    watchdog = threading.Thread(target=release_fixture_on_timeout, name='drift191-fixture-release')
    watchdog.start()
    requests = ThreadPoolExecutor(max_workers=2)
    try:
        now[0] += 1800
        clock = requests.submit(client.portal.call, main._proactive.on_clock)
        assert started.wait(10), 'The real image processing or private staging boundary must be reached'
        disabled = requests.submit(client.put, path, json={'expectedRevision': config['revision'], 'config': {
            **config, 'driftMeme': {**config['driftMeme'], 'enabled': False}}}).result(timeout=5)
        assert disabled.status_code == 200, disabled.text
        clock.result(timeout=5)
        response = requests.submit(client.post, f'/api/roles/{role}/messages', json={'content': '停用后继续编辑，不等待旧文件worker'}).result(timeout=5)
        assert 'assistant_completed' in response.text, response.text
        assert not release.is_set() and json.loads(target.read_text()) == owner
        config = client.get(path).json()['config']
        reenabling = client.put(path, json={'expectedRevision': config['revision'], 'config': {
            **config, 'driftMeme': {**config['driftMeme'], 'enabled': True}}})
        assert reenabling.status_code == 409, reenabling.text
        for _ in range(3):
            stopped = client.post(path + '/stop')
            assert stopped.status_code == 202, stopped.text
        state = client.get(path).json()
        assert state['cleanupPending'] and not state['runs'][0]['runtimeAcknowledged']
    finally:
        release.set()
        requests.shutdown(wait=True)
        watchdog.join()
    assert not watchdog_fired.is_set(), 'The fixture watchdog must never substitute for ordinary owner cleanup'
    state = client.portal.call(settled, main._proactive, role)
    assert state['runs'][0]['status'] != 'finished_silent'
    assert adapter.requests == 3, 'A sealed worker cannot obtain a later provider request after joining'
    assert json.loads(target.read_text()) == owner
    config = client.get(path).json()['config']
    enabled = client.put(path, json={'expectedRevision': config['revision'], 'config': {
        **config, 'driftMeme': {**config['driftMeme'], 'enabled': True}}})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()['config']['driftMeme']['managedEpoch'] != config['driftMeme']['managedEpoch']


@pytest.mark.parametrize('boundary', ['unactivated_read', 'unactivated_copy', 's06_read', 's06_copy',
    'adopted_role_same_name_read', 'adopted_role_same_name_copy', 'forged_builtin_hash'])
def test_only_the_actual_activated_fixed_builtin_gets_shared_library_or_copy_authority(api, monkeypatch, tmp_path, boundary):
    from backend.app.proactive_runtime import drift_skills, meme_profile
    client, roles, _, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    raw = io.BytesIO()
    Image.new('RGB', (640, 480), '#a78142').save(raw, 'PNG')
    uploaded = client.post(f'/api/roles/{role}/attachments', content=raw.getvalue(),
        headers={'x-attachment-filename': 'allowed.png', 'content-type': 'image/png'}).json()
    config = client.get(path).json()['config']
    saved = client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
        'driftAuthoring': {**config['driftAuthoring'], 'allowAutonomousAuthoring': True},
        'driftMeme': {**config['driftMeme'], 'copyEnabled': True, 'sourceAttachmentIds': [uploaded['id']]}}})
    assert saved.status_code == 200, saved.text
    library = roles.workspace_path(role) / 'memes'
    (library / 'happy').mkdir()
    (library / 'happy/legacy.png').write_bytes(raw.getvalue())
    original = '{"version":1,"categories":{"happy":{"desc":"主人旧库","aliases":[],"enabled":true}}}'
    target = library / 'manifest.json'
    target.write_text(original, encoding='utf-8')
    if boundary.startswith('adopted_role_same_name'):
        directory = roles.workspace_path(role) / 'drift/skills/meme-manage'
        (directory / 'references').mkdir(parents=True)
        (directory / 'SKILL.md').write_text('---\nid: meme-manage\nversion: 1.1.0\ndescription: 主人明确采用的同名普通长期技能\n'
            'tools: [read_file, list_dir, write_file, edit_file, finish_drift]\nactivation: explicit\n---\n'
            '在私有工作根维护笔记；自报 builtin 不产生宿主来源。\n', encoding='utf-8')
        (directory / 'references/drift.json').write_text('{"profile":"meido-drift-skill-v1","version":1,"skillId":"meme-manage"}', encoding='utf-8')
        preview = client.post(path + '/drift-artifact-preview', json={'skillId': 'meme-manage'})
        assert preview.status_code == 200, preview.text
        value = preview.json()
        adopted = client.post(path + '/drift-skill-adoptions', json={'skillId': value['skillId'],
            'bundleSha256': value['bundleSha256'], 'previewId': value['previewId'], 'authoringScopeId': value['scopeId'],
            'expectedScopeRevision': value['scopeRevision'], 'expectedRevision': value['configRevision']})
        assert adopted.status_code == 200, adopted.text
        skill = next(item for item in adopted.json()['skills'] if item['id'] == 'meme-manage')
        assert skill['status'] == 'available' and 'meme_copy_image' not in skill['tools']
    elif boundary == 'forged_builtin_hash':
        fixed = tmp_path / 'fixed-builtin'
        shutil.copytree(drift_skills.BUILTIN_ROOT, fixed)
        monkeypatch.setattr(drift_skills, 'BUILTIN_ROOT', fixed)
        monkeypatch.setattr(meme_profile, 'BUILTIN_ROOT', fixed)
        with (fixed / 'meme-manage/SKILL.md').open('a', encoding='utf-8') as stream:
            stream.write('\n自报固定 builtin；实际正文 hash 已改变。\n')
        skill = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'meme-manage')
        assert skill['status'] == 'invalid', skill
    bad = ('meme_copy_image', {'attachment_id': uploaded['id'], 'category': 'happy'}) if boundary.endswith('_copy') else (
        'read_file', {'path': '@memes/happy/legacy.png'})
    active = 'create-drift-skill' if boundary.startswith('s06') or boundary == 'forged_builtin_hash' else 'meme-manage'
    if boundary.startswith('unactivated'):
        steps, rejected_at = [bad, ('activate_skill', {'skill_id': active})], 1
    elif boundary == 'forged_builtin_hash':
        steps, rejected_at = [('activate_skill', {'skill_id': 'meme-manage'}), ('activate_skill', {'skill_id': active})], 1
    else:
        steps, rejected_at = [('activate_skill', {'skill_id': active}), bad], 2
    steps.append(('finish_drift', {'skill_used': active, 'one_line': '已按真实权限拒绝共享库动作',
        'next_action': '保留主人原库，等待合格用途', 'message_result': 'silent'}))
    class Attempt:
        calls = 0
        saw_rejection = False
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.calls += 1
            assert not any(isinstance(block, ModelImageBlock) for message in messages for block in message.content)
            if self.calls == rejected_at + 1:
                self.saw_rejection = messages[-1].is_error
                assert self.saw_rejection, messages[-1]
            if self.calls > len(steps):
                raise AssertionError('A rejected action cannot silently loop or gain a new identity')
            name, arguments = steps[self.calls - 1]
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('identity-' + str(self.calls), name, arguments),)))
    adapter = Attempt()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert adapter.saw_rejection and adapter.calls == len(steps), state
    assert state['runs'][0]['status'] == 'finished_silent', state
    assert target.read_text(encoding='utf-8') == original
    assert list((library / 'happy').iterdir()) == [library / 'happy/legacy.png']
    assert client.get(path + '/meme-library').json()['operations'] == []


@pytest.mark.parametrize('boundary', ['unknown_source', 'other_role_source', 'ungranted_category', 'changed_source',
    'changed_model', 'hardlinked_image'])
def test_original_role_source_category_model_and_real_file_identity_remain_required_after_activation(api, monkeypatch, boundary):
    client, roles, _, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    image = io.BytesIO()
    Image.new('RGB', (640, 480), '#386897').save(image, 'PNG')
    raw = image.getvalue()
    source = client.post(f'/api/roles/{role}/attachments', content=raw,
        headers={'x-attachment-filename': 'source.png', 'content-type': 'image/png'}).json()
    other = client.post('/api/roles', json={'name': '另一个相同名称角色', 'profile': {'profile': '不同稳定身份'}}).json()['role']['id']
    foreign = client.post(f'/api/roles/{other}/attachments', content=raw,
        headers={'x-attachment-filename': 'foreign.png', 'content-type': 'image/png'}).json()
    config = client.get(path).json()['config']
    saved = client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
        'driftMeme': {**config['driftMeme'], 'copyEnabled': True, 'sourceAttachmentIds': [source['id']]}}})
    assert saved.status_code == 200, saved.text
    library = roles.workspace_path(role) / 'memes'
    (library / 'happy').mkdir()
    (library / 'other').mkdir()
    original = '{"version":1,"categories":{"happy":{"desc":"原类别","aliases":[],"enabled":true},"other":{"desc":"未许可","aliases":[],"enabled":true}}}'
    target = library / 'manifest.json'
    target.write_text(original, encoding='utf-8')
    source_path = next(path for path in (roles.workspace_path(role) / 'attachments').iterdir()
        if path.name.startswith(source['id'] + '-'))
    if boundary == 'hardlinked_image':
        os.link(source_path, library / 'happy/linked.png')
        assert (library / 'happy/linked.png').stat().st_nlink == 2

    class Attempt:
        calls = 0
        rejected = False
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.calls += 1
            assert not any(isinstance(block, ModelImageBlock) for message in messages for block in message.content)
            if self.calls == 1:
                name, arguments = 'activate_skill', {'skill_id': 'meme-manage'}
            elif self.calls == 2:
                name, arguments = 'read_file', {'path': '@memes/manifest.json'}
            elif self.calls == 3:
                if boundary == 'changed_source':
                    replacement = io.BytesIO()
                    Image.new('RGB', (640, 480), '#c83251').save(replacement, 'PNG')
                    source_path.write_bytes(replacement.getvalue())
                if boundary == 'changed_model':
                    replacement = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
                        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'another-explicit-model', 'apiKey': 'fixture'}).json()['configuration']
                    current = client.get(path).json()['config']
                    changed = client.put(path, json={'expectedRevision': current['revision'], 'config': {**current,
                        'driftModelConfigurationId': replacement['id'],
                        'driftMeme': {**current['driftMeme'], 'visionModelConfigurationId': replacement['id']}}})
                    assert changed.status_code == 200, changed.text
                if boundary == 'hardlinked_image':
                    name, arguments = 'read_file', {'path': '@memes/happy/linked.png'}
                else:
                    identifier = foreign['id'] if boundary == 'other_role_source' else '0' * 32 if boundary == 'unknown_source' else source['id']
                    name, arguments = 'meme_copy_image', {'attachment_id': identifier,
                        'category': 'other' if boundary == 'ungranted_category' else 'happy'}
            else:
                assert self.calls == 4
                self.rejected = messages[-1].is_error
                assert self.rejected, messages[-1]
                name, arguments = 'finish_drift', {'skill_used': 'meme-manage', 'one_line': '原权限或实际文件身份不符，未复制',
                    'next_action': '保留原库等待主人核对', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('scope-' + str(self.calls), name, arguments),)))
    adapter = Attempt()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    if boundary in {'changed_model', 'hardlinked_image'}:
        # A link is rejected at bounded storage admission; a changed model
        # seals the already-running original request before further dispatch.
        assert adapter.calls == (0 if boundary == 'hardlinked_image' else 3), state
        assert state['runs'][0]['status'] == 'aborted', state
    else:
        assert adapter.calls == 4 and adapter.rejected, state
        assert state['runs'][0]['status'] == 'finished_silent', state
    assert target.read_text(encoding='utf-8') == original
    assert client.get(path + '/meme-library').json()['operations'] == []
    assert not list((library.parent / '.meido-memes').rglob('*.tmp'))


def test_owner_library_observation_is_invalidated_by_a_different_managed_file_publication(api, monkeypatch):
    client, roles, _, _ = api
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, _ = configure_meme(client, model)
    library = roles.workspace_path(role) / 'memes'
    original = {'version': 1, 'categories': {'happy': {'desc': '原有分类', 'aliases': [], 'enabled': True}}}
    target = library / 'manifest.json'
    target.write_text(json.dumps(original), encoding='utf-8')
    class Owner:
        calls = 0
        result = None
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            steps = [('read_file', {'path': 'memes/manifest.json'}),
                ('write_file', {'path': 'memes/owner-notes.txt', 'content': '本轮另一个真实库编辑'}),
                ('write_file', {'path': 'memes/manifest.json', 'content': json.dumps({'version': 1, 'categories': {}})})]
            if self.calls <= len(steps):
                name, arguments = steps[self.calls - 1]
                yield ModelToolCall('owner-revision-' + str(self.calls), name, arguments)
            else:
                self.result = next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('已核验共享库版本')
    adapter = Owner()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    response = client.post(f'/api/roles/{role}/messages', json={'content': '在同一轮编辑两个库文件'})
    assert 'assistant_completed' in response.text, response.text
    assert isinstance(adapter.result, str) and 'conflict' in adapter.result
    assert json.loads(target.read_text()) == original
    assert (library / 'owner-notes.txt').read_text(encoding='utf-8') == '本轮另一个真实库编辑'


def test_next_real_application_process_reads_and_maintains_the_same_library_after_full_owner_shutdown(api, tmp_path, tmp_path_factory):
    from backend.app.task_runtime import TaskOrigin, TaskBudget
    from backend.app.task_runtime.process import ProcessExecutor
    _, _, _, tasks = api
    persistent = tmp_path_factory.mktemp('r30')
    fixture = Path(__file__).resolve().parents[1] / 'fixtures/drift_meme_rebuild.py'
    reports = []
    for phase in (1, 2):
        origin = TaskOrigin('drift191-rebuild', 'role:drift191-rebuild', 'phase-' + str(phase))
        executor = ProcessExecutor([sys.executable, '-I', '-S', str(fixture), str(persistent), str(phase)], tmp_path,
            dict(os.environ, MEIDO_TEST_SITE_PATHS=_REBUILD_IMPORT_PATHS))
        identifier = tasks.submit(origin, executor, budget=TaskBudget(60, 60000), kind='shell', description='Owned full application rebuild fixture')
        while True:
            result = tasks.query(identifier, origin, wait_seconds=.2)
            if result.ended_at is not None:
                break
        assert result.status == 'succeeded' and result.exit_code == 0 and not result.log_truncated, result.output
        reports.append(json.loads((persistent / ('phase-' + str(phase) + '-result.json')).read_text(encoding='utf-8')))
    assert reports[0]['pid'] != reports[1]['pid']
    assert reports[0]['roleId'] == reports[1]['roleId']
    assert [item['progress']['runCount'] for item in reports] == [1, 2]
    assert all(item['memory'] and item['channels'] and item['actualImageRequests'] == 1 and item['newThreads'] == [] for item in reports)
    assert reports[0]['imagePath'] == reports[1]['imagePath']
    assert reports[0]['copiedSha256'] == reports[1]['copiedSha256']
    assert reports[0]['ownerImageRequests'] == 0 and reports[1]['ownerImageRequests'] == 1


def test_recovery_reports_physically_published_copy_and_preserves_later_owner_manifest(api, monkeypatch):
    client, roles, _, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    image = io.BytesIO()
    Image.new('RGB', (720, 540), '#3956b2').save(image, 'PNG')
    uploaded = client.post(f'/api/roles/{role}/attachments', content=image.getvalue(),
        headers={'x-attachment-filename': 'failure-cut.png', 'content-type': 'image/png'}).json()
    config = client.get(path).json()['config']
    assert client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
        'driftMeme': {**config['driftMeme'], 'copyEnabled': True, 'sourceAttachmentIds': [uploaded['id']]}}}).status_code == 200
    library = roles.workspace_path(role) / 'memes'
    manifest = library / 'manifest.json'
    manifest.write_text(json.dumps({'version': 1, 'categories': {'happy': {'desc': '原分类', 'aliases': [], 'enabled': True}}}), encoding='utf-8')
    original_replace = os.replace
    raised: list[str] = []
    def fail_after_actual_image(source, target):
        result = original_replace(source, target)
        if Path(target).parent == library / 'happy' and not raised:
            raised.append(str(target))
            raise OSError('fixture: caller lost the response after actual image publication')
        return result
    monkeypatch.setattr(os, 'replace', fail_after_actual_image)
    class Copy:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            steps = [('activate_skill', {'skill_id': 'meme-manage'}), ('read_file', {'path': '@memes/manifest.json'}),
                ('meme_copy_image', {'attachment_id': uploaded['id'], 'category': 'happy'})]
            assert self.requests <= 3, 'Recovery must not retry the model'
            name, arguments = steps[self.requests - 1]
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('cut-' + str(self.requests), name, arguments),)))
    adapter = Copy()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert state['runs'][0]['status'] == 'aborted' and len(raised) == 1
    copied = Path(raised[0])
    assert copied.read_bytes() == image.getvalue()
    status = client.get(path + '/meme-library')
    assert status.status_code == 200, status.text
    operation = status.json()['operations'][0]
    assert operation['status'] == 'cleanup_pending' and operation['physicalPhase'] == 'files_published'
    config = client.get(path).json()['config']
    assert client.put(path, json={'expectedRevision': config['revision'], 'config': {
        **config, 'driftMeme': {**config['driftMeme'], 'enabled': False}}}).status_code == 200
    desired = {'version': 1, 'categories': {'happy': {'desc': '主人故障后采用的内容', 'aliases': ['保留'], 'enabled': True}}}
    class Owner:
        calls = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall('owner-after-fault', 'write_file', {'path': 'memes/manifest.json', 'content': json.dumps(desired)})
            else:
                yield ModelTextDelta('主人新内容已保存')
    monkeypatch.setattr(main, 'model_adapter', Owner())
    assert 'assistant_completed' in client.post(f'/api/roles/{role}/messages', json={'content': '保留故障之后的主人内容'}).text
    client.portal.call(main._proactive.recover)
    operation = client.get(path + '/meme-library').json()['operations'][0]
    assert operation['status'] == 'conflict' and operation['physicalPhase'] == 'files_published'
    assert json.loads(manifest.read_text()) == desired and copied.read_bytes() == image.getvalue()
    assert len(list((library / 'happy').iterdir())) == 1 and adapter.requests == 3


@pytest.mark.parametrize('filled', ['combined_bytes', 'combined_entries'])
def test_managed_owner_write_reserves_actual_combined_library_and_drift_peak_before_staging(api, monkeypatch, filled):
    client, roles, _, _ = api
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    workspace = roles.workspace_path(role)
    library, drift = workspace / 'memes', workspace / 'drift'
    drift.mkdir(exist_ok=True)
    if filled == 'combined_bytes':
        for target, blocks in ((library / 'legacy-owner.bin', 32), (drift / 'unknown-owned.bin', 33)):
            with target.open('wb') as stream:
                for _ in range(blocks):
                    stream.write(b'x' * (1024 * 1024))
    else:
        for index in range(130):
            (library / ('owner-' + str(index))).write_bytes(b'owner')
        for index in range(127):
            (drift / ('old-' + str(index))).write_bytes(b'drift')
    snapshots = {str(target): target.stat().st_size for root in (library, drift) for target in root.iterdir()}
    class Owner:
        calls = 0
        result = ''
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall('quota-owner', 'write_file', {'path': 'memes/new-owner.txt', 'content': 'new bytes'})
            else:
                self.result = next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('已读取有限空间结果')
    adapter = Owner()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    response = client.post(f'/api/roles/{role}/messages', json={'content': '验证受管表情库实际合计空间'})
    assert 'assistant_completed' in response.text, response.text
    assert 'quota' in adapter.result, adapter.result
    assert not (library / 'new-owner.txt').exists()
    assert {str(target): target.stat().st_size for root in (library, drift) for target in root.iterdir()} == snapshots
    assert client.get(path + '/meme-library').json()['operations'] == []


def test_drift_admission_includes_existing_managed_library_and_private_stage_bytes(api, monkeypatch):
    client, roles, _, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    workspace = roles.workspace_path(role)
    with (workspace / 'memes/legacy.bin').open('wb') as stream:
        for _ in range(32):
            stream.write(b'o' * (1024 * 1024))
    private = workspace / '.meido-memes/staging'
    private.mkdir(parents=True)
    with (private / 'unknown-private.tmp').open('wb') as stream:
        for _ in range(33):
            stream.write(b's' * (1024 * 1024))
    class Provider:
        calls = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.calls += 1
            name, arguments = [('activate_skill', {'skill_id': 'meme-manage'}),
                ('finish_drift', {'status': 'silent', 'next_action': '继续维护', 'note': ''})][self.calls - 1]
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('full-' + str(self.calls), name, arguments),)))
    adapter = Provider()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert adapter.calls == 0, 'The existing private stage and old library must be charged before a new autonomous model call'
    assert state['runs'][0]['status'] == 'aborted' and 'quota' in state['runs'][0]['error']
    assert (private / 'unknown-private.tmp').stat().st_size == 33 * 1024 * 1024
    assert (workspace / 'memes/legacy.bin').stat().st_size == 32 * 1024 * 1024


def test_global_storage_quota_counts_another_managed_roles_actual_old_library_before_new_owner_write(api, monkeypatch):
    client, roles, _, _ = api
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    other, _ = configure_meme(client, model)
    role, _ = configure_meme(client, model)
    other_library = roles.workspace_path(other) / 'memes'
    block = b'g' * (1024 * 1024)
    for index in range(16):
        with (other_library / ('legacy-' + str(index) + '.bin')).open('wb') as stream:
            for _ in range(64):
                stream.write(block)
    class Owner:
        calls = 0
        result = ''
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall('global-owner', 'write_file', {'path': 'memes/new-owner.txt', 'content': 'new bytes'})
            else:
                self.result = next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('已核对全局实际存储')
    adapter = Owner()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    response = client.post(f'/api/roles/{role}/messages', json={'content': '验证另一角色旧库计入全局空间'})
    assert 'assistant_completed' in response.text, response.text
    assert 'quota' in adapter.result, adapter.result
    assert not (roles.workspace_path(role) / 'memes/new-owner.txt').exists()
    assert sum(target.stat().st_size for target in other_library.iterdir()) == 1024 * 1024 * 1024


@pytest.mark.parametrize(('cut', 'expected_status', 'expected_phase', 'published_files'), [
    ('before_image', 'not_published', 'files_ready', 0),
    ('after_image_identity', 'conflict', 'files_published', 1),
    ('after_metadata', 'cleanup_pending', 'files_published', 2),
    ('after_manifest', 'recorded', 'manifest_published', 3),
    ('concurrent_after_manifest', 'recorded', 'manifest_published', 3),
    ('before_receipt_sql', 'recorded', 'manifest_published', 3),
])
def test_recovery_reports_each_real_copy_publication_boundary_without_replaying_it(api, monkeypatch, record_property, cut, expected_status, expected_phase, published_files):
    client, roles, sessions, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    image = io.BytesIO()
    Image.new('RGB', (720, 540), '#976a29').save(image, 'PNG')
    uploaded = client.post(f'/api/roles/{role}/attachments', content=image.getvalue(),
        headers={'x-attachment-filename': 'phase-source.png', 'content-type': 'image/png'}).json()
    config = client.get(path).json()['config']
    assert client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
        'driftMeme': {**config['driftMeme'], 'copyEnabled': True, 'sourceAttachmentIds': [uploaded['id']]}}}).status_code == 200
    library = roles.workspace_path(role) / 'memes'
    private = library.parent / '.meido-memes'
    manifest = library / 'manifest.json'
    manifest_raw = json.dumps({'version': 1, 'categories': {'happy': {'desc': '保留原类别', 'aliases': [], 'enabled': True}}}).encode()
    manifest.write_bytes(manifest_raw)
    original_reconcile = main._proactive.memes.reconcile
    if cut == 'concurrent_after_manifest':
        # Keep the actual partial intent until the two original recovery owners
        # are deliberately admitted; no filesystem/parser or original run is replaced.
        monkeypatch.setattr(main._proactive.memes, 'reconcile', lambda role_id: None)
    if cut == 'before_receipt_sql':
        with sqlite3.connect(sessions.database_path) as db:
            db.execute("""CREATE TRIGGER fixture_reject_receipt BEFORE UPDATE ON autonomy_meme_operations
                WHEN NEW.status='manifest_published' BEGIN SELECT RAISE(FAIL,'fixture: durable receipt unavailable'); END""")
    original_replace = os.replace
    cuts: list[str] = []
    def publication_boundary(source, target):
        target = Path(target)
        image_target = target.parent == library / 'happy'
        metadata_target = target.parent == private / 'metadata'
        manifest_target = target == manifest
        if not cuts and cut == 'before_image' and image_target:
            cuts.append(str(target))
            raise OSError('fixture: image publication did not start')
        result = original_replace(source, target)
        if not cuts and ((cut == 'after_metadata' and metadata_target) or (cut in {'after_manifest', 'concurrent_after_manifest'} and manifest_target)):
            cuts.append(str(target))
            raise OSError('fixture: lost response after actual file publication')
        return result
    monkeypatch.setattr(os, 'replace', publication_boundary)
    if cut == 'after_image_identity':
        from backend.app.proactive_runtime import meme_recovery
        original_fast = main._proactive.memes._fast_fact
        original_published = meme_recovery._published
        def unavailable_identity(role_id, target):
            if Path(target).parent == library / 'happy':
                cuts.append(str(target))
                raise OSError('fixture: identity proof unavailable after rename returned')
            return original_fast(role_id, target)
        def unresolved_recovery(target, *arguments):
            if Path(target).parent == library / 'happy':
                raise PermissionError('fixture: original final identity remains unconfirmed')
            return original_published(target, *arguments)
        monkeypatch.setattr(main._proactive.memes, '_fast_fact', unavailable_identity)
        monkeypatch.setattr(meme_recovery, '_published', unresolved_recovery)
    class Provider:
        calls = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.calls += 1
            assert self.calls <= 3, 'A partial file intent must not replay the model or copy'
            name, arguments = [('activate_skill', {'skill_id': 'meme-manage'}), ('read_file', {'path': '@memes/manifest.json'}),
                ('meme_copy_image', {'attachment_id': uploaded['id'], 'category': 'happy'})][self.calls - 1]
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('phase-' + str(self.calls), name, arguments),)))
    provider = Provider()
    monkeypatch.setattr(main, 'model_adapter', provider)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert state['runs'][0]['status'] == 'aborted'
    if cut == 'concurrent_after_manifest':
        from backend.app.proactive_runtime import meme_recovery
        original_published = meme_recovery._published
        entered, release = threading.Event(), threading.Event()
        actor: ContextVar[str] = ContextVar('drift191_recovery_actor', default='')
        observation_lock = threading.Lock()
        calls: dict[str, int] = {'A': 0, 'B': 0}
        observations: list[dict[str, Any]] = []
        background_deferred: list[int] = [0]
        release_by: list[str] = []
        def observe(phase, identity):
            with observation_lock:
                observations.append({'sequence': len(observations) + 1, 'actor': identity,
                    'phase': phase, 'thread': threading.get_ident(),
                    'aHeld': entered.is_set() and not release.is_set()})
        def release_fixture(identity):
            with observation_lock:
                if not release.is_set():
                    release_by.append(identity)
                    release.set()
        def controlled_reconcile(role_id):
            identity = actor.get()
            if identity not in calls:
                with observation_lock:
                    background_deferred[0] += 1
                return
            with observation_lock:
                calls[identity] += 1
            observe('entry', identity)
            try:
                original_reconcile(role_id)
            except BaseException:
                observe('error', identity)
                raise
            else:
                observe('return', identity)
                if identity == 'B':
                    release_fixture('B-original-reconcile-returned')
        def hold_first_original_fact(target, *arguments):
            result = original_published(target, *arguments)
            if Path(target) == manifest and actor.get() == 'A':
                assert not entered.is_set(), 'Only the designated A owner may hold this original fact once'
                entered.set()
                observe('original-manifest-fact-held', 'A')
                assert release.wait(15), 'Fixture watchdog must release only its own held fact'
            return result
        async def recover_as(identity):
            token = actor.set(identity)
            try:
                await main._proactive.recover()
            finally:
                actor.reset(token)
        monkeypatch.setattr(main._proactive.memes, 'reconcile', controlled_reconcile)
        monkeypatch.setattr(meme_recovery, '_published', hold_first_original_fact)
        watchdog = threading.Timer(2, release_fixture, args=('fixture-watchdog',))
        with ThreadPoolExecutor(max_workers=2) as requests:
            first = requests.submit(client.portal.call, recover_as, 'A')
            try:
                assert entered.wait(10)
                second = requests.submit(client.portal.call, recover_as, 'B')
                watchdog.start()
                first.result(20)
                second.result(20)
            finally:
                release_fixture('fixture-finally')
                watchdog.cancel()
                if watchdog.ident is not None:
                    watchdog.join()
        record_property('recovery_actor_calls', json.dumps(calls, sort_keys=True))
        record_property('recovery_actor_observations', json.dumps(observations, sort_keys=True))
        record_property('recovery_background_deferred', background_deferred[0])
        record_property('recovery_fixture_release_by', json.dumps(release_by))
        assert calls == {'A': 1, 'B': 1}, 'Both designated owners must invoke the original reconcile exactly once'
        for identity in calls:
            assert [event['phase'] for event in observations if event['actor'] == identity and event['phase'] in {'entry', 'return', 'error'}] == ['entry', 'return']
        entries = [event for event in observations if event['phase'] == 'entry']
        assert {event['actor'] for event in entries} == {'A', 'B'}
        assert len({event['thread'] for event in entries}) == 2
        assert next(event for event in entries if event['actor'] == 'B')['aHeld']
    operation = client.get(path + '/meme-library').json()['operations'][0]
    assert operation['status'] == expected_status and operation['physicalPhase'] == expected_phase, operation
    assert provider.calls == 3 and manifest.read_bytes() == manifest_raw
    images = list((library / 'happy').iterdir()) if (library / 'happy').exists() else []
    metadata = list((private / 'metadata').iterdir())
    assert len(images) == int(published_files > 0) and len(metadata) == int(published_files >= 2)
    if images:
        assert images[0].read_bytes() == image.getvalue()
    assert list((private / 'staging').iterdir()) == []


def test_recovery_waits_for_original_ordinary_owner_file_worker_before_classifying_or_cleaning_its_stage(api, monkeypatch):
    client, roles, _, _ = api
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    workspace = roles.workspace_path(role)
    started, release = threading.Event(), threading.Event()
    real_fsync = os.fsync
    def hold_original_owner_fsync(fd):
        real_fsync(fd)
        info = os.fstat(fd)
        stages = list((workspace / '.meido-memes/staging').glob('*.tmp'))
        if any((stage.stat().st_dev, stage.stat().st_ino) == (info.st_dev, info.st_ino) for stage in stages):
            started.set()
            release.wait()
    monkeypatch.setattr(os, 'fsync', hold_original_owner_fsync)
    class Owner:
        calls = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall('ordinary-stage', 'write_file', {'path': 'memes/new-owner.txt', 'content': 'original owned bytes'})
            else:
                yield ModelTextDelta('原主人文件worker完成')
    monkeypatch.setattr(main, 'model_adapter', Owner())
    with ThreadPoolExecutor(max_workers=1) as requests:
        pending = requests.submit(client.post, f'/api/roles/{role}/messages', json={'content': '原主人受管写入'})
        try:
            assert started.wait(10), 'The actual original FileTools fsync must remain owned'
            client.portal.call(main._proactive.recover)
            operation = client.get(path + '/meme-library').json()['operations'][0]
            assert operation['status'] == 'reserved' and operation['physicalPhase'] == 'reserved', operation
            assert len(list((workspace / '.meido-memes/staging').iterdir())) == 1
        finally:
            release.set()
        assert 'assistant_completed' in pending.result(20).text
    assert (workspace / 'memes/new-owner.txt').read_text(encoding='utf-8') == 'original owned bytes'
    assert client.get(path + '/meme-library').json()['operations'][0]['status'] == 'recorded'


@pytest.mark.parametrize('echo_surface', ['text_halves', 'text_small', 'argument_key', 'argument_fragments'])
def test_provider_normal_split_image_echo_is_removed_before_history_or_persistent_transcript(api, monkeypatch, echo_surface):
    client, roles, sessions, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    library = roles.workspace_path(role) / 'memes'
    (library / 'happy').mkdir()
    Image.new('RGB', (640, 480), '#29a3bc').save(library / 'happy/legacy.png')
    (library / 'manifest.json').write_text('{"version":1,"categories":{"happy":{"desc":"original","aliases":[],"enabled":true}}}', encoding='utf-8')
    class Provider:
        calls = 0
        fragments: tuple[str, ...] = ()
        clean_next_request = False
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.calls += 1
            if self.calls == 3:
                image = next(block for message in messages for block in message.content if isinstance(block, ModelImageBlock))
                midpoint = len(image.data_base64) // 2
                self.fragments = tuple(image.data_base64[offset:offset + 32] for offset in range(0, len(image.data_base64), 32)) if echo_surface in {'text_small', 'argument_fragments'} else (image.data_base64[:midpoint], image.data_base64[midpoint:])
                arguments: dict[str, Any] = {'path': '@memes/manifest.json'}
                if echo_surface == 'argument_key':
                    self.fragments = (image.data_base64,)
                    arguments[image.data_base64] = 'untrusted echo'
                if echo_surface == 'argument_fragments':
                    arguments['untrusted_echo'] = list(self.fragments)
                texts = tuple(ModelTextBlock(fragment) for fragment in self.fragments) if echo_surface.startswith('text_') else ()
                yield ModelStreamDone(ModelAssistantMessage((*texts, ModelToolCallBlock('echo-read', 'read_file', arguments))))
                return
            if self.calls == 4:
                serialized = json.dumps([asdict(message) for message in messages])
                self.clean_next_request = not any(fragment in serialized for fragment in self.fragments if len(fragment) >= 32)
            name, arguments = {1: ('activate_skill', {'skill_id': 'meme-manage'}), 2: ('read_file', {'path': '@memes/happy/legacy.png'}),
                4: ('finish_drift', {'skill_used': 'meme-manage', 'one_line': '核验输出安全投影', 'next_action': '下次维护', 'message_result': 'silent'})}[self.calls]
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('echo-' + str(self.calls), name, arguments),)))
    provider = Provider()
    monkeypatch.setattr(main, 'model_adapter', provider)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert state['runs'][0]['status'] == 'finished_silent'
    assert provider.clean_next_request, 'Provider image echoes must not become text history in the next request'
    assert all(fragment.encode('ascii') not in Path(sessions.database_path).read_bytes() for fragment in provider.fragments if len(fragment) >= 32)


@pytest.mark.parametrize('echo_surface', ['text', 'argument_content', 'text_after_prefix_noise',
    'thinking', 'thinking_signature', 'text_signature', 'tool_signature'])
@pytest.mark.parametrize('split', ['thirty_two', 'one_character_boundaries'])
def test_known_image_echo_cannot_be_reassembled_across_separate_responses_before_history_persistence_or_dispatch(api, monkeypatch, echo_surface, split):
    client, roles, sessions, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    library = roles.workspace_path(role) / 'memes'
    (library / 'happy').mkdir()
    Image.new('RGB', (1, 1), (0, 0, 0)).save(library / 'happy/legacy.png')
    before = {'version': 1, 'categories': {'happy': {'desc': '原类别', 'aliases': [], 'enabled': True}}}
    after = {'version': 1, 'categories': {'happy': {'desc': '正常业务分类仍可维护', 'aliases': ['保留别名'], 'enabled': True}}}
    (library / 'manifest.json').write_text(json.dumps(before, ensure_ascii=False), encoding='utf-8')

    def pieces(value):
        result = {}
        def visit(item):
            if isinstance(item, dict):
                content = item.get('content', [])
                if isinstance(content, list):
                    for call in content:
                        if isinstance(call, dict) and str(call.get('id', '')).startswith('cross-echo-'):
                            number = int(call['id'].removeprefix('cross-echo-'))
                            if echo_surface in {'text', 'text_after_prefix_noise'}:
                                result[number] = ''.join(block['text'] for block in content
                                    if isinstance(block, dict) and block.get('type') == 'text')
                            elif echo_surface == 'argument_content':
                                result[number] = call['arguments']['content']
                            elif echo_surface in {'thinking', 'thinking_signature'}:
                                field = 'text' if echo_surface == 'thinking' else 'signature'
                                result[number] = ''.join(block[field] for block in content
                                    if isinstance(block, dict) and block.get('type') == 'thinking')
                            elif echo_surface == 'text_signature':
                                result[number] = ''.join(block['text_signature'] for block in content
                                    if isinstance(block, dict) and block.get('type') == 'text')
                            else:
                                result[number] = call['thought_signature']
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
        visit(value)
        return result

    class Provider:
        calls = 0
        encoded = ''
        fragments: tuple[str, ...] = ()
        actual_image_requests = 0
        history_pieces: dict[int, set[str]] = {}
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.calls += 1
            for index, value in pieces([asdict(message) for message in messages]).items():
                self.history_pieces.setdefault(index, set()).add(value)
            images = [block for message in messages for block in message.content if isinstance(block, ModelImageBlock)]
            if images:
                assert self.calls == 4 and len(images) == 1
                self.actual_image_requests += 1
                self.encoded = images[0].data_base64
                with Image.open(io.BytesIO(base64.b64decode(self.encoded))) as actual:
                    assert actual.size == (1, 1) and actual.getpixel((0, 0)) == (0, 0, 0)
                assert 64 < len(self.encoded) <= 128
                edges = (0, 1, 31, 32, 63, 64, len(self.encoded)) if split == 'one_character_boundaries' else tuple(range(0, len(self.encoded), 32)) + (len(self.encoded),)
                self.fragments = tuple(self.encoded[start:end] for start, end in zip(edges, edges[1:]))
                assert ''.join(self.fragments) == self.encoded
            echo_start = 5 if echo_surface == 'text_after_prefix_noise' else 4
            if self.calls <= 3:
                name, arguments = [('activate_skill', {'skill_id': 'meme-manage'}),
                    ('read_file', {'path': '@memes/manifest.json'}),
                    ('read_file', {'path': '@memes/happy/legacy.png'})][self.calls - 1]
                yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('cross-setup-' + str(self.calls), name, arguments),)))
            elif self.calls == 4 and echo_surface == 'text_after_prefix_noise':
                yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock(self.encoded[0]),
                    ModelToolCallBlock('cross-noise', 'list_dir', {'path': '@memes/happy'}))))
            elif self.calls < echo_start + len(self.fragments):
                index = self.calls - echo_start
                fragment = self.fragments[index]
                name, arguments = ('write_file', {'path': 'echo-' + str(index) + '.txt', 'content': fragment}) if echo_surface == 'argument_content' else (
                    'list_dir', {'path': '@memes/happy'})
                text: tuple[Any, ...]
                if echo_surface in {'text', 'text_after_prefix_noise'}:
                    text = (ModelTextBlock(fragment),)
                elif echo_surface in {'thinking', 'thinking_signature'}:
                    text = (ModelThinkingBlock(text=fragment),) if echo_surface == 'thinking' else (ModelThinkingBlock(text='保留正常思考', signature=fragment),)
                elif echo_surface == 'text_signature':
                    text = (ModelTextBlock('保留正常说明', text_signature=fragment),)
                else:
                    text = ()
                call = ModelToolCallBlock('cross-echo-' + str(index), name, arguments,
                    thought_signature=fragment if echo_surface == 'tool_signature' else None)
                yield ModelStreamDone(ModelAssistantMessage((*text, call)))
            elif self.calls == echo_start + len(self.fragments):
                yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('cross-business-write', 'write_file',
                    {'path': '@memes/manifest.json', 'content': json.dumps(after, ensure_ascii=False)}),)))
            else:
                assert self.calls == echo_start + len(self.fragments) + 1, 'Normal classification/finish must remain finite and usable'
                yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('cross-business-finish', 'finish_drift',
                    {'skill_used': 'meme-manage', 'one_line': '完成正常类别维护', 'next_action': '下次继续维护正常类别', 'message_result': 'silent'}),)))
    provider = Provider()
    monkeypatch.setattr(main, 'model_adapter', provider)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert state['runs'][0]['status'] == 'finished_silent', json.dumps(state['runs'], ensure_ascii=False)
    assert provider.actual_image_requests == 1
    assert json.loads((library / 'manifest.json').read_text(encoding='utf-8')) == after
    skill = next(item for item in client.get(path + '/drift-skills').json()['skills'] if item['id'] == 'meme-manage')
    assert skill['progress']['nextAction'] == '下次继续维护正常类别'
    with sqlite3.connect(sessions.database_path) as db:
        durable_pieces: dict[int, set[str]] = {}
        for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = '"' + table.replace('"', '""') + '"'
            for row in db.execute('SELECT * FROM ' + quoted):
                for value in row:
                    if isinstance(value, str):
                        assert provider.encoded not in value, 'No durable text field may contain the known raw image'
                        try:
                            document = json.loads(value)
                        except (ValueError, TypeError):
                            continue
                        for index, text in pieces(document).items():
                            durable_pieces.setdefault(index, set()).add(text)
    assert not all(fragment in durable_pieces.get(index, set()) for index, fragment in enumerate(provider.fragments)), 'No selection from all persisted responses may reassemble the known image'
    assert not all(fragment in provider.history_pieces.get(index, set()) for index, fragment in enumerate(provider.fragments)), 'No selection from all later provider histories may reassemble the known image'
    if echo_surface == 'argument_content':
        private = roles.workspace_path(role) / 'drift'
        published = ''.join((private / ('echo-' + str(index) + '.txt')).read_text(encoding='utf-8') for index in range(len(provider.fragments)))
        assert published != provider.encoded, 'Tool dispatch must not publish the completing known-image fragment'


@pytest.mark.parametrize('category', ['happy/../secret', 'happy/nested', 'manifest.json', 'secret'])
def test_copy_requires_an_exact_canonical_category_in_the_frozen_finite_grant_before_any_stage_or_publication(api, monkeypatch, category):
    client, roles, _, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, endpoint = configure_meme(client, model)
    output = io.BytesIO()
    Image.new('RGB', (720, 540), '#637f91').save(output, 'PNG')
    uploaded = client.post(f'/api/roles/{role}/attachments', content=output.getvalue(),
        headers={'x-attachment-filename': 'category-fixture.png', 'content-type': 'image/png'})
    assert uploaded.status_code == 200, uploaded.text
    source = uploaded.json()
    config = client.get(endpoint).json()['config']
    saved = client.put(endpoint, json={'expectedRevision': config['revision'], 'config': {**config,
        'driftMeme': {**config['driftMeme'], 'copyEnabled': True, 'sourceAttachmentIds': [source['id']]}}})
    assert saved.status_code == 200, saved.text
    library = roles.workspace_path(role) / 'memes'
    (library / 'happy').mkdir()
    (library / 'secret').mkdir()
    item = {'desc': '已有主人类别', 'aliases': [], 'enabled': True}
    original = json.dumps({'version': 1, 'categories': {'happy': item, 'secret': item, category: item}}, ensure_ascii=False)
    (library / 'manifest.json').write_text(original, encoding='utf-8')
    before = {str(path.relative_to(library)): path.read_bytes() for path in library.rglob('*') if path.is_file()}
    class Provider:
        calls = 0
        copy_result = None
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.calls += 1
            if self.calls <= 3:
                name, arguments = [('activate_skill', {'skill_id': 'meme-manage'}),
                    ('read_file', {'path': '@memes/manifest.json'}),
                    ('meme_copy_image', {'attachment_id': source['id'], 'category': category})][self.calls - 1]
            else:
                assert self.calls == 4
                self.copy_result = json.loads(messages[-1].content[0].text)
                name, arguments = 'finish_drift', {'skill_used': 'meme-manage', 'one_line': '有限类别拒绝完成',
                    'next_action': '保留主人有限类别许可', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('category-' + str(self.calls), name, arguments),)))
    provider = Provider()
    monkeypatch.setattr(main, 'model_adapter', provider)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert provider.calls == 4 and state['runs'][0]['status'] == 'finished_silent', state
    assert {str(path.relative_to(library)): path.read_bytes() for path in library.rglob('*') if path.is_file()} == before, 'An out-of-grant category must not publish any image even when the tool reports rejected'
    assert isinstance(provider.copy_result, dict) and provider.copy_result.get('status') != 'recorded'
    assert client.get(endpoint + '/meme-library').json()['operations'] == [], 'Reject category before reserving journal or staging'
    assert not (library.parent / '.meido-memes/staging').exists()


@pytest.mark.parametrize('image_mapping', ['one_each_request', 'same_payload_twice', 'history_reintroduced'])
def test_actual_provider_send_spends_the_original_run_durable_image_ledger_before_socket_and_refuses_fifth_exposure(api, monkeypatch, image_mapping):
    from backend.app.ai import ModelAdapterRegistry, RoutedModelAdapter
    from dataclasses import replace
    client, roles, sessions, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    arrivals: list[dict[str, Any]] = []
    endpoint = {'path': ''}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            document = json.loads(body)
            images = [part['image_url']['url'].split(';base64,')[1] for message in document['messages']
                for part in (message.get('content') or []) if isinstance(part, dict) and part.get('type') == 'image_url']
            ledger = client.get(endpoint['path'] + '/meme-library').json().get('modelRequests', [])
            arrivals.append({'sha256': hashlib.sha256(body).hexdigest(), 'bodyBytes': len(body),
                'imageOccurrences': len(images), 'imageBytes': sum(len(value) for value in images),
                'persistedBeforeArrival': len(ledger)})
            number = len(arrivals)
            name, arguments = ('activate_skill', {'skill_id': 'meme-manage'}) if number == 1 else ('read_file', {'path': '@memes/happy/legacy.png'})
            events = [{'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'wire-' + str(number), 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}]}}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}]
            raw = (''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(raw)
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), name='drift191-real-provider')
    thread.start()
    try:
        model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local', 'api': 'openai-completions',
            'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
        role, path = configure_meme(client, model)
        endpoint['path'] = path
        library = roles.workspace_path(role) / 'memes'
        (library / 'happy').mkdir()
        Image.new('RGB', (640, 480), '#a28437').save(library / 'happy/legacy.png')
        routed = RoutedModelAdapter(ModelAdapterRegistry(timeout=2))
        class PublicImageMapping:
            """Exercise supported message mapping, then use the original serializer and real socket."""
            previous = None
            async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
                mapped = []
                for message in messages:
                    content = list(message.content)
                    for image in message.content:
                        if isinstance(image, ModelImageBlock):
                            if image_mapping == 'same_payload_twice':
                                content.append(image)
                            elif image_mapping == 'history_reintroduced':
                                if self.previous is not None:
                                    content.append(self.previous)
                                self.previous = image
                    mapped.append(replace(message, content=tuple(content)))
                async for event in routed.stream_rich_messages(tuple(mapped), configuration, tools,
                        max_tokens=max_tokens, cancel_check=cancel_check):
                    yield event
        monkeypatch.setattr(main, 'model_adapter', PublicImageMapping())
        now[0] += 1800
        state = client.portal.call(settled, main._proactive, role)
        expected = {'one_each_request': [0, 0, 1, 1, 1, 1], 'same_payload_twice': [0, 0, 2, 2],
            'history_reintroduced': [0, 0, 1, 2]}[image_mapping]
        assert [item['imageOccurrences'] for item in arrivals] == expected
        assert [item['persistedBeforeArrival'] for item in arrivals] == list(range(1, len(expected) + 1))
        assert state['runs'][0]['status'] == 'aborted' and 'run_image_occurrences' in state['runs'][0]['error']
        ledger = client.get(path + '/meme-library').json()['modelRequests']
        assert sum(item['imageOccurrences'] for item in ledger) == sum(expected)
        assert sum(item['imageInputEstimate'] for item in ledger) == sum(expected) * 8192
        assert sum(item['imageBase64Bytes'] for item in ledger) == sum(item['imageBytes'] for item in arrivals)
        assert {item['bodySha256'] for item in ledger} == {item['sha256'] for item in arrivals}
        assert sum(item['bodyBytes'] for item in ledger) == sum(item['bodyBytes'] for item in arrivals)
        assert all(item['envelopeBytes'] <= 8192 and item['wireNonImageBytes'] <= 56 * 1024 for item in ledger)
        assert 'data:image/' not in Path(sessions.database_path).read_text(encoding='utf-8', errors='ignore')
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        assert not thread.is_alive()


@pytest.mark.parametrize('changed', ['library_root', 'builtin_support_hash'])
def test_frozen_library_root_or_full_builtin_bundle_change_stops_the_old_run_before_further_model_requests(api, monkeypatch, tmp_path, changed):
    from backend.app.proactive_runtime import drift_skills, meme_profile
    client, roles, _, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    fixture_builtin = tmp_path / 'host-owned-builtin'
    # Exact fixed original bytes; fault injection never changes repository files.
    shutil.copytree(drift_skills.BUILTIN_ROOT / 'meme-manage', fixture_builtin / 'meme-manage')
    monkeypatch.setattr(drift_skills, 'BUILTIN_ROOT', fixture_builtin)
    monkeypatch.setattr(meme_profile, 'BUILTIN_ROOT', fixture_builtin)
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    workspace = roles.workspace_path(role)
    library = workspace / 'memes'
    (library / 'happy').mkdir()
    Image.new('RGB', (640, 480), '#835ca9').save(library / 'happy/legacy.png')
    class Provider:
        calls = 0
        images = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.calls += 1
            self.images += sum(isinstance(block, ModelImageBlock) for message in messages for block in message.content)
            if self.calls == 2:
                if changed == 'library_root':
                    retired = workspace / 'owner-retired-memes'
                    assert library.is_relative_to(workspace) and retired.is_relative_to(workspace)
                    library.rename(retired)
                    (library / 'happy').mkdir(parents=True)
                    shutil.copy2(retired / 'happy/legacy.png', library / 'happy/legacy.png')
                else:
                    support = fixture_builtin / meme_profile.PROFILE_PATH
                    support.write_bytes(support.read_bytes() + b'\n')
            steps = [('activate_skill', {'skill_id': 'meme-manage'}), ('read_file', {'path': '@memes/happy/legacy.png'}),
                ('finish_drift', {'skill_used': 'meme-manage', 'one_line': '旧运行不能采用新目录或指令', 'next_action': '重新核对', 'message_result': 'silent'})]
            name, arguments = steps[self.calls - 1]
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('changed-' + str(self.calls), name, arguments),)))
    provider = Provider()
    monkeypatch.setattr(main, 'model_adapter', provider)
    now[0] += 1800
    state = client.portal.call(settled, main._proactive, role)
    assert provider.calls == 2 and provider.images == 0
    assert state['runs'][0]['status'] == 'aborted'
    assert client.get(path + '/meme-library').json()['operations'] == []


def test_peak_quota_reserves_the_full_persisted_json_journal_bytes_including_text_escaping(api, monkeypatch):
    client, roles, _, _ = api
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    library = roles.workspace_path(role) / 'memes'
    original_size = 61 * 1024 * 1024 + 512 * 1024
    with (library / 'old-owner.bin').open('wb') as stream:
        for _ in range(123):
            stream.write(b'o' * (512 * 1024))
    class Owner:
        calls = 0
        result = ''
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall('json-journal-peak', 'write_file', {'path': 'memes/new-owner.txt', 'content': '"' * (1024 * 1024)})
            else:
                self.result = next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('已核对真实持久计划字节')
    adapter = Owner()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    response = client.post(f'/api/roles/{role}/messages', json={'content': '写入必须先预留文件和完整JSON转义后的持久计划'})
    assert 'assistant_completed' in response.text, response.text[-2000:]
    assert 'quota' in adapter.result, adapter.result
    assert not (library / 'new-owner.txt').exists()
    assert (library / 'old-owner.bin').stat().st_size == original_size
    assert client.get(path + '/meme-library').json()['operations'] == []


def test_fully_rebuilt_disabled_application_preserves_pending_copy_and_owner_actually_edits_and_reads_the_same_new_image(api, tmp_path, tmp_path_factory):
    from backend.app.task_runtime import TaskOrigin, TaskBudget
    from backend.app.task_runtime.process import ProcessExecutor
    _, _, _, tasks = api
    # Keep the real Windows attachment/staging paths below MAX_PATH; each run
    # still owns fresh absolute roots, HOME and TEMP before child app import.
    persistent = tmp_path_factory.mktemp('r21')
    fixture = Path(__file__).resolve().parents[1] / 'fixtures/drift_meme_rebuild.py'
    reports = []
    for phase in (1, 2):
        origin = TaskOrigin('drift191-pending-rebuild', 'role:drift191-pending-rebuild', 'phase-' + str(phase))
        executor = ProcessExecutor([sys.executable, '-I', '-S', str(fixture), str(persistent), str(phase), 'pending-disabled'], tmp_path,
            dict(os.environ, MEIDO_TEST_SITE_PATHS=_REBUILD_IMPORT_PATHS))
        identifier = tasks.submit(origin, executor, budget=TaskBudget(60, 60000), kind='shell', description='Owned pending copy application rebuild fixture')
        while True:
            result = tasks.query(identifier, origin, wait_seconds=.2)
            if result.ended_at is not None:
                break
        assert result.status == 'succeeded' and result.exit_code == 0 and not result.log_truncated, result.output
        reports.append(json.loads((persistent / ('phase-' + str(phase) + '-result.json')).read_text(encoding='utf-8')))
    assert reports[0]['pid'] != reports[1]['pid'] and reports[0]['roleId'] == reports[1]['roleId']
    assert reports[0]['originalStatus'] == 'cleanup_pending' and reports[1]['recoveredStatus'] == 'conflict'
    assert reports[0]['copiedSha256'] == reports[1]['copiedSha256']
    assert reports[1]['actualImageRequests'] == 1 and all(item['modelRequests'] == 3 for item in reports)
    assert all(item['memory'] and item['channels'] and item['guardRestoredAfterOriginalOwners'] and item['newThreads'] == [] for item in reports)


def test_owner_admitted_before_disable_cannot_turn_into_an_unlocked_write_and_fresh_disabled_owner_keeps_original_path_order(api, monkeypatch):
    from backend.app.agent_runtime.file_tools import _file_lock
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    client, roles, _, _ = api
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    target = roles.workspace_path(role) / 'memes/manifest.json'
    original = '{"version":1,"categories":{"happy":{"desc":"original","aliases":[],"enabled":true}}}'
    desired = '{"version":1,"categories":{"happy":{"desc":"fresh owner","aliases":[],"enabled":true}}}'
    target.write_text(original, encoding='utf-8')
    info = target.stat()
    target_identity = info.st_dev, info.st_ino
    announced, paused, release, retry_announced = (threading.Event() for _ in range(4))
    real_read = os.read
    def pause_original_preparation(fd, count):
        raw = real_read(fd, count)
        current = os.fstat(fd)
        if announced.is_set() and not paused.is_set() and (current.st_dev, current.st_ino) == target_identity:
            paused.set()
            release.wait()
        return raw
    monkeypatch.setattr(os, 'read', pause_original_preparation)
    class Owner:
        calls = 0
        retry = False
        retry_calls = 0
        original_result = ''
        async def stream_structured_messages(self, messages, configuration, tools):
            if self.retry:
                self.retry_calls += 1
                if self.retry_calls == 1:
                    retry_announced.set()
                    yield ModelToolCall('fresh-disabled-write', 'write_file', {'path': 'memes/manifest.json', 'content': desired})
                else:
                    yield ModelTextDelta('新普通owner按原锁完成')
                return
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall('race-owner-read', 'read_file', {'path': 'memes/manifest.json'})
            elif self.calls == 2:
                announced.set()
                yield ModelToolCall('race-owner-write', 'write_file', {'path': 'memes/manifest.json', 'content': desired})
            else:
                self.original_result = next(item['content'] for item in reversed(messages) if item['role'] == 'tool')
                yield ModelTextDelta('已核对原受管准入')
    adapter = Owner()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    config = client.get(path).json()['config']
    watchdog_fired = threading.Event()
    def fixture_watchdog():
        if not release.wait(20):
            watchdog_fired.set()
            release.set()
    watchdog = threading.Thread(target=fixture_watchdog, name='drift191-owner-race-release')
    watchdog.start()
    requests, locks = ThreadPoolExecutor(max_workers=2), ExitStack()
    locks.enter_context(_file_lock(target, ToolContext(role, 'fixture-path-order', 'fixture-path-order', CancellationToken())))
    try:
        pending = requests.submit(client.post, f'/api/roles/{role}/messages', json={'content': '关闭前已进入的受管owner操作'})
        assert paused.wait(10)
        disabled = requests.submit(client.put, path, json={'expectedRevision': config['revision'], 'config': {**config,
            'driftMeme': {**config['driftMeme'], 'enabled': False}}}).result(timeout=5)
        assert disabled.status_code == 200, disabled.text
        release.set()
        assert 'assistant_completed' in pending.result(5).text
        assert target.read_text(encoding='utf-8') == original, 'An old managed attempt must never replace without the shared path lock after disable'
        assert 'meme_permission_changed' in adapter.original_result
        adapter.retry = True
        fresh = requests.submit(client.post, f'/api/roles/{role}/messages', json={'content': '停用后新的普通owner直接写，不需先read'})
        assert retry_announced.wait(5)
        with pytest.raises(TimeoutError):
            fresh.result(timeout=.1)
        assert target.read_text(encoding='utf-8') == original
        locks.close()
        assert 'assistant_completed' in fresh.result(5).text
    finally:
        release.set()
        locks.close()
        requests.shutdown(wait=True)
        watchdog.join()
    assert not watchdog_fired.is_set() and target.read_text(encoding='utf-8') == desired


@pytest.mark.parametrize('first_seal', ['human_input', 'disable'])
def test_new_owner_input_and_repeated_stop_preserve_the_original_slow_image_provider_finally_until_join(api, monkeypatch, first_seal):
    client, roles, _, _ = api
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-vision-fixture', 'apiKey': 'fixture'}).json()['configuration']
    role, path = configure_meme(client, model)
    library = roles.workspace_path(role) / 'memes'
    (library / 'happy').mkdir()
    Image.new('RGB', (640, 480), '#987342').save(library / 'happy/legacy.png')
    target = library / 'manifest.json'
    target.write_text('{"version":1,"categories":{}}', encoding='utf-8')
    entered, finally_started, release, joined = (threading.Event() for _ in range(4))
    watchdog_fired = threading.Event()
    desired = '{"version":1,"categories":{"happy":{"desc":"主人在旧provider清理期间保存","aliases":[],"enabled":true}}}'

    class SlowImageAndOwner:
        drift_calls = 0
        owner_calls = 0
        finally_calls = 0
        finally_interrupted = False
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            if any('主人在慢图片provider仍被持有时继续编辑' in getattr(block, 'text', '') for message in messages for block in message.content):
                self.owner_calls += 1
                steps = [('read_file', {'path': 'memes/manifest.json'}),
                    ('write_file', {'path': 'memes/manifest.json', 'content': desired})]
                block = (ModelToolCallBlock('owner-live-' + str(self.owner_calls), *steps[self.owner_calls - 1])
                    if self.owner_calls <= 2 else ModelTextBlock('主人编辑已按真实文件完成'))
                yield ModelStreamDone(ModelAssistantMessage((block,)))
                return
            self.drift_calls += 1
            if self.drift_calls <= 2:
                name, arguments = ('activate_skill', {'skill_id': 'meme-manage'}) if self.drift_calls == 1 else (
                    'read_file', {'path': '@memes/happy/legacy.png'})
                yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock('slow-provider-' + str(self.drift_calls), name, arguments),)))
                return
            assert self.drift_calls == 3
            assert any(isinstance(block, ModelImageBlock) for message in messages for block in message.content)
            entered.set()
            try:
                await asyncio.sleep(3600)
            finally:
                self.finally_calls += 1
                finally_started.set()
                try:
                    while not release.is_set():
                        await asyncio.sleep(.01)
                except asyncio.CancelledError:
                    self.finally_interrupted = True
                    raise
                finally:
                    joined.set()

    adapter = SlowImageAndOwner()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    def release_only_fixture_on_timeout():
        if not release.wait(25):
            watchdog_fired.set()
            release.set()
    watchdog = threading.Thread(target=release_only_fixture_on_timeout, name='drift191-provider-fixture-release')
    watchdog.start()
    requests = ThreadPoolExecutor(max_workers=2)
    try:
        now[0] += 1800
        requests.submit(client.portal.call, main._proactive.on_clock).result(timeout=5)
        assert entered.wait(10), 'The original provider must actually receive the decoded image'
        if first_seal == 'disable':
            config = client.get(path).json()['config']
            disabled = requests.submit(client.put, path, json={'expectedRevision': config['revision'], 'config': {
                **config, 'driftMeme': {**config['driftMeme'], 'enabled': False}}}).result(timeout=5)
            assert disabled.status_code == 200, disabled.text
        owner = requests.submit(client.post, f'/api/roles/{role}/messages',
            json={'content': '主人在慢图片provider仍被持有时继续编辑'}).result(timeout=5)
        assert 'assistant_completed' in owner.text, owner.text
        assert target.read_text(encoding='utf-8') == desired
        assert finally_started.wait(5) and not joined.is_set()
        for _ in range(3):
            stopped = requests.submit(client.post, path + '/stop').result(timeout=5)
            assert stopped.status_code == 202, stopped.text
        config = client.get(path).json()['config']
        if config['driftMeme']['enabled']:
            disabled = client.put(path, json={'expectedRevision': config['revision'], 'config': {
                **config, 'driftMeme': {**config['driftMeme'], 'enabled': False}}})
            assert disabled.status_code == 200, disabled.text
            config = disabled.json()['config']
        blocked = client.put(path, json={'expectedRevision': config['revision'], 'config': {
            **config, 'driftMeme': {**config['driftMeme'], 'enabled': True}}})
        assert blocked.status_code == 409, blocked.text
        now[0] += 1800
        assert requests.submit(client.portal.call, main._proactive.on_clock).result(timeout=5) == 0
        waiting = client.get(path).json()
        assert waiting['cleanupPending'] and len(waiting['runs']) == 1
        assert not waiting['runs'][0]['runtimeAcknowledged']
        assert not joined.is_set() and adapter.finally_calls == 1 and not adapter.finally_interrupted
    finally:
        release.set()
        requests.shutdown(wait=True)
        watchdog.join()
    assert not watchdog_fired.is_set()
    state = client.portal.call(settled, main._proactive, role)
    assert joined.is_set() and adapter.finally_calls == 1 and not adapter.finally_interrupted
    assert adapter.drift_calls == 3, 'The original sealed turn must not make another model request'
    assert state['runs'][0]['runtimeAcknowledged'] and state['runs'][0]['status'] == 'aborted'
    assert target.read_text(encoding='utf-8') == desired
    config = client.get(path).json()['config']
    enabled = client.put(path, json={'expectedRevision': config['revision'], 'config': {
        **config, 'driftMeme': {**config['driftMeme'], 'enabled': True}}})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()['config']['driftMeme']['managedEpoch'] != config['driftMeme']['managedEpoch']
