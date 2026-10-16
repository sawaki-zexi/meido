"""Original source permission and first decision deadlines survive channel recovery."""
import asyncio
from contextlib import contextmanager
import json
import sqlite3
import uuid

import pytest

from backend.app import main
from backend.app.channels.host import ChannelHost
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.mcp_runtime import McpHost
from backend.app.multichannel import MultichannelStore
from backend.app.proactive_runtime import retention
from backend.app.proactive_runtime.config import SourceGrant
from backend.app.session_store import SessionStore
from backend.app.task_runtime import TaskRuntime
from test_background_shell_api import MemoryCredentials
from test_proactive_snapshot import grant_for
from test_proactive_channel_ack_recovery import _Reply, _ack_source, _calls, _runtime_released, _runtime_settled
from test_proactive_targets_api import api, shell_api, platform, source, role_target, save_external  # noqa: F401


pytestmark = pytest.mark.usefixtures('local_platform_only')


class _OriginalChannel:
    def __init__(self, api_fixture, monkeypatch, platform_fixture, source_fixture, tmp_path,
                 client, role, account, host, *, original_ack):
        self.api, self.patch, self.platform, self.source = api_fixture, monkeypatch, platform_fixture, source_fixture
        self.client, self.role, self.account, self.original_host = client, role, account, host
        self.path, self.now, self.model = f'/api/roles/{role}/autonomy', [1_781_100_000.0], _Reply()
        self.root = source_fixture[0]
        self.original_ack = original_ack
        self.owners, self.closed = [], set()
        self.evidence_path = tmp_path / 'channel-ack-boundary-evidence.json'
        self.evidence = {}
        self.patch.setattr(main, '_proactive_clock', lambda: self.now[0])
        self.patch.setattr(main, 'model_adapter', self.model)

    def record(self, stage, **values):
        self.evidence.update(stage=stage, role=self.role, **values)
        self.evidence_path.write_text(json.dumps(self.evidence, indent=2), encoding='utf-8')

    async def start(self):
        if self.original_ack:
            base, _, _ = _ack_source(self.source, self.role)
            grant = base.model_copy(update={'ackContract': 'legacy-ttl'})
        else:
            server = self.source[1].save(self.source[2].model_copy(update={'roleIds': [self.role], 'timeoutSeconds': 10}))
            grant = grant_for(self.source).model_copy(update={
                'configRevision': server.revision, 'grantRevision': server.grant_id})
        (self.root / 'events.json').write_text(json.dumps([{'id': 'original-remote-content', 'kind': 'content',
            'title': 'PRIVATE BOUNDARY TITLE', 'body': 'PRIVATE BOUNDARY BODY'}]), encoding='utf-8')
        self.application = main._proactive_application()
        self.owners.append((self.application, self.original_host, self.api[3], main.channel_host))
        model_id = self.client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
            'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-only', 'apiKey': 'fixture'}).json()['configuration']['id']
        state, _ = save_external(self.client, self.path, self.account, {'activeDecisionEnabled': True,
            'decisionModelConfigurationId': model_id, 'consentRevision': 1, 'sourceGrants': [grant.model_dump(mode='json')]})
        self.grant = SourceGrant.model_validate(state['config']['sourceGrants'][0])
        assert self.grant.revision == 1
        with self.application._transaction() as db:
            db.execute("""CREATE TRIGGER fixture_block_boundary_projection BEFORE INSERT ON messages
                WHEN json_extract(NEW.metadata_json,'$.internalEvent')='active_decision'
                BEGIN SELECT RAISE(ABORT,'fixture: retain original unresolved projection'); END""")
        self.now[0] += 1800
        assert await self.application.on_clock() == 1
        state = await _runtime_settled(self.application, self.role)
        assert state['acknowledgements'] == []
        self.tick, self.intent = state['runs'][0]['id'], state['deliveries'][0]['intentId']
        assert state['deliveries'][0]['status'] in {'dispatching', 'pending', 'uncertain'}
        self.assert_no_replay()
        _runtime_released(self.application)
        with self.application._transaction() as db:
            self.encoded = db.execute('SELECT disposition_inputs FROM autonomy_delivery_recovery WHERE intent_id=?', (self.intent,)).fetchone()[0]
            assert json.loads(self.encoded)['sourceGrants'] == [self.grant.model_dump(mode='json')]
            assert db.execute('SELECT sent_disposition_decision_at FROM autonomy_delivery_recovery WHERE intent_id=?', (self.intent,)).fetchone()[0] is None
        self.record('original-unknown', tick=self.tick, intent=self.intent)

    def assert_no_replay(self):
        assert [call['name'] for call in _calls(self.root)] == ['get_events']
        assert self.model.requests == 3 and len(self.platform[0]['sent']) == 1
        assert self.platform[0]['sent'][0]['user_id'] == 101

    def replace_sources(self, grants):
        current = self.client.get(self.path).json()['config']
        updated = self.client.put(self.path, json={'expectedRevision': current['revision'],
            'config': {**current, 'sourceGrants': grants}})
        assert updated.status_code == 200, updated.text
        return updated.json()['config']

    def prune_original_snapshot(self):
        self.now[0] += 86401
        with self.application._transaction() as db:
            pruned = retention.prune(db, self.application._now(db))
            snapshot = json.loads(db.execute('SELECT snapshot FROM autonomy_runs WHERE tick_id=?', (self.tick,)).fetchone()[0])
            assert pruned['expiredSnapshots'] == 1 and pruned['protectedRuns'] >= 1
            assert set(snapshot) == {'sources', 'expired'} and snapshot['expired'] is True
            assert db.execute('SELECT disposition_inputs FROM autonomy_delivery_recovery WHERE intent_id=?', (self.intent,)).fetchone()[0] == self.encoded
        self.record('pruned-original', snapshot=snapshot)

    async def close_owner(self, owner):
        application, host, tasks, channel_host = owner
        if id(application) in self.closed:
            return
        try:
            await application.close()
        finally:
            try:
                await host.close()
            finally:
                try:
                    await channel_host.pause_and_drain(self.account.account_id)
                    await channel_host.stop(self.account.account_id)
                    channel_host.unregister(self.account.account_id)
                finally:
                    await asyncio.to_thread(tasks.close)
        assert not application.has_owned_work and not host.autonomy_cleanup_pending(self.role)
        self.closed.add(id(application))

    async def rebuild(self):
        old = self.owners[-1]
        await self.close_owner(old)
        sessions = SessionStore(old[0].sessions.database_path)
        tasks, host = TaskRuntime(old[2].root), McpHost(self.source[1])
        channels = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
        channel_host = ChannelHost(channels)
        channel_host.register(NapCatAdapter(NapCatConfig(self.account.account_id), transport=self.platform[1]))
        self.patch.setattr(main, 'session_store', sessions)
        self.patch.setattr(main, 'task_runtime', tasks)
        self.patch.setattr(main, 'multichannel_store', channels)
        self.patch.setattr(main, 'channel_host', channel_host)
        self.patch.setattr(main, 'mcp_host', host)
        self.patch.setattr(main, '_proactive', None)
        try:
            self.application = main._proactive_application()
        except BaseException:
            await host.close()
            await channel_host.stop(self.account.account_id)
            await asyncio.to_thread(tasks.close)
            raise
        self.owners.append((self.application, host, tasks, channel_host))
        with self.application._transaction() as db:
            assert db.execute('SELECT disposition_inputs FROM autonomy_delivery_recovery WHERE intent_id=?', (self.intent,)).fetchone()[0] == self.encoded

    def allow_original_projection(self):
        with self.application._transaction() as db:
            db.execute('DROP TRIGGER fixture_block_boundary_projection')

    def original_ack_row(self):
        with self.application._transaction() as db:
            rows = db.execute('SELECT * FROM autonomy_ack_outbox WHERE tick_id=?', (self.tick,)).fetchall()
            assert len(rows) == 1
            row = dict(rows[0])
        assert row['source_revision'] == 1 and row['event_id'] == 'original-remote-content'
        assert json.loads(row['source_grant']) == self.grant.model_dump(mode='json')
        return row

    def assert_business_released(self):
        with self.application._transaction() as db:
            reservation = dict(db.execute('SELECT * FROM autonomy_storage_reservations WHERE operation_id=?', (self.tick,)).fetchone())
        self.record('checking-resolved-reservation', reservation=reservation)
        assert (reservation['remaining_bytes'], reservation['rows_reserved'], reservation['metadata_reserved']) == (0, 0, 0)
        _runtime_released(self.application)

    async def close(self):
        errors = []
        for owner in reversed(self.owners):
            try:
                await self.close_owner(owner)
            except Exception as error:
                errors.append(error)
        self.record(self.evidence.get('stage', 'setup'), allOwnersClosed=not errors)
        if errors:
            raise ExceptionGroup('channel ACK boundary cleanup', errors)


