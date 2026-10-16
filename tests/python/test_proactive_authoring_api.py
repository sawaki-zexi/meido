"""Authoring runs through real chat/FileTools and explicit owner HTTP actions."""
import asyncio
import json
import hashlib
from pathlib import Path
import re
import sys
import time
import os
import shutil
from types import SimpleNamespace
import uuid

import pytest

from backend.app import main
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.mcp_config import McpConfigStore
from backend.app.mcp_runtime import McpHost
from backend.app.mcp_stdio import McpStdioClient
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.proactive_runtime.source_validation import retry_allowed
from backend.app.proactive_runtime.config import SourceGrant
from test_background_shell_api import MemoryCredentials
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401


pytestmark = pytest.mark.usefixtures('local_platform_only')


def template_files():
    skill = Path(__file__).resolve().parents[2] / 'backend/app/builtin_skills/create-proactive-source/SKILL.md'
    return {name: content + '\n' for name, content in re.findall(
        r'### `([^`]+)`\s+```(?:json|python|text)\n(.*?)\n```', skill.read_text(encoding='utf-8'), re.S)}


@pytest.fixture
def authored(api, tmp_path, monkeypatch):
    client, roles, sessions, tasks = api
    host = McpHost(McpConfigStore(tmp_path / 'author-mcp.db', MemoryCredentials()))
    monkeypatch.setattr(main, 'mcp_host', host)
    monkeypatch.setattr(main, '_proactive', None)
    now = [time.time()]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    role = client.post('/api/roles', json={'name': '真实作者产物', 'profile': {'profile': '只消费本地夹具'},
        'agentConfig': {'enabledTools': ['write_file', 'read_file', 'list_dir']}}).json()['role']
    role_id = role['id']

    class Adapter:
        files = None
        step = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            system = '\n'.join(str(item['content']) for item in messages if item['role'] == 'system')
            if self.files is None:
                self.files = [(name, text + '\n') for name, text in re.findall(
                    r'### `([^`]+)`\s+```(?:json|python|text)\n(.*?)\n```', system, re.S)]
                assert len(self.files) == 6
            if self.step < len(self.files):
                name, content = self.files[self.step]
                self.step += 1
                yield ModelToolCall(f'author-{self.step}', 'write_file', {'path': 'mcp/example/' + name, 'content': content})
            else:
                yield ModelTextDelta('全部六份产物已写入，尚未获得 MCP/自主权限')

    monkeypatch.setattr(main, 'model_adapter', Adapter())
    created = client.post(f'/api/roles/{role_id}/messages', json={'content': '$create-proactive-source 创建完整离线示范'})
    assert 'assistant_completed' in created.text, created.text
    root = roles.workspace_path(role_id) / 'mcp/example'
    path = f'/api/roles/{role_id}/autonomy'
    first = client.post(path + '/source-artifact-preview', json={'manifestPath': 'mcp/example/manifest.json'}).json()
    # The owner correction is written through the same actual file tool seam.
    class Correct:
        called = False

        async def stream_structured_messages(self, messages, configuration, tools):
            if not self.called:
                self.called = True
                yield ModelToolCall('owner-correct-manifest', 'write_file', {'path': 'mcp/example/manifest.json', 'content': first['preparedManifest']})
            else:
                yield ModelTextDelta('完整清单已保存')

    monkeypatch.setattr(main, 'model_adapter', Correct())
    assert 'assistant_completed' in client.post(f'/api/roles/{role_id}/messages', json={'content': '保存已核对的清单'}).text
    trusted = client.post('/api/mcp/servers', json={'serverId': 'authored_source', 'name': '离线作者产物',
        'command': [str(Path(sys.executable).resolve()), '-I', str(root / 'source.py')], 'cwd': str(root),
        'roleIds': [role_id], 'env': {}, 'enabled': True})
    assert trusted.status_code == 201, trusted.text
    preview = client.post(path + '/source-artifact-preview', json={'manifestPath': 'mcp/example/manifest.json',
        'serverId': 'authored_source'})
    assert preview.status_code == 200, preview.text
    assert preview.json()['manifestReady'] and len(preview.json()['sourceGrants']) == 3
    value = SimpleNamespace(client=client, roles=roles, sessions=sessions, tasks=tasks, host=host,
        role_id=role_id, root=root, path=path, preview=preview.json(), now=now)
    try:
        yield value
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())
        asyncio.run(host.close())


