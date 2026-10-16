"""Real SQLite/Runtime recovery fixtures; these do not claim native Git validation."""
import asyncio
from dataclasses import asdict
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
import sys

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.coding_tasks.ledger import _json
from backend.app.coding_tasks import owned_process, publication
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskBudget, TaskOrigin, TaskOutcome, TaskRuntime
from test_coding_application import GrantedConfiguration

pytestmark = pytest.mark.usefixtures('local_platform_only')


def recovery_fixture(tmp_path, *, channel=False):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    channels = MultichannelStore(sessions.database_path, sessions)
    if channel:
        account = channels.register_channel_account('role-a', 'qq', account_id='fixture-account', enabled=True)
        thread = channels.ensure_channel_thread('role-a', 'qq', account.account_id, 'original-owner', 'private')
        origin = TaskOrigin('role-a', thread.session_key, 'parent', 'call', source_account_id=account.account_id,
            source_thread_id=thread.thread_id, source_scope_id='immutable-source')
    else:
        thread = None
        origin = TaskOrigin('role-a', 'role:role-a', 'parent', 'call')
    tasks = TaskRuntime(tmp_path / 'tasks', retention_seconds=0)
    app = CodingApplication(tmp_path / 'coding', tasks, sessions,
        config=GrantedConfiguration(), role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True)
    record, _ = app.ledger.admit(origin, requirement='persisted recovery fixture',
        repository={'id': 'repo-a', 'revision': 'r'},
        profile={'id': 'profile-a', 'revision': 'p', 'executor': 'herdr'}, max_daily=5)
    app.ledger.update_attempt(record['attemptId'], status='unknown', runtimeStatus='interrupted',
        cleanupState='complete', messageSynced=True)
    record = app.ledger.attempt(record['attemptId'])
    operation = {'id': 'codingop_recovery', 'roleId': 'role-a', 'taskId': record['id'],
        'attemptId': record['attemptId'], 'action': 'commit', 'candidate': {'record': record},
        'origin': asdict(origin), 'status': 'validated_commit', 'runtimeStatus': 'interrupted',
        'runtimeTaskId': None, 'runtimeAckDirty': False, 'cleanupState': 'complete',
        'deliveryStatus': 'pending', 'messageSynced': False, 'resourceJobs': [], 'sealed': False}
    with app.ledger.transaction() as db:
        db.execute('INSERT INTO coding_owner_operations VALUES(?,?,?,?,?,?)',
            (operation['id'], 'role-a', record['id'], 'unique-recovery-request', 'fingerprint', _json(operation)))
    return app, tasks, sessions, channels, thread, origin, record, operation


def test_late_durable_operation_receipt_repairs_same_source_projection_without_resending(tmp_path):
    app, tasks, sessions, channels, thread, origin, native, operation = recovery_fixture(tmp_path, channel=True)
    sends = []
    async def deliver(record, content, authorized):
        assert authorized()
        sends.append(record['completionId'])
        receipt = channels.begin_delivery(thread, content, source_kind=record['completionKind'],
            source_tool_call_id=record['completionId'])
        channels.finish_delivery(receipt, status='sent', platform_message_id='fixture-sent')
        raise OSError('local acknowledgement lost after durable sent receipt')
    def receipt(record):
        value = channels.delivery_for_source('role-a', source_kind=record['completionKind'],
            source_tool_call_id=record['completionId'])
        return {'status': value[0], 'attemptId': value[1]} if value else None
    app.deliver, app.receipt = deliver, receipt
    try:
        asyncio.run(app.tick())
        first = sessions.list_messages(origin.session_key)
        assert len(first) == 1 and first[0].metadata['deliveryStatus'] == 'uncertain'
        asyncio.run(app.tick())
        current = app.publication.inspect('role-a', operation['id'])
        messages = sessions.list_messages(origin.session_key)
        assert current['deliveryStatus'] == 'sent'
        assert len(messages) == 1 and messages[0].id == first[0].id
        assert messages[0].metadata['deliveryStatus'] == 'sent' and messages[0].metadata['channel'] == 'qq'
        assert messages[0].runId is None and sends == [operation['id']]
        assert not sessions.list_messages('role:role-a')
    finally:
        asyncio.run(app.close())
        tasks.close()


