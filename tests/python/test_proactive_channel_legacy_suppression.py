"""Old-format keys independently suppress real later autonomous deliveries.

Migration fixtures below retain only desktop-namespace keys from actual current
desktop/channel runs. They are NOT claimed to be old-baseline production runs.
Expected legacy hashes use the exact 312b7f1 delivery.py algorithm (Git blob
40411014cdd403b1b7bb71c8d822090ee8321f5d), with literal canonical text/URL values.
312 A01 desktop keys/message/receipt were atomic; failed desktop transactions
cannot honestly supply a committed old key, and 312 Drift did not write keys.
"""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import uuid

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelTextBlock, ModelToolCallBlock
from backend.app.channels.host import ChannelHost
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.mcp_runtime import McpHost
from backend.app.multichannel import MultichannelStore
from backend.app.proactive_runtime import retention
from backend.app.session_store import SessionStore
from backend.app.task_runtime import TaskRuntime
from test_background_shell_api import MemoryCredentials
from test_proactive_channel_delivery import channel_for
from test_proactive_channel_failure_retention import failure_platform  # noqa: F401
from test_proactive_channel_human_priority import owner_http
from test_proactive_drift_delivery import prepare, settled
from test_proactive_snapshot import grant_for
from test_proactive_targets_api import api, shell_api, platform, source, save_external  # noqa: F401


pytestmark = pytest.mark.usefixtures('local_platform_only')
_ORIGINAL = 'Ｌｅｇａｃｙ   NOTE'
_CANONICAL_URL = 'https://example.invalid/article?a=1&b=2'


def _expected_keys(role, namespace):
    # Literal normalized values are independent of the current _keys helpers.
    return {hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            for value in ([role, namespace, 'legacy note'], [role, namespace, [_CANONICAL_URL]])}


class _Decision:
    def __init__(self, now, message, *, delay=0):
        self.now, self.message, self.delay, self.requests = now, message, delay, 0

    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        self.requests += 1
        if not tools:
            # A missing legacy lookup must reach a permissive semantic guard,
            # not accidentally pass because that unrelated guard rejects it.
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelTextBlock('{"duplicate":false}'),)))
            return
        if self.requests == 1:
            name, args = 'get_content_events', {}
        elif self.requests == 2:
            self.now[0] += self.delay
            identifier = json.loads(messages[-1].content[0].text)[0]['id']
            name, args = 'message_push', {'message': self.message, 'mood': '', 'thought': '', 'evidence': [identifier]}
        else:
            assert self.requests == 3
            name, args = 'finish_turn', {'decision': 'reply'}
        yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'legacy-a01-{self.requests}', name, args),)))


class _Drift:
    def __init__(self, message, *, sent):
        self.message, self.sent, self.requests = message, sent, 0

    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        self.requests += 1
        if self.requests == 1:
            name, args = 'activate_skill', {'skill_id': 'reading-notes'}
        elif self.requests == 2:
            name, args = 'message_push', {'message': self.message, 'mood': '', 'thought': ''}
        else:
            assert self.requests == 3
            assert messages[-1].is_error == (not self.sent)
            name, args = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '实际去重与投递结果',
                'next_action': '保留原身份与原占用', 'message_result': 'sent' if self.sent else 'silent'}
        yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'legacy-drift-{self.requests}', name, args),)))


