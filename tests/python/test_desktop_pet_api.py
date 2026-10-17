"""Owner-visible desktop pet package and binding behavior; no real model account."""
import io
import json
import zipfile
import os
import sys
import time
from pathlib import Path
import sysconfig

from PIL import Image
import pytest

from backend.app import main
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api as spawn_api


pytestmark = pytest.mark.usefixtures('local_platform_only')


@pytest.fixture
def api(spawn_api, monkeypatch):
    client, roles, sessions, tasks = spawn_api
    proof = {'memory': False, 'channels': False, 'errors': []}
    if not hasattr(main, '_pet195_owner_proofs'):
        main._pet195_owner_proofs = []
    main._pet195_owner_proofs.append(proof)
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


def package_bytes():
    image = Image.new('RGBA', (1536, 1872))
    for row, count in enumerate((6, 8, 8, 4, 5, 8, 6, 6, 6)):
        for column in range(count):
            image.paste((30 + row * 20, 70 + column * 15, 150, 255),
                (column * 192 + 32, row * 208 + 32, column * 192 + 160, row * 208 + 176))
    encoded = io.BytesIO()
    image.save(encoded, 'WEBP', lossless=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('fixture/pet.json', json.dumps({'id': 'fixture-pet', 'displayName': '本地测试桌宠',
            'description': '自制几何素材', 'spritesheetPath': 'sprite.webp', 'actions': {'wave': 'waving'}}))
        archive.writestr('fixture/sprite.webp', encoded.getvalue())
    return output.getvalue(), encoded.getvalue()


def test_owner_imports_real_shiori_package_binds_it_and_retains_default_off(api):
    client, _, _, _ = api
    role = client.post('/api/roles', json={'name': '桌宠主人', 'profile': {'profile': '本地验收'},
        'agentConfig': {'enabledTools': ['read_file'], 'shell': {'enabled': False}}}).json()['role']
    path = f"/api/roles/{role['id']}/desktop-pet"
    initial = client.get(path)
    assert initial.status_code == 200
    assert initial.json()['enabled'] is False and initial.json()['packages'] == []
    raw, sprite = package_bytes()
    imported = client.post(path + '/packages', content=raw, headers={'content-type': 'application/zip'})
    assert imported.status_code == 201, imported.text
    state = imported.json()
    assert state['enabled'] is False and state['selectedPackageId'] is None
    package = state['packages'][0]
    assert package['id'] == 'fixture-pet' and package['format'] == 'codex-sprite@1'
    assert package['actions'] == {'wave': 'waving'}
    bound = client.put(path, json={'expectedRevision': state['revision'], 'enabled': False,
        'packageId': package['id']})
    assert bound.status_code == 200, bound.text
    assert bound.json()['selectedPackageId'] == package['id'] and bound.json()['enabled'] is False
    actual = client.get(path + '/packages/' + package['id'] + '/assets/sprite')
    assert actual.status_code == 200 and actual.content == sprite
    assert actual.headers['content-type'].startswith('image/webp')
    unchanged = client.get('/api/roles/' + role['id']).json()['role']
    assert unchanged['agentConfig'] == role['agentConfig']


def test_bad_archive_cross_role_and_stale_settings_do_not_replace_owner_package(api):
    client, _, _, _ = api
    role = client.post('/api/roles', json={'name': '原角色', 'profile': {'profile': '测试'}}).json()['role']
    other = client.post('/api/roles', json={'name': '另一个', 'profile': {'profile': '测试'}}).json()['role']
    path = f"/api/roles/{role['id']}/desktop-pet"
    raw, _ = package_bytes()
    state = client.post(path + '/packages', content=raw).json()
    assert client.put(f"/api/roles/{other['id']}/desktop-pet", json={
        'expectedRevision': 0, 'packageId': 'fixture-pet', 'enabled': False}).status_code == 400
    assert client.put(path, json={'expectedRevision': 0, 'packageId': 'fixture-pet', 'enabled': False}).status_code == 400
    malformed = io.BytesIO()
    with zipfile.ZipFile(malformed, 'w') as archive:
        archive.writestr('../pet.json', '{}')
    assert client.post(path + '/packages', content=malformed.getvalue()).status_code == 400
    assert client.post(path + '/packages', content=raw).status_code == 400
    assert client.get(path).json()['packages'] == state['packages']


def test_http_tool_calls_enforce_one_real_run_cooldown_and_default_off_after_revocation(api, monkeypatch):
    from dataclasses import replace
    from backend.app.agent_runtime import ToolContext, CancellationToken
    from types import SimpleNamespace
    from backend.app.desktop_pet import application as pet_application, host as pet_host
    from backend.app.model_adapter import ModelTextDelta, ModelToolCall
    from backend.app.agent_runtime.types import ToolResultMessage
    class Host:
        def __init__(self, root, *, on_event):
            self.closed = False
            self.events, self.count = on_event, 0
        def start(self, binding):
            self.binding = binding
        def close(self):
            self.closed = True
        def snapshot(self):
            return {'status': 'stopped' if self.closed else 'ready', 'reason': ''}
        def action(self, identifier, arguments):
            self.count += 1
            return {'type': 'finished', 'actionId': identifier, 'roleId': self.binding['roleId']}
    monkeypatch.setattr(pet_host, 'PetHost', Host)
    monkeypatch.setattr(pet_application, 'sys', SimpleNamespace(platform='win32'))
    client, _, sessions, _ = api
    role = client.post('/api/roles', json={'name': '工具验收', 'profile': {'profile': '测试'},
        'agentConfig': {'enabledTools': ['tool_search']}}).json()['role']
    path = f"/api/roles/{role['id']}/desktop-pet"
    raw, _ = package_bytes()
    state = client.post(path + '/packages', content=raw).json()
    enabled = client.put(path, json={'expectedRevision': state['revision'], 'packageId': 'fixture-pet', 'enabled': True}).json()
    host = main._desktop_pet_application().host
    class Adapter:
        phase = 0
        stale_tool = None
        async def stream_structured_messages(self, messages, configuration, tools):
            available = {tool['function']['name'] for tool in tools}
            if 'pet_action' not in available:
                yield ModelToolCall('find-pet', 'tool_search', {'query': 'select:pet_action'})
            elif self.phase == 0:
                self.phase += 1
                current = sessions.active_run(role['id'])
                context = ToolContext(role['id'], current.sessionKey, current.runId, CancellationToken())
                tool = next(item for item in main._runtime_tool_candidates(main.store.get(role['id'])) if item.definition.name == 'pet_action')
                args = {'action': 'play', 'name': 'wave'}
                assert (await tool.execute(args, replace(context, role_id='another-role'))).is_error
                assert (await tool.execute(args, replace(context, session_key='thread:forged-owner'))).is_error
                snapshot = current.modelSnapshot
                sessions.update_run(current.runId, status='running', model_snapshot={**snapshot, 'humanInputSource': 'proactive'})
                assert (await tool.execute(args, context)).is_error
                sessions.update_run(current.runId, status='running', model_snapshot=snapshot)
                if self.stale_tool is not None:
                    stale = await self.stale_tool.execute(args, context)
                    assert stale.is_error and '绑定已改变' in stale.content
                yield ModelToolCall('first-pet', 'pet_action', {'action': 'play', 'name': 'wave'})
                yield ModelToolCall('second-pet', 'pet_action', {'action': 'play', 'name': 'wave'})
            else:
                yield ModelTextDelta('已读取实际结果')
    adapter = Adapter()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '同回合两次'})
    assert 'assistant_completed' in response.text, response.text
    results = [item for item in sessions.runtime_messages(f"role:{role['id']}")
        if isinstance(item, ToolResultMessage) and item.tool_name == 'pet_action']
    assert len(results) == 2 and not results[0].is_error and results[1].is_error
    assert '每个真实回合最多一次' in results[1].content and host.count == 1
    adapter.stale_tool = next(item for item in main._runtime_tool_candidates(main.store.get(role['id'])) if item.definition.name == 'pet_action')
    enabled = client.post(path + '/restart').json()
    adapter.phase = 0
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '立即再次请求'})
    assert 'assistant_completed' in response.text, response.text
    results = [item for item in sessions.runtime_messages(f"role:{role['id']}")
        if isinstance(item, ToolResultMessage) and item.tool_name == 'pet_action']
    assert all(item.is_error and '至少3秒' in item.content for item in results[-2:]) and host.count == 1
    stopped = client.put(path, json={'expectedRevision': enabled['revision'], 'packageId': 'fixture-pet', 'enabled': False})
    assert stopped.json()['enabled'] is False and host.closed and main._desktop_pet_application().host is None
    # Generic enabledTools cannot reinstate the independent pet grant.
    value = main._runtime_capability_policy(main.store.get(role['id']), include_channel=False)
    assert 'pet_action' not in value.enabled_tools
    removed = client.delete(path + '/packages/fixture-pet?expectedRevision=' + str(stopped.json()['revision']))
    assert removed.status_code == 200 and removed.json()['packages'] == []