@contextmanager
def _case(api_fixture, monkeypatch, platform_fixture, source_fixture, tmp_path, *, original_ack=True):
    host = McpHost(source_fixture[1])
    monkeypatch.setattr(main, 'mcp_host', host)
    try:
        with role_target(api_fixture, monkeypatch, platform_fixture) as (client, role, account, _):
            case = _OriginalChannel(api_fixture, monkeypatch, platform_fixture, source_fixture, tmp_path,
                                    client, role, account, host, original_ack=original_ack)
            yield case
    finally:
        asyncio.run(host.close())


def _run(case, scenario):
    async def exercise():
        try:
            await case.start()
            await scenario()
        finally:
            await case.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('permission', ['unsupported', 'revoked', 'aba'])
def test_original_permission_cannot_be_upgraded_by_late_receipt_or_source_recreation(
        api, monkeypatch, platform, source, tmp_path, permission):
    with _case(api, monkeypatch, platform, source, tmp_path, original_ack=permission != 'unsupported') as case:
        async def scenario():
            if permission != 'unsupported':
                removed = case.replace_sources([])
                assert removed['sourceGrants'] == []
                if permission == 'aba':
                    restored = case.replace_sources([case.grant.model_dump(mode='json')])
                    assert restored['sourceGrants'][0]['revision'] > case.grant.revision
                    assert {key: value for key, value in restored['sourceGrants'][0].items() if key != 'revision'} == {
                        key: value for key, value in case.grant.model_dump(mode='json').items() if key != 'revision'}
            case.prune_original_snapshot()
            await case.rebuild()
            case.allow_original_projection()
            first_decision = case.now[0]
            for _ in range(3):
                await case.application.recover()
            row = case.original_ack_row()
            assert row['status'] == ('unsupported' if permission == 'unsupported' else 'revoked')
            assert row['attempts'] == 0 and row['decision_at'] == first_decision
            assert row['expires_at'] == first_decision + 604800
            delivered = case.application.query(case.role)['deliveries'][0]
            assert delivered['status'] == 'sent' and delivered['platformMessageId'] == '8301'
            original = row.copy()
            await case.rebuild()
            case.now[0] += 900
            for _ in range(3):
                await case.application.recover()
            assert case.original_ack_row() == original
            case.assert_no_replay()
            case.assert_business_released()
        _run(case, scenario)