@contextmanager
def _prepared(api_fixture, monkeypatch, peer, source_fixture, tmp_path):
    root, mcp_store, server_input, *_ = source_fixture
    host = McpHost(mcp_store)
    monkeypatch.setattr(main, 'mcp_host', host)
    client, _, _, tasks = api_fixture
    role, path, now = prepare(client, monkeypatch, extra_tools=('message_push',), clock_start=1_782_300_000.0)
    account = channel_for(main.multichannel_store, role, peer[1])
    server = mcp_store.save(server_input.model_copy(update={'roleIds': [role], 'timeoutSeconds': 10}))
    grant = grant_for(source_fixture).model_copy(update={'configRevision': server.revision, 'grantRevision': server.grant_id})
    (root / 'events.json').write_text(json.dumps([{'id': 'old-remote', 'kind': 'content',
        'title': 'PRIVATE LEGACY TITLE', 'body': 'PRIVATE LEGACY BODY',
        'url': 'https://Example.invalid/article?b=2&utm_source=fixture&a=1#fragment'}]), encoding='utf-8')
    owners = [(main._proactive, host, tasks, main.channel_host)]
    closed, evidence = set(), {'migrationFixture': 'desktop namespace only; not an old-baseline production run'}
    with owner_http() as (http, invoke):
        ctx = {'client': http, 'invoke': invoke, 'app': main._proactive, 'role': role, 'path': path,
            'now': now, 'account': account, 'grant': grant, 'root': root, 'peer': peer, 'owners': owners}

        def record(stage, **fields):
            evidence.update(stage=stage, **fields)
            (tmp_path / 'legacy-suppression-evidence.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        ctx['record'] = record

        async def close(owner):
            application, mcp, runtime, channels = owner
            if id(application) in closed:
                return
            try:
                await application.close()
            finally:
                try:
                    await mcp.close()
                finally:
                    try:
                        await channels.pause_and_drain(account.account_id)
                        await channels.stop(account.account_id)
                        channels.unregister(account.account_id)
                    finally:
                        await asyncio.to_thread(runtime.close)
            assert not application.has_owned_work and not mcp.autonomy_cleanup_pending(role)
            closed.add(id(application))

        async def rebuild():
            await close(owners[-1])
            old = owners[-1]
            sessions = SessionStore(old[0].sessions.database_path)
            runtime, mcp = TaskRuntime(old[2].root), McpHost(mcp_store)
            store = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
            channels = ChannelHost(store)
            channels.register(NapCatAdapter(NapCatConfig(account.account_id), transport=peer[1]))
            monkeypatch.setattr(main, 'session_store', sessions)
            monkeypatch.setattr(main, 'task_runtime', runtime)
            monkeypatch.setattr(main, 'multichannel_store', store)
            monkeypatch.setattr(main, 'channel_host', channels)
            monkeypatch.setattr(main, 'mcp_host', mcp)
            monkeypatch.setattr(main, '_proactive', None)
            try:
                ctx['app'] = main._proactive_application()
            except BaseException:
                await mcp.close()
                await channels.stop(account.account_id)
                await asyncio.to_thread(runtime.close)
                raise
            owners.append((ctx['app'], mcp, runtime, channels))
        ctx['rebuild'] = rebuild
        try:
            yield ctx
        finally:
            errors = []
            for owner in reversed(owners):
                try:
                    invoke(close(owner))
                except Exception as error:
                    errors.append(error)
            record(evidence.get('stage', 'setup'), allOwnersClosed=not errors)
            if errors:
                raise ExceptionGroup('legacy suppression fixture cleanup', errors)


def _configure(ctx, **changes):
    current = ctx['client'].get(ctx['path']).json()['config']
    response = ctx['client'].put(ctx['path'], json={'expectedRevision': current['revision'], 'config': {**current, **changes}})
    assert response.status_code == 200, response.text
    return response.json()['config']


def _keys(ctx, intent):
    with ctx['app']._transaction() as db:
        return [tuple(row) for row in db.execute('SELECT * FROM autonomy_delivery_keys WHERE intent_id=? ORDER BY key', (intent,))]


def _legacy_only(ctx, intent):
    legacy, modern = _expected_keys(ctx['role'], 'desktop'), _expected_keys(ctx['role'], 'autonomy')
    with ctx['app']._transaction() as db:
        rows = db.execute('SELECT * FROM autonomy_delivery_keys WHERE intent_id=?', (intent,)).fetchall()
        assert {row['key'] for row in rows} == legacy | modern
        # A controlled migration fixture removes ONLY the new namespace. The
        # actual old-format hash/identity/expiry rows remain byte-for-byte.
        for key in modern:
            assert db.execute('DELETE FROM autonomy_delivery_keys WHERE role_id=? AND intent_id=? AND key=?',
                              (ctx['role'], intent, key)).rowcount == 1
    result = _keys(ctx, intent)
    assert {row[1] for row in result} == legacy
    return result


def _prune(ctx, tick):
    with ctx['app']._transaction() as db:
        result = retention.prune(db, ctx['app']._now(db))
        snapshot = json.loads(db.execute('SELECT snapshot FROM autonomy_runs WHERE tick_id=?', (tick,)).fetchone()[0])
        assert result['expiredSnapshots'] >= 1 and set(snapshot) == {'sources', 'expired'}
        assert snapshot['expired'] is True and 'PRIVATE LEGACY' not in json.dumps(snapshot)
    return snapshot


def _tick(ctx, monkeypatch, model):
    monkeypatch.setattr(main, 'model_adapter', model)
    state = ctx['invoke'](settled(ctx['app'], ctx['role']))
    assert model.requests == 3, state
    return state


@pytest.mark.parametrize('candidate', ['a01-content', 'a01-url', 'drift-content'])
def test_legacy_desktop_key_independently_suppresses_a_new_target_after_real_snapshot_expiry(
        api, monkeypatch, platform, source, tmp_path, candidate):
    with _prepared(api, monkeypatch, platform, source, tmp_path) as ctx:
        config = ctx['client'].get(ctx['path']).json()['config']
        _configure(ctx, activeDecisionEnabled=True, driftEnabled=False,
            decisionModelConfigurationId=config['driftModelConfigurationId'], sourceGrants=[ctx['grant'].model_dump(mode='json')])
        ctx['now'][0] += 1800
        original = _tick(ctx, monkeypatch, _Decision(ctx['now'], _ORIGINAL, delay=120))
        delivery = original['deliveries'][0]
        assert delivery['status'] == 'sent' and delivery['target']['ref'] == 'desktop'
        assert platform[0]['sent'] == []
        intent, tick = delivery['intentId'], original['runs'][0]['id']
        old_keys = _legacy_only(ctx, intent)
        started = original['runs'][0]['startedAt']
        config = ctx['client'].get(ctx['path']).json()['config']
        save_external(ctx['client'], ctx['path'], ctx['account'], {**config,
            'activeDecisionEnabled': candidate != 'drift-content', 'driftEnabled': candidate == 'drift-content',
            'sourceGrants': [] if candidate == 'drift-content' else config['sourceGrants']}, expected_revision=config['revision'])
        (source[0] / 'events.json').write_text(json.dumps([{'id': 'fresh-remote', 'kind': 'content',
            'body': 'fresh body', 'url': _CANONICAL_URL if candidate == 'a01-url' else 'https://example.invalid/different'}]), encoding='utf-8')
        ctx['now'][0] = started + 86401
        snapshot = _prune(ctx, tick)
        assert all(row[3] > ctx['now'][0] for row in old_keys)
        model = _Drift('legacy note', sent=False) if candidate == 'drift-content' else _Decision(
            ctx['now'], 'new wording for the same referenced article' if candidate == 'a01-url' else 'legacy note')
        result = _tick(ctx, monkeypatch, model)
        assert result['runs'][0]['status'] == ('finished_silent' if candidate == 'drift-content' else 'duplicate')
        assert len(result['deliveries']) == 1 and platform[0]['sent'] == []
        assert _keys(ctx, intent) == old_keys
        with ctx['app']._transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM autonomy_deliveries WHERE role_id=? AND status IN ('dispatching','pending','uncertain')", (ctx['role'],)).fetchone()[0] == 0
        ctx['record']('old-format-key-alone-suppressed-new-target', candidate=candidate, oldKeys=old_keys,
                      snapshot=snapshot, originalIntent=intent)


@pytest.mark.parametrize('failure_platform', ['uncertain'], indirect=True)
def test_manual_future_permission_and_new_drift_budgets_cannot_release_old_unknown_occupancy(
        api, monkeypatch, failure_platform, source, tmp_path):
    with _prepared(api, monkeypatch, failure_platform, source, tmp_path) as ctx:
        config = ctx['client'].get(ctx['path']).json()['config']
        save_external(ctx['client'], ctx['path'], ctx['account'], {**config,
            'activeDecisionEnabled': True, 'driftEnabled': False,
            'decisionModelConfigurationId': config['driftModelConfigurationId'],
            'sourceGrants': [ctx['grant'].model_dump(mode='json')]}, expected_revision=config['revision'])
        ctx['now'][0] += 1800
        initial = _tick(ctx, monkeypatch, _Decision(ctx['now'], _ORIGINAL))
        original = initial['deliveries'][0]
        assert original['status'] == 'uncertain' and len(failure_platform[0]['accepted']) == 1
        intent, tick = original['intentId'], initial['runs'][0]['id']
        old_keys = _legacy_only(ctx, intent)
        with ctx['app']._transaction() as db:
            claim = dict(db.execute('SELECT * FROM autonomy_deliveries WHERE intent_id=?', (intent,)).fetchone())
            events = [tuple(row) for row in db.execute('SELECT * FROM autonomy_event_claims WHERE tick_id=?', (tick,))]
        assert len(events) == 1 and events[0][3] is None
        current = ctx['client'].get(ctx['path']).json()['config']
        _configure(ctx, activeDecisionEnabled=False, driftEnabled=True, sourceGrants=[],
                   defaultTargetRef='desktop', allowedTargetRefs=['desktop', *current['allowedTargetRefs']])
        ctx['now'][0] += 86401
        _prune(ctx, tick)
        assert all(row[3] < ctx['now'][0] for row in old_keys)
        ctx['invoke'](ctx['rebuild']())
        ctx['now'][0] += 1800
        # Different text has no old key: failure before the owner report must
        # be the still-unresolved role gate, including on a new daily budget.
        blocked = _tick(ctx, monkeypatch, _Drift('fresh control permitted only after owner report', sent=False))
        assert blocked['runs'][0]['status'] == 'finished_silent' and len(blocked['deliveries']) == 1
        request = {'requestId': str(uuid.uuid4()), 'expectedResolutionRevision': 0, 'expectedStatus': original['status'],
            'decision': 'reported_not_sent', 'allowFuture': True, 'note': '主人允许后续新动作；原未知事实仍须保留'}
        response = ctx['client'].post(ctx['path'] + '/deliveries/' + intent + '/reconciliation', json=request)
        assert response.status_code == 200, response.text
        assert response.json()['deliveries'][0]['resolution']['allowFuture'] is True
        assert _keys(ctx, intent) == old_keys
        with ctx['app']._transaction() as db:
            assert dict(db.execute('SELECT * FROM autonomy_deliveries WHERE intent_id=?', (intent,)).fetchone()) == claim
            assert [tuple(row) for row in db.execute('SELECT * FROM autonomy_event_claims WHERE tick_id=?', (tick,))] == events
        # The same unrelated text now really commits once on desktop, proving
        # that the role gate, skill, target and new budget cannot mask dedupe.
        ctx['now'][0] += 86401
        control = _tick(ctx, monkeypatch, _Drift('fresh control permitted only after owner report', sent=True))
        assert control['runs'][0]['status'] == 'finished_sent' and len(control['deliveries']) == 2
        ctx['now'][0] += 86401
        suppressed = _tick(ctx, monkeypatch, _Drift('legacy note', sent=False))
        assert suppressed['runs'][0]['status'] == 'finished_silent' and len(suppressed['deliveries']) == 2
        assert len(failure_platform[0]['sendRequests']) == 1 and len(failure_platform[0]['accepted']) == 1
        assert _keys(ctx, intent) == old_keys
        with ctx['app']._transaction() as db:
            assert dict(db.execute('SELECT * FROM autonomy_deliveries WHERE intent_id=?', (intent,)).fetchone()) == claim
            assert [tuple(row) for row in db.execute('SELECT * FROM autonomy_event_claims WHERE tick_id=?', (tick,))] == events
            assert db.execute('SELECT COUNT(DISTINCT day) FROM autonomy_budgets WHERE role_id=? AND drifts>0', (ctx['role'],)).fetchone()[0] >= 3
            assert db.execute('SELECT COUNT(*) FROM autonomy_runs WHERE role_id=? AND (runtime_acknowledged=0 OR result_ack_pending<>0)', (ctx['role'],)).fetchone()[0] == 0
        ctx['invoke'](ctx['rebuild']())
        ctx['invoke'](ctx['app'].recover())
        assert _keys(ctx, intent) == old_keys and len(failure_platform[0]['sendRequests']) == 1
        assert [json.loads(line)['name'] for line in (source[0] / 'calls.jsonl').read_text().splitlines()] == ['get_events']
        ctx['record']('manual-future-opened-gate-but-preserved-old-key-and-event', oldKeys=old_keys,
                      originalIntent=intent, originalClaim=claim, originalEvents=events, ownerGenerations=len(ctx['owners']))