def test_host_snapshot_rejects_repository_filters_before_any_filter_child(tmp_path):
    from backend.app.coding_tasks.evidence import snapshot_worktree
    from backend.app.coding_tasks.errors import CodingFailure
    from backend.app.coding_tasks.git_repository import git
    repository = tmp_path / 'repo'
    repository.mkdir()
    git(repository, 'init', '-b', 'main')
    git(repository, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base')
    (repository / 'fixture.txt').write_text('work')
    git(repository, 'add', 'fixture.txt')
    marker, script = tmp_path / 'filter-ran', tmp_path / 'filter.py'
    script.write_text('import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text("executed")\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n')
    command = ' '.join('"' + str(path).replace('\\', '/') + '"' for path in (Path(sys.executable), script, marker))
    git(repository, 'config', 'filter.fixture.clean', command)
    (repository / '.gitattributes').write_text('fixture.txt filter=fixture\n')
    try:
        with pytest.raises(CodingFailure, match='repository_git_filters_unavailable'):
            snapshot_worktree(repository, str(repository / '.git'), 'main', time.monotonic() + 10)
        assert not marker.exists(), 'host inspection must not leave an unowned filter descendant'
    finally:
        git(repository, 'config', '--remove-section', 'filter.fixture')


@pytest.mark.parametrize('hazard', ['hardlink', 'byte-limit'])
def test_review_refuses_private_artifact_redirection_or_retained_budget_before_writes(tmp_path, hazard):
    from backend.app.coding_tasks.errors import CodingFailure
    app, tasks, sessions, _, _, _, native, operation = recovery_fixture(tmp_path)
    artifact = app.publication.root / 'fixture-retained'
    external = tmp_path / 'unrelated-private'
    external.write_text('must remain unchanged')
    try:
        if hazard == 'hardlink':
            os.link(external, artifact)
        else:
            with artifact.open('wb') as stream:
                stream.truncate(2 * 1024 * 1024 * 1024 + 1)
        with pytest.raises(CodingFailure, match='publication_artifact_'):
            app.publication.prepare('role-a', native['id'], action='commit', message='Never written',
                author_name='Owner', author_email='owner@example.invalid')
        assert external.read_text() == 'must remain unchanged'
        assert not tasks.list(TaskOrigin(**native['origin']), kind='coding_commit')
    finally:
        artifact.unlink(missing_ok=True)
        asyncio.run(app.close())
        tasks.close()


def test_replacing_broker_credential_seals_and_joins_old_owner_before_enabling_new_revision(tmp_path):
    app, tasks, sessions, _, _, origin, native, operation = recovery_fixture(tmp_path)
    class Credentials:
        token = ''
        def get(self, key): return self.token
        def set(self, key, value): self.token = value
    app.publication.credentials = Credentials()
    app.publication.set_token('fixture-before')
    stopped, entered = threading.Event(), threading.Event()
    class Broker:
        def run(self, execution):
            entered.set()
            assert stopped.wait(10), 'credential replacement skipped old broker cleanup'
            app.publication._update(operation['id'], status='cancelled', cleanupState='complete')
            return TaskOutcome('cancelled')
        def cancel(self): stopped.set()
    owner = Broker()
    runtime = tasks.submit(origin, owner, kind='coding_publish', budget=TaskBudget(20, 100, 1024),
        submission_key=operation['id'], retain_until_ack=True, completion=False)
    assert entered.wait(2)
    app.publication._owners[operation['id']] = owner
    app.publication._update(operation['id'], action='publish', status='running', cleanupState='running', runtimeTaskId=runtime,
        candidate={'record': native, 'credentialRevision': 2})
    try:
        setting = app.publication.set_token('fixture-after')
        assert stopped.is_set()
        assert setting == {'revision': 3, 'configured': True, 'cleanupPending': False}
        assert app.publication._record(operation['id'])['sealed']
        assert tasks.query(runtime, origin).status == 'cancelled'
    finally:
        stopped.set()
        asyncio.run(app.close())
        tasks.close()


def test_operation_send_waits_for_original_role_lock_and_final_insert_rechecks_source(tmp_path):
    app, tasks, sessions, channels, thread, origin, native, operation = recovery_fixture(tmp_path, channel=True)
    allowed, sends = [True], []
    app.authorized = lambda _: allowed[0]
    lock = asyncio.Lock()
    app.lock_for = lambda _: lock
    async def deliver(record, content, authorized):
        assert authorized()
        sends.append(record['completionId'])
        receipt = channels.begin_delivery(thread, content, source_kind=record['completionKind'],
            source_tool_call_id=record['completionId'])
        channels.finish_delivery(receipt, status='sent', platform_message_id='real-local-receipt')
        return {'status': 'sent', 'attemptId': receipt}
    app.deliver = deliver
    def revoke_at_sequence_read(statement):
        if statement.startswith('SELECT COALESCE(MAX(sequence)'):
            allowed[0] = False
    app.ledger._db.set_trace_callback(revoke_at_sequence_read)
    async def scenario():
        await lock.acquire()
        pending = asyncio.create_task(app.tick())
        await asyncio.sleep(.05)
        assert not sends and not sessions.list_messages(origin.session_key)
        lock.release()
        await pending
        assert sends == [operation['id']]
        assert not sessions.list_messages(origin.session_key), 'permission changed immediately before final INSERT'
        assert not app.publication._record(operation['id'])['messageSynced']
        app.ledger._db.set_trace_callback(None)
        await app.tick()
        assert app.publication.inspect('role-a', operation['id'])['deliveryStatus'] == 'sent'
        assert not sessions.list_messages(origin.session_key) and not sessions.list_messages('role:role-a')
    try:
        asyncio.run(scenario())
    finally:
        app.ledger._db.set_trace_callback(None)
        asyncio.run(app.close())
        tasks.close()


def test_cancel_closes_http_even_when_git_cleanup_cannot_be_confirmed(tmp_path):
    from backend.app.coding_tasks.github_publication import GithubPlatform
    app, tasks, sessions, _, _, origin, native, operation = recovery_fixture(tmp_path)
    owner = publication.PublicationExecutor(app.publication, operation)
    class UnconfirmedGit:
        def close(self): raise owned_process.CleanupFailed('injected owned Git close failure')
    owner.git = UnconfirmedGit()
    owner.platform = GithubPlatform('fixture-only', {'owner': 'fixture', 'repository': 'repo'}, base_url='http://127.0.0.1:1')
    app.publication._owners[operation['id']] = owner
    app.publication._update(operation['id'], cleanupState='cleanup_failed')
    try:
        with pytest.raises(owned_process.CleanupFailed):
            app.publication.cancel('role-a', operation['id'])
        assert owner.platform.client.is_closed
        assert app.publication.inspect('role-a', operation['id'])['cleanupState'] == 'cleanup_failed'
    finally:
        owner.git = None
        asyncio.run(app.close())
        tasks.close()


def test_shutdown_retains_pending_original_source_result_for_restart_without_reexecution(tmp_path):
    app, tasks, sessions, _, _, origin, native, operation = recovery_fixture(tmp_path)
    root, configuration = app.root, app.config
    try:
        asyncio.run(app.close())
        with sqlite3.connect(sessions.database_path) as db:
            preserved = json.loads(db.execute('SELECT body FROM coding_owner_operations WHERE id=?', (operation['id'],)).fetchone()[0])
        assert preserved['deliveryStatus'] == 'pending' and not preserved['messageSynced']
        app = CodingApplication(root, tasks, sessions, config=configuration,
            role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
            executor_factory=lambda *_: (_ for _ in ()).throw(AssertionError('shutdown recovery must never rerun CLI')))
        asyncio.run(app.tick())
        assert app.publication.inspect('role-a', operation['id'])['deliveryStatus'] == 'sent'
        messages = sessions.list_messages(origin.session_key)
        assert len(messages) == 1 and messages[0].metadata['operationId'] == operation['id']
    finally:
        asyncio.run(app.close())
        tasks.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows private named Job recovery acceptance')
def test_restart_observes_actual_private_job_before_releasing_operation_capacity(tmp_path):
    import uuid
    app, tasks, sessions, _, _, origin, native, operation = recovery_fixture(tmp_path)
    job = 'Local\\Meido.Coding.publication-restart-' + uuid.uuid4().hex
    process = owned_process.OwnedProcess([sys.executable, '-I', '-c', 'import time;time.sleep(30)'],
        tmp_path, {}, job_name=job)
    app.publication._update(operation['id'], status='unknown', cleanupState='running', resourceJobs=[job])
    try:
        asyncio.run(app.tick())
        assert app.publication.inspect('role-a', operation['id'])['cleanupState'] == 'running'
        with pytest.raises(ValueError, match='concurrency budget'):
            app.publication.ensure_native_admission('role-a', 'repo-a')
        process.close()
        asyncio.run(app.tick())
        assert app.publication.inspect('role-a', operation['id'])['cleanupState'] == 'complete'
        app.publication.ensure_native_admission('role-a', 'repo-a')
    finally:
        process.close()
        asyncio.run(app.close())
        tasks.close()


def test_restarted_operation_confirms_journaled_resources_before_releasing_capacity(tmp_path):
    app, tasks, sessions, _, _, _, native, operation = recovery_fixture(tmp_path)
    app.publication._update(operation['id'], status='unknown', cleanupState='running', resourceJobs=[])
    try:
        asyncio.run(app.tick())
        assert app.publication.inspect('role-a', operation['id'])['cleanupState'] == 'complete'
        assert app.publication.inspect('role-a', operation['id'])['status'] == 'unknown'
    finally:
        app.publication._update(operation['id'], cleanupState='complete')
        asyncio.run(app.close())
        tasks.close()


def test_native_unconfirmed_cleanup_still_stops_independent_broker_and_retains_database(tmp_path, monkeypatch):
    app, tasks, sessions, _, _, origin, native, operation = recovery_fixture(tmp_path)
    stopped, entered = threading.Event(), threading.Event()
    class Broker:
        def run(self, execution):
            entered.set()
            assert stopped.wait(10), 'broker owner was skipped after native cleanup failure'
            return TaskOutcome('cancelled')
        def cancel(self): stopped.set()
    owner = Broker()
    runtime = tasks.submit(origin, owner, kind='coding_commit', budget=TaskBudget(20, 100, 1024),
        submission_key=operation['id'], retain_until_ack=True, completion=False)
    assert entered.wait(2)
    app.publication._owners[operation['id']] = owner
    app.publication._update(operation['id'], status='running', cleanupState='running', runtimeTaskId=runtime)
    app.ledger.update_attempt(native['attemptId'], cleanupState='running', resourceJobs=['Local\\Meido.Coding.fixture-unconfirmed'])
    available = [False]
    def observe(name):
        if not available[0]: raise owned_process.CleanupFailed('fixture native resource query failed')
        return False
    monkeypatch.setattr(owned_process, 'named_job_active', observe)
    try:
        with pytest.raises((owned_process.CleanupFailed, ExceptionGroup)):
            asyncio.run(app.close())
        assert stopped.is_set(), 'all independent resource kinds must drain even when another fails'
        assert not app._resources_closed
        assert app.ledger.attempt(native['attemptId'])['cleanupState'] == 'running'
        assert app.publication.inspect('role-a', operation['id'])['cleanupState'] == 'complete'
        available[0] = True
        asyncio.run(app.close())
        assert app._resources_closed
    finally:
        stopped.set()
        available[0] = True
        asyncio.run(app.close())
        tasks.close()