@pytest.mark.skipif(sys.platform != 'win32' or os.environ.get('MEIDO_PET_NATIVE_TEST') != '1',
    reason='explicit Windows own-window acceptance required')
def test_native_owner_http_discovery_move_play_restart_disable_and_disconnect(api, monkeypatch, tmp_path):
    from backend.app.model_adapter import ModelTextDelta, ModelToolCall
    from backend.app.agent_runtime.types import ToolResultMessage
    client, roles, sessions, _ = api
    role = client.post('/api/roles', json={'name': '原生验收', 'profile': {'profile': '仅本窗口'},
        'agentConfig': {'enabledTools': ['tool_search']}}).json()['role']
    path = f"/api/roles/{role['id']}/desktop-pet"
    raw, _ = package_bytes()
    state = client.post(path + '/packages', content=raw).json()
    enabled = client.put(path, json={'expectedRevision': state['revision'], 'packageId': 'fixture-pet', 'enabled': True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()['host']['status'] == 'ready', enabled.text
    original = main._desktop_pet_application()
    first_host = original.host
    window = enabled.json()['host']['window']
    assert window['visible'] and window['alwaysOnTop'] and not window['focusable'] and window['transparent']
    commands = [{'action': 'move', 'target': target, 'animation': 'run'}
        for target in ('top_left', 'top_right', 'center', 'bottom_left', 'bottom_right')]
    commands.append({'action': 'play', 'name': 'wave'})
    class Adapter:
        command = None
        phase = 0
        called = False
        async def stream_structured_messages(self, messages, configuration, tools):
            self.phase += 1
            available = {tool['function']['name']: tool for tool in tools}
            if 'pet_action' not in available:
                yield ModelToolCall('discover-pet', 'tool_search', {'query': 'select:pet_action'})
            elif not self.called:
                self.called = True
                assert available['pet_action']['function']['parameters']['properties']['name']['enum'] == ['wave']
                yield ModelToolCall('native-pet', 'pet_action', self.command)
            else:
                yield ModelTextDelta('已确认本窗口动作回执')
    adapter = Adapter()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    evidence = []
    for command in commands:
        # Actual time passes; no cooldown clock mocking in native acceptance.
        time.sleep(3.05)
        adapter.command = command
        adapter.phase = 0
        adapter.called = False
        response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '执行当前桌宠动作'})
        assert 'assistant_completed' in response.text, response.text
        result = [item for item in sessions.runtime_messages(f"role:{role['id']}")
            if isinstance(item, ToolResultMessage) and item.tool_name == 'pet_action'][-1]
        assert not result.is_error and result.details['type'] == 'finished', result
        assert result.details['frames'] >= (12 if command['action'] == 'play' else 24)
        history = client.get(path).json()['history']
        receipts = [item['type'] for item in history if item.get('actionId') == result.details['actionId']]
        assert receipts == ['accepted', 'started', 'finished']
        evidence.append({'command': command, 'result': result.details, 'receipts': receipts})
    restarted = client.post(path + '/restart')
    assert restarted.status_code == 200 and restarted.json()['host']['status'] == 'ready', restarted.text
    assert first_host.closed and first_host.job.exit_confirmed and all(not thread.is_alive() for thread in first_host._threads)
    second_host = original.host
    # Re-enter the original ASGI lifespan rather than adopting native cleanup into another loop.
    state = client.get(path).json()
    stopped = client.put(path, json={'expectedRevision': state['revision'], 'packageId': 'fixture-pet', 'enabled': False})
    assert stopped.status_code == 200 and stopped.json()['host']['status'] == 'stopped'
    assert second_host.closed and second_host.job.exit_confirmed
    state = stopped.json()
    assert client.put(path, json={'expectedRevision': state['revision'], 'packageId': 'fixture-pet', 'enabled': True}).json()['host']['status'] == 'ready'
    third_host = original.host
    # Explicit native close and restart exercise unexpected native disconnection without replay.
    third_host.process.terminate()
    third_host.process.wait(timeout=5)
    state = client.get(path).json()
    assert state['host']['status'] == 'disconnected'
    assert client.post(path + '/restart').json()['host']['status'] == 'ready'
    assert third_host.closed and third_host.job.exit_confirmed
    final_host = original.host
    state = client.get(path).json()
    client.put(path, json={'expectedRevision': state['revision'], 'packageId': 'fixture-pet', 'enabled': False})
    assert final_host.closed and final_host.job.exit_confirmed
    (tmp_path / 'native-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32' or os.environ.get('MEIDO_PET_NATIVE_TEST') != '1',
    reason='explicit Windows own-window acceptance required')
