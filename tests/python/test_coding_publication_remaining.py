"""Remaining-only approval survives lost POST receipts and Runtime pruning."""
import asyncio
import copy
import hashlib
import json
import sqlite3

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.coding_tasks.github_publication import GithubPlatform, GitTransport
from backend.app.coding_tasks.git_repository import git
from backend.app.coding_tasks.ledger import _json
from backend.app.multichannel import MultichannelStore
from backend.app.task_runtime import TaskOrigin, TaskRuntime
from test_coding_publication import owned_application, platform, settle

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_remaining_pr_lost_receipt_recovers_approved_chain_after_ack_prune_and_restart(tmp_path):
    app, tasks, sessions, _ = owned_application(tmp_path)
    channels = MultichannelStore(sessions.database_path, sessions)
    account = channels.register_channel_account('role-a', 'qq', account_id='original', enabled=True)
    thread = channels.ensure_channel_thread('role-a', 'qq', account.account_id, 'original-owner', 'private')
    origin = TaskOrigin('role-a', thread.session_key, 'original-parent', 'original-call',
        source_account_id=account.account_id, source_thread_id=thread.thread_id, source_scope_id='original-scope')
    sends = []
    def identity(record):
        return record.get('completionKind', 'coding_completion'), record.get('completionId', record['attemptId'])
    async def deliver(record, content, allowed):
        assert allowed()
        kind, identifier = identity(record)
        sends.append((kind, identifier))
        receipt = channels.begin_delivery(thread, content, source_kind=kind, source_tool_call_id=identifier)
        channels.finish_delivery(receipt, status='sent', platform_message_id='fixture-' + str(len(sends)))
        return {'status': 'sent', 'attemptId': receipt}
    def receipt(record):
        kind, identifier = identity(record)
        value = channels.delivery_for_source('role-a', source_kind=kind, source_tool_call_id=identifier)
        return {'status': value[0], 'attemptId': value[1]} if value else None
    app.deliver, app.receipt = deliver, receipt
    bare = tmp_path / 'bare.git'
    bare.mkdir()
    git(bare, 'init', '--bare', '-b', 'main')
    git(tmp_path / 'repo', 'push', str(bare), 'HEAD:refs/heads/main')
    pushes, calls = [], []
    class Credentials:
        def get(self, key): return 'fixture-broker-secret'
        def set(self, key, value): pass
    class CountingTransport(GitTransport):
        def push(self, worktree):
            pushes.append(self.candidate['commitSha'])
            super().push(worktree)
    class CountingPlatform(GithubPlatform):
        def _request(self, method, path, **kwargs):
            calls.append((method, path))
            return super()._request(method, path, **kwargs)
    try:
        task = app.start_task(origin, repository='fixture-repo', requirement='work', executor='codex_exec')
        settle(app)
        commit = app.publication.prepare('role-a', task['id'], action='commit', message='Reviewed',
            author_name='Owner', author_email='owner@example.invalid')
        committed = settle(app, app.publication.approve('role-a', commit['id'], request_id='commit'))
        assert committed['status'] == 'validated_commit'
        app.publication.credentials = Credentials()
        app.publication.set_token('fixture-broker-secret')
        with platform(tmp_path, bare) as (endpoint, state):
            factory = lambda token, remote: CountingPlatform(token, remote, base_url=endpoint)
            app.publication.platform_factory = factory
            app.publication.transport_factory = lambda *args: CountingTransport(*args, destination=str(bare))
            state['block_pulls'] = True
            first = app.publication.prepare('role-a', task['id'], action='publish', title='First', body='First')
            prior = settle(app, app.publication.approve('role-a', first['id'], request_id='first'))
            assert prior['pushConfirmed'] and not prior.get('prDispatched')
            state['block_pulls'] = False
            assert app.publication.observe('role-a', prior['id'])['status'] == 'push_confirmed'
            remaining = app.publication.prepare('role-a', task['id'], action='publish', title='New approved title',
                body='New explicit remaining approval', previous_operation_id=prior['id'])
            assert remaining['pushRequired'] is False
            state['lost'] = True
            result = settle(app, app.publication.approve('role-a', remaining['id'], request_id='remaining'))
            assert result['status'] == 'unknown' and result['prDispatched'] and not result.get('pushDispatched')
            persisted = app.publication._record(result['id'])
            runtime = persisted['runtimeTaskId']
            assert persisted['runtimeResult']['status'] == 'failed' and not persisted['runtimeAckDirty']
            assert len(pushes) == state['posts'] == 1
            tasks.retention_seconds = 0
            tasks.list(origin, kind='coding_publish')  # Real ACK released retention, then actual prune.
            with pytest.raises(KeyError): tasks.query(runtime, origin)
            assert tasks.find_submission(origin, kind='coding_publish', submission_key=result['id']) == runtime
            previous = app
            asyncio.run(app.close())
            tasks.close()
            tasks = TaskRuntime(tmp_path / 'runtime', retention_seconds=0)
            app = CodingApplication(tmp_path / 'coding', tasks, sessions, config=previous.config,
                role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
                executor_factory=lambda *_: (_ for _ in ()).throw(AssertionError('readback must not execute CLI')),
                publication_credentials=previous.publication.credentials, publication_platform_factory=factory,
                publication_transport_factory=lambda *_: (_ for _ in ()).throw(AssertionError('readback must not push')),
                deliver=deliver, receipt=receipt)
            # Already-dispatched PR effects remain unknown when GET is down;
            # that is never converted into another remaining write approval.
            state['block_pulls'] = True
            with pytest.raises(ValueError): app.publication.observe('role-a', result['id'])
            assert app.publication.inspect('role-a', result['id'])['status'] == 'unknown'
            with pytest.raises(ValueError):
                app.publication.prepare('role-a', task['id'], action='publish', title='No second POST',
                    body='Already dispatched', previous_operation_id=result['id'])
            assert len(pushes) == state['posts'] == 1
            state['block_pulls'] = False
            original = app.publication._record(prior['id'])
            original_current = app.publication._record(result['id'])
            baseline_calls = len(calls)
            # Each corruption is restored before the next, preserving the
            # genuine approved rows and final positive readback in this DB.
            for target, key, value in [('prior', 'resolvedBy', 'wrong-child'), ('prior', 'pushConfirmed', False),
                    ('prior', 'pushAcknowledged', False), ('prior', 'pushDispatched', False),
                    ('prior', 'prDispatched', True), ('prior', 'platformRepositoryId', 99),
                    ('prior', 'origin', origin.__dict__ | {'source_scope_id': 'another-scope'}),
                    ('current', 'priorPushId', result['id']), ('current', 'priorPushId', 'missing'),
                    ('current', 'pushDispatched', True),
                    ('current', 'platformRepositoryId', 99)]:
                record = copy.deepcopy(original if target == 'prior' else original_current)
                record[key] = value
                identifier = record['id']
                with app.ledger.transaction() as db:
                    db.execute('UPDATE coding_owner_operations SET body=? WHERE id=?', (_json(record), identifier))
                try:
                    with pytest.raises(PermissionError): app.publication.observe('role-a', result['id'])
                    assert len(calls) == baseline_calls, (target, key)
                finally:
                    with app.ledger.transaction() as db:
                        db.execute('UPDATE coding_owner_operations SET body=? WHERE id=?',
                            (_json(original if target == 'prior' else original_current), identifier))
            # Alternative approved-history fixtures retain a valid consumed
            # review/fingerprint but disagree with the current approval. This
            # exercises identity binding beyond detecting a corrupted digest.
            for key, value in [('commitSha', '0' * 40), ('head', '0' * 40), ('tree', '0' * 40),
                    ('branch', 'codex/another-native'), ('publishBranch', 'codex/another-push'),
                    ('baseHead', '0' * 40), ('baseRef', 'another-base'),
                    ('nativeSession', '00000000-0000-4000-8000-000000000000'),
                    ('remote', {'url': 'https://github.com/another/repo.git', 'owner': 'another', 'repository': 'repo'}),
                    ('source', origin.__dict__ | {'source_scope_id': 'another-scope'})]:
                alternative = copy.deepcopy(original)
                alternative['candidate'][key] = value
                payload = dict(alternative['candidate'])
                payload.pop('fingerprint')
                digest = hashlib.sha256(_json(payload).encode()).hexdigest()
                alternative['candidate']['fingerprint'] = digest
                with app.ledger.transaction() as db:
                    db.execute('UPDATE coding_reviews SET body=?,fingerprint=? WHERE id=?',
                        (_json(alternative['candidate']), digest, first['id']))
                    db.execute('UPDATE coding_owner_operations SET body=?,fingerprint=? WHERE id=?',
                        (_json(alternative), digest, prior['id']))
                try:
                    with pytest.raises(PermissionError): app.publication.observe('role-a', result['id'])
                    assert len(calls) == baseline_calls, key
                finally:
                    with app.ledger.transaction() as db:
                        db.execute('UPDATE coding_reviews SET body=?,fingerprint=? WHERE id=?',
                            (_json(original['candidate']), original['candidate']['fingerprint'], first['id']))
                        db.execute('UPDATE coding_owner_operations SET body=?,fingerprint=? WHERE id=?',
                            (_json(original), original['candidate']['fingerprint'], prior['id']))
            with app.ledger.transaction() as db:
                db.execute('UPDATE coding_reviews SET consumed_by=NULL WHERE id=?', (remaining['id'],))
            try:
                with pytest.raises(PermissionError): app.publication.observe('role-a', result['id'])
                assert len(calls) == baseline_calls
            finally:
                with app.ledger.transaction() as db:
                    db.execute('UPDATE coding_reviews SET consumed_by=? WHERE id=?', (result['id'], remaining['id']))
            app.authorized = lambda _: False
            with pytest.raises(PermissionError): app.publication.observe('role-a', result['id'])
            app.authorized = lambda _: True
            app.generation = lambda _: 1
            # Freshening the live record cannot revive the old approval's
            # frozen authority generation after an off/on ABA transition.
            app.ledger.update_attempt(task['attemptId'], authorityGeneration=1)
            with pytest.raises(PermissionError): app.publication.observe('role-a', result['id'])
            app.generation = lambda _: 0
            app.ledger.update_attempt(task['attemptId'], authorityGeneration=0)
            assert len(calls) == baseline_calls
            with app.ledger.transaction() as db:
                db.execute('UPDATE coding_publication_credentials SET revision=revision+1 WHERE id=1')
            with pytest.raises(PermissionError): app.publication.observe('role-a', result['id'])
            with app.ledger.transaction() as db:
                db.execute('UPDATE coding_publication_credentials SET revision=revision-1 WHERE id=1')
            assert len(calls) == baseline_calls
            observed = app.publication.observe('role-a', result['id'])
            assert observed['status'] == 'delivered_draft'
            asyncio.run(app.tick())
            actual = app.publication._record(result['id'])
            with sqlite3.connect(tmp_path / 'platform.db') as db:
                pull = json.loads(db.execute('SELECT body FROM pulls').fetchone()[0])
            assert actual['platformProof']['headSha'] == pull['head']['sha'] == committed['commitSha']
            assert actual['prUrl'] == pull['html_url'] and pull['draft'] and pull['state'] == 'open'
            assert actual['runtimeTaskId'] == runtime and actual['runtimeResult'] == persisted['runtimeResult']
            assert not actual['runtimeAckDirty'] and actual['cleanupState'] == 'complete'
            assert actual['deliveryStatus'] == 'sent' and actual['messageSynced']
            assert actual['priorPushId'] == prior['id'] and not actual.get('pushDispatched')
            assert actual['prDispatched'] and actual['candidate']['pushRequired'] is False
            assert len(pushes) == state['posts'] == 1 and all(method == 'GET' for method, _ in calls[baseline_calls:])
            messages = [item for item in sessions.list_messages(origin.session_key)
                if item.metadata.get('operationId') == result['id']]
            assert len(messages) == 1 and messages[0].runId is None
            assert messages[0].metadata['operationStatus'] == 'delivered_draft'
            assert messages[0].metadata['channel'] == 'qq' and messages[0].metadata['deliveryStatus'] == 'sent'
            assert sends.count(('coding_publication', result['id'])) == 1
            assert not sessions.list_messages('role:role-a')
            asyncio.run(app.tick())
            assert app.publication.observe('role-a', result['id'])['status'] == 'delivered_draft'
            assert len(pushes) == state['posts'] == 1 and sends.count(('coding_publication', result['id'])) == 1
    finally:
        asyncio.run(app.close())
        tasks.close()