def verify_source(value, channel):
    grant = next(item for item in value.preview['sourceGrants'] if item['channel'] == channel)
    saved = value.client.put(value.path, json={'expectedRevision': 0, 'config': {'sourceGrants': [{**grant, 'enabled': True}]}})
    assert saved.status_code == 200, saved.text
    return submit_verification(value, grant['sourceId'])


def submit_verification(value, source_id):
    revision = value.client.get(value.path).json()['config']['revision']
    response = value.client.post(value.path + '/source-verifications', json={'requestNonce': str(uuid.uuid4()),
        'sourceId': source_id, 'expectedRevision': revision, 'manifestPath': value.preview['manifestPath'],
        'manifestSha256': value.preview['manifestSha256'], 'trustLocalCode': True})
    assert response.status_code == 202, response.text
    identifier = response.json()['id']

    async def settle():
        async with asyncio.timeout(20):
            while True:
                response = value.client.get(value.path + '/source-verifications')
                if response.status_code == 200:
                    final = next(item for item in response.json()['attempts'] if item['id'] == identifier)
                    if final['runtimeAcknowledged']:
                        return final
                else:
                    assert response.status_code == 503, response.text
                await asyncio.sleep(.02)
    return asyncio.run(settle())


@pytest.mark.parametrize('channel', ['alert', 'content', 'context'])
def test_generated_artifact_really_verifies_each_legal_channel_without_unrelated_tools(authored, channel):
    final = verify_source(authored, channel)
    assert final['status'] == 'succeeded', final
    assert final['proof']['verifierVersion'] == 'meido-source-author-v2'
    assert final['proof']['collectionVerified'] is True
    assert final['proof']['ackVerified'] is (channel != 'context')
    assert not (authored.root / 'state').exists()
    assert authored.host.public()[0]['activeProcesses'] == 0