def test_two_real_backends_restore_enabled_native_binding_and_finish_original_trees(api, tmp_path, tmp_path_factory):
    from backend.app.task_runtime import TaskOrigin, TaskBudget
    from backend.app.task_runtime.process import ProcessExecutor
    _, _, _, tasks = api
    persistent = tmp_path_factory.mktemp('pet-rebuild')
    fixture = Path(__file__).resolve().parents[1] / 'fixtures/desktop_pet_rebuild.py'
    dependencies = json.dumps(list(dict.fromkeys([sysconfig.get_path('purelib'), sysconfig.get_path('platlib')])))
    reports = []
    for phase in (1, 2):
        origin = TaskOrigin('pet195-rebuild', 'role:pet195-rebuild', 'phase-' + str(phase))
        executor = ProcessExecutor([sys.executable, '-I', '-S', str(fixture), str(persistent), str(phase)], tmp_path,
            dict(os.environ, MEIDO_TEST_SITE_PATHS=dependencies))
        identifier = tasks.submit(origin, executor, budget=TaskBudget(60, 60000), kind='shell', description='Owned native pet application rebuild')
        while True:
            result = tasks.query(identifier, origin, wait_seconds=.2)
            if result.ended_at is not None:
                break
        assert result.status == 'succeeded' and result.exit_code == 0 and not result.log_truncated, result.output
        reports.append(json.loads((persistent / ('phase-' + str(phase) + '-result.json')).read_text(encoding='utf-8')))
    assert reports[0]['pid'] != reports[1]['pid'] and reports[0]['nativePid'] != reports[1]['nativePid']
    assert reports[0]['roleId'] == reports[1]['roleId']
    assert reports[0]['actionId'] != reports[1]['actionId']
    assert all(item['nativeTreeExited'] and item['memory'] and item['channels'] and item['newThreads'] == [] for item in reports)
    assert reports[0]['receipt']['window']['bounds'] == reports[1]['window']['bounds']