@pytest.mark.parametrize('elapsed', [601, 604801], ids=['original-600-second-window', 'original-finite-expiry'])
def test_original_ack_decision_cannot_open_a_new_window_after_late_admission(
        api, monkeypatch, platform, source, tmp_path, elapsed):
    with _case(api, monkeypatch, platform, source, tmp_path) as case:
        async def scenario():
            case.prune_original_snapshot()
            await case.rebuild()
            case.allow_original_projection()
            with case.application._transaction() as db:
                db.execute("""CREATE TRIGGER fixture_block_original_ack_admission BEFORE INSERT ON autonomy_runs
                    WHEN NEW.kind='autonomy_ack'
                    BEGIN SELECT RAISE(ABORT,'fixture: original source ACK admission unavailable'); END""")
            first_decision = case.now[0]
            with pytest.raises(sqlite3.IntegrityError, match='original source ACK admission unavailable'):
                await case.application.recover()
            decided = case.original_ack_row()
            assert decided['status'] == 'pending' and decided['attempts'] == 0
            assert decided['decision_at'] == first_decision and decided['expires_at'] == first_decision + 604800
            with case.application._transaction() as db:
                assert db.execute("SELECT COUNT(*) FROM autonomy_runs WHERE kind='autonomy_ack'").fetchone()[0] == 0
                db.execute('DROP TRIGGER fixture_block_original_ack_admission')
            case.now[0] += elapsed
            for _ in range(3):
                await case.application.recover()
            expired = case.original_ack_row()
            assert expired == {**decided, 'status': 'expired'}
            assert case.application.query(case.role)['deliveries'][0]['status'] == 'sent'
            await case.rebuild()
            await case.application.recover()
            assert case.original_ack_row() == expired
            case.assert_no_replay()
            case.assert_business_released()
        _run(case, scenario)


@pytest.mark.parametrize('owner_report', [False, True], ids=['unknown', 'reported-sent-is-not-a-receipt'])
def test_unknown_projection_and_owner_report_do_not_create_success_ack(
        api, monkeypatch, platform, source, tmp_path, owner_report):
    with _case(api, monkeypatch, platform, source, tmp_path) as case:
        async def scenario():
            if owner_report:
                delivery = case.application.query(case.role)['deliveries'][0]
                report = case.client.post(case.path + '/deliveries/' + case.intent + '/reconciliation', json={
                    'requestId': str(uuid.uuid4()), 'expectedResolutionRevision': 0,
                    'expectedStatus': delivery['status'], 'decision': 'reported_sent',
                    'allowFuture': False, 'note': '主人报告；保留原回执和投递事实'})
                assert report.status_code == 200, report.text
                assert report.json()['deliveries'][0]['resolution']['decision'] == 'reported_sent'
            case.prune_original_snapshot()
            await case.rebuild()
            for _ in range(3):
                await case.application.recover()
            current = case.application.query(case.role)
            assert current['acknowledgements'] == []
            assert current['deliveries'][0]['status'] in {'dispatching', 'pending', 'uncertain'}
            assert current['deliveries'][0]['projectionPending'] is True
            if owner_report:
                assert current['deliveries'][0]['resolution']['decision'] == 'reported_sent'
            with case.application._transaction() as db:
                assert db.execute('SELECT sent_disposition_decision_at FROM autonomy_delivery_recovery WHERE intent_id=?', (case.intent,)).fetchone()[0] is None
                assert db.execute('SELECT remaining_bytes FROM autonomy_storage_reservations WHERE operation_id=?', (case.tick,)).fetchone()[0] > 0
                assert db.execute('SELECT COUNT(*) FROM autonomy_event_claims WHERE tick_id=? AND expires_at IS NULL', (case.tick,)).fetchone()[0] == 1
            case.assert_no_replay()
            _runtime_released(case.application)
            case.record('unknown-duty-retained', ownerReport=owner_report)
        _run(case, scenario)