def test_same_chat_generated_source_runs_next_real_clock_then_persists_ack_after_app_and_stdio_restart(authored, monkeypatch):
    import sqlite3

    value = authored
    grants = [{**item, 'enabled': True} for item in value.preview['sourceGrants']]
    saved = value.client.put(value.path, json={'expectedRevision': 0, 'config': {'sourceGrants': grants}})
    assert saved.status_code == 200, saved.text
    proofs = {}
    for grant in grants:
        final = submit_verification(value, grant['sourceId'])
        assert final['status'] == 'succeeded', final
        proofs[grant['sourceId']] = final['proof']
    assert not (value.root / 'state').exists()
    for grant in grants:
        proof = proofs[grant['sourceId']]
        if proof['ackVerified']:
            grant['ackProofId'] = proof['proofId']
        else:
            with sqlite3.connect(value.sessions.database_path) as db:
                assert not retry_allowed(db, value.role_id, SourceGrant.model_validate({**grant, 'ackProofId': proof['proofId']}))
    model = value.client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'scripted-only', 'apiKey': 'fixture-not-authentication'}).json()['configuration']

    class Decision:
        requests = 0
        content_id = None

        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            step = self.requests
            if step == 1:
                name, arguments = 'get_context_data', {}
            elif step == 2:
                assert 'synthetic' in messages[-1].content[0].text
                name, arguments = 'get_content_events', {}
            elif step == 3:
                self.content_id = json.loads(messages[-1].content[0].text)[0]['id']
                name, arguments = 'get_content', {'item_ids': [self.content_id]}
            elif step == 4:
                assert 'content-demo-1 的固定关联正文' in messages[-1].content[0].text
                name, arguments = 'get_alert_events', {}
            elif step == 5:
                alert_id = json.loads(messages[-1].content[0].text)[0]['id']
                name, arguments = 'message_push', {'message': '作者产物已被后续真实 tick 消费', 'mood': 'calm',
                    'thought': '仅离线合成数据', 'evidence': [self.content_id, alert_id]}
            else:
                name, arguments = 'finish_turn', {'decision': 'reply'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'author-tick-{step}', name, arguments),)))

    adapter = Decision()
    monkeypatch.setattr(main, 'model_adapter', adapter)
    enabled = value.client.put(value.path, json={'expectedRevision': 1, 'config': {'sourceGrants': grants,
        'activeDecisionEnabled': True, 'decisionModelConfigurationId': model['id'], 'consentRevision': 1}})
    assert enabled.status_code == 200, enabled.text
    assert not (value.root / 'state').exists() and adapter.requests == 0

    async def settle():
        async with asyncio.timeout(20):
            while True:
                await main._proactive.recover()
                state = main._proactive.query(value.role_id)
                if state['runs'] and all(item['runtimeAcknowledged'] for item in state['runs']):
                    return state
                await asyncio.sleep(.02)

    async def exercise():
        app = main._proactive_application()
        await app.on_clock()  # Initial poll is a real separate Runtime task.
        await settle()
        value.now[0] += 1800
        await app.on_clock()
        await settle()
        # A due decision waits for the new poll's original result ACK.
        await app.on_clock()
        async with asyncio.timeout(20):
            while True:
                state = await settle()
                if len(state['acknowledgements']) == 2 and all(item['status'] == 'acknowledged' for item in state['acknowledgements']):
                    break
                await asyncio.sleep(.02)
        assert adapter.requests == 6
        decisions = [item for item in state['runs'] if item['kind'] == 'autonomy_tick']
        assert len(decisions) == 1 and decisions[0]['status'] == 'sent', state
        messages = [item for item in value.sessions.list_messages(f'role:{value.role_id}') if item.messageType == 'internal_completion']
        assert len(messages) == 1 and messages[0].content == '作者产物已被后续真实 tick 消费'
        assert messages[0].metadata['deliveryStatus'] == 'sent'
        before = {item['id']: (item['expiresAt'], item['decisionAt']) for item in state['acknowledgements']}
        assert sorted(item['permanent'] for item in state['acknowledgements']) == [False, True]
        finite = next(item for item in state['acknowledgements'] if not item['permanent'])
        assert finite['expiresAt'] == finite['decisionAt'] + 168 * 3600
        await app.close()
        await value.host.close()
        monkeypatch.setattr(main, '_proactive', None)
        fresh_host = McpHost(value.host.store)
        monkeypatch.setattr(main, 'mcp_host', fresh_host)
        try:
            rebuilt = main._proactive_application()
            assert {item['id']: (item['expiresAt'], item['decisionAt']) for item in rebuilt.query(value.role_id)['acknowledgements']} == before
            server = fresh_host.store.get('authored_source')
            peer = McpStdioClient(server, fresh_host.store.environment(server))
            try:
                catalog = await peer.connect()
                assert not any(item['name'].startswith('meido_test_') for item in catalog)
                assert json.loads((await peer.call('get_contents', {}))['content'][0]['text']) == []
                assert json.loads((await peer.call('get_alerts', {}))['content'][0]['text']) == []
                assert 'synthetic' in (await peer.call('get_context', {}))['content'][0]['text']
            finally:
                await peer.close()
            assert peer.exit_confirmed
            assert adapter.requests == 6
        finally:
            await rebuilt.close()
            await fresh_host.close()
    asyncio.run(exercise())


