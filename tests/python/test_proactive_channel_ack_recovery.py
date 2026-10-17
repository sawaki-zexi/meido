"""Original channel receipts recover ACKs after real snapshot expiry and restart."""
import asyncio
import hashlib
import json
import sqlite3
import sys

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.channels.host import ChannelHost
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.mcp_autonomy import declaration_hash
from backend.app.mcp_runtime import McpHost
from backend.app.multichannel import MultichannelStore
from backend.app.proactive_runtime import retention
from backend.app.proactive_runtime.source_validation import SourceContractRegistry
from backend.app.session_store import SessionStore
from backend.app.task_runtime import TaskRuntime
from test_background_shell_api import MemoryCredentials
from test_proactive_ack import write_manifest
from test_proactive_mcp import FIXTURE
from test_proactive_snapshot import grant_for
from test_proactive_targets_api import api, shell_api, platform, source, role_target, save_external  # noqa: F401


pytestmark = pytest.mark.usefixtures('local_platform_only')


def _calls(root):
    path = root / 'calls.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def _ack_source(source_fixture, role):
    root, store, server_input, *_ = source_fixture
    declaration = {'name': 'ack', 'description': 'persistent fixed-expiry acknowledgement',
        'inputSchema': {'type': 'object', 'properties': {
            'event_ids': {'type': 'array', 'items': {'type': 'string'}},
            'ack_id': {'type': 'string'}, 'expires_at': {'type': ['number', 'null']},
            'ttl_hours': {'type': 'integer', 'minimum': 0}},
            'required': ['event_ids', 'ttl_hours'], 'additionalProperties': False}}
    catalog = [*json.loads((root / 'catalog.json').read_text(encoding='utf-8')), declaration]
    (root / 'catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
    artifact = root / 'channel-ack-contract'
    artifact.mkdir()
    (artifact / 'source.py').write_bytes(FIXTURE.read_bytes())
    (artifact / 'catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
    manifest, manifest_hash = write_manifest(artifact)
    server = store.save(server_input.model_copy(update={
        'roleIds': [role], 'command': [sys.executable, '-I', str(artifact / 'source.py'), str(root)],
        'timeoutSeconds': 10}))
    grant = grant_for(source_fixture).model_copy(update={
        'configRevision': server.revision, 'grantRevision': server.grant_id,
        'ackTool': 'ack', 'ackDefinitionHash': declaration_hash(declaration), 'ackContract': 'absolute-idempotent'})
    return grant, manifest, manifest_hash


class _Reply:
    def __init__(self):
        self.requests = 0
        self.item_id = None

    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        self.requests += 1
        if self.requests == 1:
            name, arguments = 'get_content_events', {}
        elif self.requests == 2:
            self.item_id = json.loads(messages[-1].content[0].text)[0]['id']
            name, arguments = 'message_push', {'message': 'late-original-8301', 'mood': '平静',
                'thought': '原回执恢复', 'evidence': [self.item_id]}
        else:
            assert self.requests == 3, 'receipt recovery must not repeat the original model'
            name, arguments = 'finish_turn', {'decision': 'reply'}
        yield ModelStreamDone(ModelAssistantMessage(content=(
            ModelToolCallBlock(f'late-ack-{self.requests}', name, arguments),)))


async def _runtime_settled(application, role):
    async with asyncio.timeout(30):
        while True:
            try:
                await application.recover()
                state = application.query(role)
            except sqlite3.OperationalError as error:
                if (getattr(error, 'sqlite_errorcode', 0) & 0xFF) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
            else:
                if state['runs'] and all(run['runtimeAcknowledged'] for run in state['runs']):
                    return state
            await asyncio.sleep(.01)


def _runtime_released(application):
    with application._transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM autonomy_runs WHERE runtime_acknowledged=0 OR result_ack_pending<>0').fetchone()[0] == 0
    with sqlite3.connect(application.tasks.root / 'tasks.db') as db:
        assert db.execute('SELECT COUNT(*) FROM tasks WHERE retain_until_ack<>0 OR ended_at IS NULL').fetchone()[0] == 0


def test_real_late_original_receipt_restores_absolute_ack_after_prune_and_full_owner_rebuild(
        api, monkeypatch, platform, source, tmp_path):
    now, model = [1_781_000_000.0], _Reply()
    root, mcp_store, *_ = source
    original_host = McpHost(mcp_store)
    monkeypatch.setattr(main, 'mcp_host', original_host)
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    monkeypatch.setattr(main, 'model_adapter', model)
    (root / 'events.json').write_text(json.dumps([{'id': 'original-remote-content', 'kind': 'content',
        'title': 'PRIVATE LATE TITLE', 'summary': 'PRIVATE LATE SUMMARY',
        'url': 'https://private.invalid/late', 'body': 'PRIVATE LATE BODY'}]), encoding='utf-8')
    evidence, owners = {'stage': 'configured'}, []
    evidence_path = tmp_path / 'late-channel-ack-evidence.json'

    def record(stage, **values):
        evidence.update(stage=stage, **values)
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')

    with role_target(api, monkeypatch, platform) as (client, role, account, _):
        path = f'/api/roles/{role}/autonomy'
        grant, manifest, manifest_hash = _ack_source(source, role)

        async def close_owner(owner):
            application, host, tasks, channel_host = owner
            try:
                await application.close()
            finally:
                try:
                    await host.close()
                finally:
                    try:
                        await channel_host.pause_and_drain(account.account_id)
                        await channel_host.stop(account.account_id)
                        channel_host.unregister(account.account_id)
                    finally:
                        await asyncio.to_thread(tasks.close)
            assert not application.has_owned_work
            assert not host.autonomy_cleanup_pending(role)

        async def rebuild():
            previous = owners[-1]
            await close_owner(previous)
            sessions = SessionStore(previous[0].sessions.database_path)
            tasks, host = TaskRuntime(previous[2].root), McpHost(mcp_store)
            channels = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
            channel_host = ChannelHost(channels)
            channel_host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=platform[1]))
            monkeypatch.setattr(main, 'session_store', sessions)
            monkeypatch.setattr(main, 'task_runtime', tasks)
            monkeypatch.setattr(main, 'multichannel_store', channels)
            monkeypatch.setattr(main, 'channel_host', channel_host)
            monkeypatch.setattr(main, 'mcp_host', host)
            monkeypatch.setattr(main, '_proactive', None)
            try:
                application = main._proactive_application()
            except BaseException:
                await host.close()
                await channel_host.stop(account.account_id)
                await asyncio.to_thread(tasks.close)
                raise
            owners.append((application, host, tasks, channel_host))
            return application

        async def scenario():
            application = main._proactive_application()
            owners.append((application, original_host, api[3], main.channel_host))
            model_id = client.post('/api/model/configurations', json={
                'providerId': 'custom', 'provider': 'local', 'baseUrl': 'http://127.0.0.1:1/v1',
                'model': 'local-only', 'apiKey': 'fixture'}).json()['configuration']['id']
            initial, selected = save_external(client, path, account, {'activeDecisionEnabled': True,
                'decisionModelConfigurationId': model_id, 'consentRevision': 1,
                'sourceGrants': [grant.model_dump(mode='json')]})
            assert initial['config']['sourceGrants'] == [grant.model_dump(mode='json')]
            registry = SourceContractRegistry(application.sessions.database_path, mcp_store)
            try:
                proof = await registry.validate_local(role, grant, manifest_path=manifest,
                    expected_manifest_sha256=manifest_hash,
                    authorized=lambda: main.store.get(role) is not None and mcp_store.get(grant.serverId).enabled)
            finally:
                await registry.close()
            assert proof['ackVerified'] is True and proof['verifierVersion'] == 'meido-source-contract-v1'
            proven = grant.model_copy(update={'ackProofId': proof['proofId']})
            updated = client.put(path, json={'expectedRevision': initial['config']['revision'],
                'config': {**initial['config'], 'sourceGrants': [proven.model_dump(mode='json')]}})
            assert updated.status_code == 200, updated.text
            state = updated.json()
            original_grant = state['config']['sourceGrants'][0]
            assert original_grant == proven.model_dump(mode='json')
            baseline = _calls(root)
            assert baseline == []  # Contract verification used its own isolated state, not this source.
            with application._transaction() as db:
                db.execute("""CREATE TRIGGER fixture_block_autonomy_projection BEFORE INSERT ON messages
                    WHEN json_extract(NEW.metadata_json,'$.internalEvent')='active_decision'
                    BEGIN SELECT RAISE(ABORT,'fixture: durable channel receipt before business projection'); END""")
            now[0] += 1800
            assert await application.on_clock() == 1
            unknown = await _runtime_settled(application, role)
            assert len(platform[0]['sent']) == 1 and platform[0]['sent'][0]['user_id'] == 101
            assert model.requests == 3
            assert [call['name'] for call in _calls(root)] == ['get_events']
            assert unknown['acknowledgements'] == []
            delivery = unknown['deliveries'][0]
            assert delivery['status'] in {'dispatching', 'pending', 'uncertain'}
            intent, tick = delivery['intentId'], unknown['runs'][0]['id']
            with application._transaction() as db:
                recovery = dict(db.execute('SELECT * FROM autonomy_delivery_recovery WHERE intent_id=?', (intent,)).fetchone())
                encoded = recovery['disposition_inputs']
                assert recovery['sent_disposition_decision_at'] is None and recovery['projection_pending'] == 1
                assert not any(value in encoded for value in ('PRIVATE LATE', 'private.invalid', 'late-original-8301'))
                inputs = json.loads(encoded)
                assert inputs['sourceGrants'] == [original_grant]
                assert inputs['evidence'] == [model.item_id]
                assert inputs['items'][model.item_id] == {'sourceId': proven.sourceId, 'sourceRevision': proven.revision,
                    'eventDomain': proven.eventDomain, 'remoteId': 'original-remote-content', 'kind': 'content'}
                assert db.execute('SELECT expires_at,status FROM autonomy_event_claims WHERE tick_id=?', (tick,)).fetchone()[:] == (None, 'dispatching')
                charged = retention.usage(db, role)
                db.execute('SAVEPOINT fixture_measure_disposition_bytes')
                try:
                    db.execute('UPDATE autonomy_delivery_recovery SET disposition_inputs=? WHERE intent_id=?', ('', intent))
                    without_inputs = retention.usage(db, role)
                    assert charged['globalBytes'] - without_inputs['globalBytes'] == len(encoded.encode('utf-8'))
                    assert charged['roleBytes'] - without_inputs['roleBytes'] == len(encoded.encode('utf-8'))
                finally:
                    db.execute('ROLLBACK TO fixture_measure_disposition_bytes')
                    db.execute('RELEASE fixture_measure_disposition_bytes')
                attempt = dict(db.execute('SELECT * FROM channel_delivery_attempts WHERE source_kind=? AND source_tool_call_id=?',
                                         ('autonomy', intent)).fetchone())
                assert attempt['status'] == 'sent' and attempt['platform_message_id'] == '8301'
                assert attempt['source_run_id'] == tick and attempt['account_id'] == account.account_id
            _runtime_released(application)
            record('unknown-original-receipt', role=role, tick=tick, intent=intent,
                   attempt=attempt['attempt_id'], inputsSha256=hashlib.sha256(encoded.encode('utf-8')).hexdigest())

            now[0] += 86401
            with application._transaction() as db:
                result = retention.prune(db, application._now(db))
                snapshot = json.loads(db.execute('SELECT snapshot FROM autonomy_runs WHERE tick_id=?', (tick,)).fetchone()[0])
                assert result['expiredSnapshots'] == 1 and result['protectedRuns'] >= 1
                assert set(snapshot) == {'sources', 'expired'} and snapshot['expired'] is True
                assert 'PRIVATE LATE' not in json.dumps(snapshot)
                retained = dict(db.execute('SELECT * FROM autonomy_delivery_recovery WHERE intent_id=?', (intent,)).fetchone())
                assert retained == recovery
                assert db.execute('SELECT remaining_bytes FROM autonomy_storage_reservations WHERE operation_id=?', (tick,)).fetchone()[0] > 0
            record('pruned-full-snapshot', snapshot=snapshot, prune=result)
            application = await rebuild()
            with application._transaction() as db:
                assert db.execute('SELECT disposition_inputs FROM autonomy_delivery_recovery WHERE intent_id=?', (intent,)).fetchone()[0] == encoded
                db.execute('DROP TRIGGER fixture_block_autonomy_projection')
            first_decision = now[0]
            async with asyncio.timeout(30):
                while True:
                    await application.recover()
                    recovered = application.query(role)
                    acks = recovered['acknowledgements']
                    if acks and acks[0]['status'] == 'acknowledged' and all(run['runtimeAcknowledged'] for run in recovered['runs']):
                        break
                    await asyncio.sleep(.01)
            ack, final_delivery = acks[0], recovered['deliveries'][0]
            assert ack['decisionAt'] == first_decision and ack['expiresAt'] == first_decision + 604800
            assert ack['reason'] == 'referenced' and ack['attempts'] == 1 and ack['sourceId'] == proven.sourceId
            assert final_delivery['status'] == 'sent' and final_delivery['projectionPending'] is False
            assert final_delivery['attemptId'] == attempt['attempt_id'] and final_delivery['platformMessageId'] == '8301'
            with application._transaction() as db:
                row = dict(db.execute('SELECT * FROM autonomy_ack_outbox WHERE ack_id=?', (ack['id'],)).fetchone())
                original_ack = row.copy()
                assert row['source_revision'] == proven.revision and row['event_id'] == 'original-remote-content'
                assert json.loads(row['source_grant']) == original_grant
                event_key = hashlib.sha256(json.dumps([proven.serverId, proven.configRevision,
                    proven.eventDomain, proven.channel, 'original-remote-content'], separators=(',', ':')).encode()).hexdigest()
                assert ack['id'] == hashlib.sha256(json.dumps([role, tick, event_key, 'referenced']).encode()).hexdigest()
                assert db.execute('SELECT sent_disposition_decision_at FROM autonomy_delivery_recovery WHERE intent_id=?', (intent,)).fetchone()[0] == first_decision
                assert db.execute('SELECT remaining_bytes FROM autonomy_storage_reservations WHERE operation_id=?', (tick,)).fetchone()[0] == 0
            calls = _calls(root)
            assert [call['name'] for call in calls] == ['get_events', 'ack']
            assert calls[1]['arguments']['event_ids'] == ['original-remote-content']
            assert calls[1]['arguments']['ack_id'] == ack['id'] and calls[1]['arguments']['expires_at'] == ack['expiresAt']
            assert json.loads((root / 'acks.json').read_text(encoding='utf-8'))[ack['id']] == calls[1]['arguments']
            assert model.requests == 3 and len(platform[0]['sent']) == 1
            _runtime_released(application)
            record('acknowledged-original', decisionAt=first_decision, ack=ack, delivery=final_delivery, calls=calls)

            # A second complete owner rebuild and later recover cannot reopen a decided ACK or original send.
            application = await rebuild()
            now[0] += 900
            for _ in range(3):
                await application.recover()
            final = application.query(role)
            assert final['config']['sourceGrants'] == [original_grant]
            assert final['acknowledgements'] == [ack]
            with application._transaction() as db:
                assert dict(db.execute('SELECT * FROM autonomy_ack_outbox WHERE ack_id=?', (ack['id'],)).fetchone()) == original_ack
            threads = main.multichannel_store.list_threads(role)
            assert len(threads) == 1
            _, messages, _, _ = main.multichannel_store.list_messages(role, threads[0].thread_id)
            assert len(messages) == 1 and messages[0].content == 'late-original-8301'
            assert messages[0].metadata['autonomyIntentId'] == intent
            assert messages[0].metadata['deliveryAttemptId'] == attempt['attempt_id']
            assert main.session_store.list_messages(f'role:{role}') == []
            assert _calls(root) == calls and model.requests == 3 and len(platform[0]['sent']) == 1
            _runtime_released(application)
            record('second-rebuild-no-replay', ownerGenerations=len(owners))

        async def owned_scenario():
            try:
                await scenario()
            finally:
                errors = []
                for owner in reversed(owners):
                    try:
                        await close_owner(owner)
                    except Exception as error:
                        errors.append(error)
                record(evidence['stage'], allOwnersClosed=not errors)
                if errors:
                    raise ExceptionGroup('late ACK fixture cleanup', errors)

        try:
            asyncio.run(owned_scenario())
        finally:
            asyncio.run(original_host.close())