def refresh_artifact(value, monkeypatch):
    inspected = value.client.post(value.path + '/source-artifact-preview', json={'manifestPath': 'mcp/example/manifest.json'})
    assert inspected.status_code == 200, inspected.text
    corrected = inspected.json()['preparedManifest']

    class Correct:
        called = False

        async def stream_structured_messages(self, messages, configuration, tools):
            if not self.called:
                self.called = True
                yield ModelToolCall('resave-owner-manifest', 'write_file', {'path': 'mcp/example/manifest.json', 'content': corrected})
            else:
                yield ModelTextDelta('修正清单已保存')
    monkeypatch.setattr(main, 'model_adapter', Correct())
    assert 'assistant_completed' in value.client.post(f'/api/roles/{value.role_id}/messages', json={'content': '保存本次核对的清单'}).text
    value.preview = value.client.post(value.path + '/source-artifact-preview', json={
        'manifestPath': 'mcp/example/manifest.json', 'serverId': 'authored_source'}).json()
    assert value.preview['manifestReady']


@pytest.mark.parametrize('context_format', ['nested', 'flat'])
def test_context_only_server_does_not_need_alert_content_poll_or_ack_operations(authored, monkeypatch, context_format):
    value = authored
    catalog = json.loads((value.root / 'catalog.json').read_text(encoding='utf-8'))
    (value.root / 'catalog.json').write_text(json.dumps([item for item in catalog if item['name'] == 'get_context']))
    draft = json.loads((value.root / 'source-draft.json').read_text(encoding='utf-8'))
    draft['sources'] = [item for item in draft['sources'] if item['channel'] == 'context']
    (value.root / 'source-draft.json').write_text(json.dumps(draft))
    script = value.root / 'source.py'
    replacement = ('{"data": event.get("data", {})}' if context_format == 'nested' else 'event.get("data", {})')
    script.write_text(script.read_text(encoding='utf-8').replace('return [event for event in events if',
        'return [' + replacement + ' if kind == "context" else event for event in events if'), encoding='utf-8')
    refresh_artifact(value, monkeypatch)
    final = verify_source(value, 'context')
    assert final['status'] == 'succeeded', final
    assert final['proof']['collectionVerified'] and not final['proof']['ackVerified']


@pytest.mark.parametrize('fault', ['hides_read_failure', 'wrong_body', 'unstable_identity', 'loses_pending_on_restart'])
def test_real_authored_source_fails_verification_for_broken_collection_contract(authored, monkeypatch, fault):
    value = authored
    script = value.root / 'source.py'
    text = script.read_text(encoding='utf-8')
    if fault == 'hides_read_failure':
        text = text.replace('raise ValueError("synthetic read failure")', 'return []')
    elif fault == 'wrong_body':
        text = text.replace('event["id"] not in suppressed]', 'event["id"] not in suppressed and not event.pop("body", None)]')
    elif fault == 'unstable_identity':
        text = text.replace('kind = {"get_alerts"', 'for item in events:\n                item["id"] += str(time.time_ns())\n            kind = {"get_alerts"')
    else:
        text = text.replace('db.commit()', 'db.execute("DELETE FROM meta")\n    db.commit()')
    assert text != script.read_text(encoding='utf-8')
    script.write_text(text, encoding='utf-8', newline='\n')
    refresh_artifact(value, monkeypatch)
    final = verify_source(value, 'content')
    assert final['status'] == 'failed' and final['proof'] is None, final
    assert final['runtimeAcknowledged'] and not final['cleanupPending']
    assert not (value.root / 'state').exists()


def test_explicit_author_skill_writes_a_real_role_file_without_shell_or_registration(api, monkeypatch):
    client, roles, sessions, _ = api
    monkeypatch.setattr(main, '_proactive', None)

    class Adapter:
        calls = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            system = '\n'.join(str(item['content']) for item in messages if item['role'] == 'system')
            assert 'meido-source-author-v2' in system
            assert '--meido-contract-test' in system
            assert 'trustLocalCode' in system
            assert 'shell' not in {item['function']['name'] for item in tools}
            if self.calls == 1:
                yield ModelToolCall('author-source', 'write_file', {'path': 'mcp/example/README.md',
                    'content': 'Synthetic source only; owner MCP trust and source permission are still required.'})
            else:
                yield ModelTextDelta('已写草稿，等待主人检查与许可')

    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '来源作者', 'profile': {'profile': '本地合成来源'},
        'agentConfig': {'enabledTools': ['write_file', 'read_file', 'list_dir']}}).json()['role']
    try:
        result = client.post(f"/api/roles/{role['id']}/messages", json={'content': '$create-proactive-source 创建合成来源'})
        assert 'assistant_completed' in result.text, result.text
        assert (roles.workspace_path(role['id']) / 'mcp/example/README.md').is_file()
        run = sessions.get_run(sessions.list_messages(f"role:{role['id']}")[-1].runId)
        selected = run.modelSnapshot['capabilities']['skills']
        assert [(item['id'], item['source']) for item in selected] == [('create-proactive-source', 'builtin')]
        assert selected[0]['supportingFiles'][0]['path'] == 'create-proactive-source/references/contract.md'
        assert 'shell' not in selected[0]['tools']
        assert client.get(f"/api/roles/{role['id']}/autonomy").json()['config']['sourceGrants'] == []
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())


def test_owner_preview_computes_real_hashes_but_only_file_tools_save_the_correction(api, monkeypatch):
    client, roles, sessions, _ = api
    monkeypatch.setattr(main, '_proactive', None)
    role = client.post('/api/roles', json={'name': '产物预览', 'profile': {'profile': '本地'},
        'agentConfig': {'enabledTools': ['write_file', 'read_file']}}).json()['role']
    root = roles.workspace_path(role['id']) / 'mcp/example'
    root.mkdir(parents=True)
    for name, content in template_files().items():
        (root / name).write_text(content, encoding='utf-8', newline='\n')
    before = {item.name: item.read_bytes() for item in root.iterdir()}
    path = f"/api/roles/{role['id']}/autonomy/source-artifact-preview"
    try:
        response = client.post(path, json={'manifestPath': 'mcp/example/manifest.json'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['manifestReady'] is False
        assert result['manifestSha256'] == hashlib.sha256(before['manifest.json']).hexdigest()
        fixed = json.loads(result['preparedManifest'])
        assert fixed['files']['source.py'] == hashlib.sha256(before['source.py']).hexdigest()
        assert before == {item.name: item.read_bytes() for item in root.iterdir()}

        class Adapter:
            calls = 0

            async def stream_structured_messages(self, messages, configuration, tools):
                self.calls += 1
                if self.calls == 1:
                    yield ModelToolCall('save-owner-manifest', 'write_file', {'path': 'mcp/example/manifest.json',
                        'content': result['preparedManifest']})
                else:
                    yield ModelTextDelta('已保存主人核对的清单')

        monkeypatch.setattr(main, 'model_adapter', Adapter())
        corrected = client.post(f"/api/roles/{role['id']}/messages", json={'content': '原样保存已核对的完整清单'})
        assert 'assistant_completed' in corrected.text
        ready = client.post(path, json={'manifestPath': 'mcp/example/manifest.json'}).json()
        assert ready['manifestReady'] is True
        assert ready['manifestSha256'] == hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest()
        assert not (root / 'state').exists()
        assert client.get(f"/api/roles/{role['id']}/autonomy").json()['config']['sourceGrants'] == []
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())


@pytest.mark.parametrize('fault', ['absolute', 'parent', 'foreign_role', 'hardlink', 'missing', 'oversized', 'duplicate_key', 'extra_authority'])
def test_owner_preview_rejects_unsafe_or_incomplete_artifacts_without_secret_or_side_effect(api, monkeypatch, fault):
    client, roles, _, _ = api
    monkeypatch.setattr(main, '_proactive', None)
    role = client.post('/api/roles', json={'name': '预览边界', 'profile': {'profile': '本地'}}).json()['role']
    workspace = roles.workspace_path(role['id'])
    root = workspace / 'mcp/example'
    root.mkdir(parents=True)
    for name, content in template_files().items():
        (root / name).write_text(content, encoding='utf-8', newline='\n')
    foreign = workspace.parent / 'owner-secret.txt'
    foreign.write_text('must-never-leak-author-secret')
    body = {'manifestPath': 'mcp/example/manifest.json'}
    if fault == 'absolute':
        body['manifestPath'] = str(root / 'manifest.json')
    elif fault == 'parent':
        body['manifestPath'] = '../owner-secret.txt'
    elif fault == 'foreign_role':
        body['manifestPath'] = '../../other-role/workspace/manifest.json'
    elif fault == 'hardlink':
        (root / 'source.py').unlink()
        os.link(foreign, root / 'source.py')
    elif fault == 'missing':
        (root / 'source.py').unlink()
    elif fault == 'oversized':
        (root / 'source.py').write_bytes(b'x' * (8 * 1024 * 1024 + 1))
    elif fault == 'duplicate_key':
        (root / 'manifest.json').write_text('{"files":{},"files":{}}')
    else:
        body['trustLocalCode'] = True
    before = {str(item.relative_to(workspace)): item.read_bytes() for item in root.iterdir()}
    try:
        response = client.post(f"/api/roles/{role['id']}/autonomy/source-artifact-preview", json=body)
        assert response.status_code in {403, 404, 422}, response.text
        assert 'must-never-leak-author-secret' not in response.text
        assert before == {str(item.relative_to(workspace)): item.read_bytes() for item in root.iterdir()}
        assert not (root / 'state').exists()
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())


def test_preview_does_not_create_a_missing_legacy_workspace(api, monkeypatch):
    client, roles, *_ = api
    monkeypatch.setattr(main, '_proactive', None)
    role = client.post('/api/roles', json={'name': '旧角色缺目录', 'profile': {'profile': '本地'}}).json()['role']
    workspace = roles.workspace_path(role['id'])
    backup = workspace.with_name('workspace-owned-fixture-backup')
    assert workspace.resolve().is_relative_to(roles.root.resolve()) and backup.resolve().is_relative_to(roles.root.resolve())
    workspace.rename(backup)
    try:
        response = client.post(f"/api/roles/{role['id']}/autonomy/source-artifact-preview", json={'manifestPath': 'mcp/manifest.json'})
        assert response.status_code == 422, response.text
        assert not workspace.exists()
    finally:
        backup.rename(workspace)
        if main._proactive is not None:
            asyncio.run(main._proactive.close())


def test_preview_rejects_a_replaced_artifact_directory_even_with_identical_file_bytes(api, monkeypatch):
    from backend.app.proactive_runtime import source_authoring

    client, roles, *_ = api
    monkeypatch.setattr(main, '_proactive', None)
    role = client.post('/api/roles', json={'name': '目录身份检查', 'profile': {'profile': '本地'}}).json()['role']
    workspace = roles.workspace_path(role['id'])
    root = workspace / 'mcp/example'
    root.mkdir(parents=True)
    for name, content in template_files().items():
        (root / name).write_text(content, encoding='utf-8', newline='\n')
    original = source_authoring._read
    replaced = []

    def replace_after_first_read(path, limit):
        value = original(path, limit)
        if not replaced:
            retired = root.with_name('example-original-owned-fixture')
            assert root.resolve().is_relative_to(workspace) and retired.resolve().is_relative_to(workspace)
            root.rename(retired)
            shutil.copytree(retired, root)
            replaced.append(retired)
        return value

    monkeypatch.setattr(source_authoring, '_read', replace_after_first_read)
    try:
        result = client.post(f"/api/roles/{role['id']}/autonomy/source-artifact-preview", json={'manifestPath': 'mcp/example/manifest.json'})
        assert result.status_code == 422, result.text
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())
