"""Owner publication policy, real Git and local protocol transactions."""
from pathlib import Path
import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import sqlite3
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from backend.app.coding_tasks.config import CodingConfigStore
from backend.app.coding_tasks.git_repository import git
from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.coding_tasks.codex_exec import CodexExecutor
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskOrigin, TaskOutcome, TaskRuntime
from test_codex_executor import fixture_process

pytestmark = pytest.mark.usefixtures('local_platform_only')


def repository(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--allow-empty', '-m', 'base')
    git(root, 'remote', 'add', 'origin', 'https://github.com/meido-fixture/publication.git')
    return root


def test_owner_can_grant_commit_and_publication_independently_and_freezes_actual_remote(tmp_path):
    root = repository(tmp_path)
    config = CodingConfigStore(tmp_path / 'configuration.db')
    try:
        local = config.save_repository({'root': str(root), 'allowedRoleIds': ['role-a'], 'localCommit': True})
        assert local['localCommit'] and not local['push'] and not local['draftPr']
        published = config.save_repository({'id': local['id'], 'root': str(root), 'allowedRoleIds': ['role-a'],
            'localCommit': True, 'push': True, 'draftPr': True})
        assert published['publicationRemote'] == {'url': 'https://github.com/meido-fixture/publication.git',
            'owner': 'meido-fixture', 'repository': 'publication'}
        assert published['revision'] != local['revision']
        assert not published['fetch']
    finally:
        config.close()


@pytest.mark.parametrize('url', ['https://secret@github.com/owner/repo.git',
    'https://github.com.evil.invalid/owner/repo.git', 'http://github.com/owner/repo.git',
    'https://github.com/owner/repo.git?token=secret', 'ssh://git@github.com/owner/repo.git'])
def test_publication_refuses_credential_urls_or_unowned_transport_before_any_dispatch(tmp_path, url):
    root = repository(tmp_path)
    git(root, 'remote', 'set-url', 'origin', url)
    config = CodingConfigStore(tmp_path / 'configuration.db')
    try:
        with pytest.raises(ValueError):
            config.save_repository({'root': str(root), 'push': True, 'draftPr': True})
        assert not config.public()['repositories']
    finally:
        config.close()


def owned_application(tmp_path, executor='codex_exec'):
    from backend.app.coding_tasks.git_repository import repository_identity
    root = repository(tmp_path)
    auth = tmp_path / 'auth'
    auth.mkdir()
    (auth / 'auth.json').write_text('{"fixture":"not-real"}')
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    tasks = TaskRuntime(tmp_path / 'runtime')
    class Configuration:
        def resolve(self, role_id, role, selected, **kwargs):
            return (repository_identity(str(root)) | {'id': 'fixture-repo', 'revision': 'repo-rev',
                'baseRef': 'main', 'remote': 'origin', 'localCommit': True, 'push': True, 'draftPr': True,
                'publicationRemote': {'url': 'https://github.com/meido-fixture/publication.git',
                    'owner': 'meido-fixture', 'repository': 'publication'},
                'validation': [{'id': 'real-check', 'argv': [sys.executable, '-I', '-c',
                    'from pathlib import Path; assert Path("fixture.txt").read_text(encoding="utf-8") in ("work", "continue work")'], 'cwd': '.'}]},
                {'id': 'fixture-profile', 'revision': 'profile-rev', 'kind': 'codex', 'executor': executor,
                 'version': '0.159.3', 'model': 'fixture', 'executable': str(Path(sys.executable).resolve()),
                 'authDirectory': str(auth), 'authenticationRevision': hashlib.sha256((auth / 'auth.json').read_bytes()).hexdigest()},
                {'fixture': 'only', 'executable': sys.executable})
        def close(self): pass
    def native_factory(record, ledger):
        if executor == 'herdr':
            from backend.app.coding_tasks.executor import HerdrExecutor
            from test_coding_attestation import LocalHerdrBoundary
            return HerdrExecutor(tmp_path / 'coding', record, ledger,
                authorized=lambda: app._execution_allowed(record), client_factory=LocalHerdrBoundary)
        return CodexExecutor(tmp_path / 'coding', record, ledger,
            authorized=lambda: app._execution_allowed(record), process_factory=fixture_process)
    app = CodingApplication(tmp_path / 'coding', tasks, sessions, role_config=lambda _: CodingRoleConfig(enabled=True),
        authorized=lambda _: True, config=Configuration(),
        executor_factory=native_factory)
    origin = TaskOrigin('role-a', 'role:role-a', 'parent', 'call')
    return app, tasks, sessions, origin


def settle(app, operation=None):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        asyncio.run(app.tick())
        if operation is not None:
            value = app.publication.inspect('role-a', operation['id'])
            if value['runtimeStatus'] in {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'} and not value['runtimeAckDirty']:
                return value
        else:
            records = app.ledger.role_records('role-a')
            if records and records[-1]['runtimeStatus'] in {'succeeded', 'failed'}:
                return records[-1]
        time.sleep(.01)
    raise AssertionError('owned operation did not settle')


def test_owner_reviews_real_diff_then_commits_exact_tree_once_without_a_second_native_turn(tmp_path):
    app, tasks, sessions, origin = owned_application(tmp_path)
    try:
        task = app.start_task(origin, repository='fixture-repo', requirement='work', executor='codex_exec')
        native = settle(app)
        assert native['status'] == 'validated_diff'
        review = app.publication.prepare('role-a', task['id'], action='commit',
            message='明确审阅的提交', author_name='Fixture Owner', author_email='fixture@example.invalid')
        assert 'work' in review['diff'] and review['head'] == native['validatedHead']
        accepted = app.publication.approve('role-a', review['id'], request_id='owner-first')
        repeated = app.publication.approve('role-a', review['id'], request_id='owner-first')
        assert repeated['id'] == accepted['id']
        result = settle(app, accepted)
        assert result['status'] == 'validated_commit', result
        worktree = next((tmp_path / 'coding/t').glob('*/*/w'))
        head = git(worktree, 'rev-parse', 'HEAD')
        assert head == result['commitSha'] == review['commitSha']
        assert git(worktree, 'show', '-s', '--format=%P', head) == native['baseHead']
        assert git(worktree, 'show', '-s', '--format=%s', head) == '明确审阅的提交'
        assert git(worktree, 'status', '--porcelain') == ''
        assert app.ledger.attempt(task['attemptId'])['baseHead'] == native['baseHead']
        assert app.ledger.attempt(task['attemptId'])['expectedHead'] == head
        assert len(tasks.list(origin, kind='coding_codex')) == 1
        messages = sessions.list_messages(origin.session_key)
        assert len(messages) == 2
        assert messages[-1].metadata['internalEvent'] == 'coding_commit'
        assert messages[-1].runId is None
    finally:
        asyncio.run(app.close())
        tasks.close()


def test_changed_snapshot_and_expired_original_budget_never_commit_or_silently_restart_cli(tmp_path, monkeypatch):
    from backend.app.coding_tasks import evidence
    from backend.app.coding_tasks.errors import CodingDeadline, CodingFailure
    app, tasks, sessions, origin = owned_application(tmp_path)
    try:
        task = app.start_task(origin, repository='fixture-repo', requirement='work', executor='codex_exec')
        original = settle(app)
        worktree = next((tmp_path / 'coding/t').glob('*/*/w'))
        source = worktree / 'fixture.txt'
        source.write_text('changed after validation', encoding='utf-8')
        with pytest.raises(CodingFailure, match='snapshot_changed'):
            app.publication.prepare('role-a', task['id'], action='commit', message='Must not commit',
                author_name='Owner', author_email='owner@example.invalid')
        source.write_text('work', encoding='utf-8')
        reviewed = app.publication.prepare('role-a', task['id'], action='commit', message='A concrete reviewed tree',
            author_name='Owner', author_email='owner@example.invalid')
        source.write_text('changed after explicit review', encoding='utf-8')
        rejected = settle(app, app.publication.approve('role-a', reviewed['id'], request_id='stale-reviewed-tree'))
        assert rejected['status'] == 'failed' and not rejected.get('commitSha')
        assert 'host_attestation_snapshot_changed' in rejected['blockers']
        assert git(worktree, 'rev-parse', 'HEAD') == original['baseHead']
        source.write_text('work', encoding='utf-8')
        # Advance only the evidence clock, never the Runtime or fixture waits.
        real_clock = evidence.time
        monkeypatch.setattr(evidence, 'time', SimpleNamespace(time=lambda: real_clock.time() + 700,
            monotonic=lambda: real_clock.monotonic() + 700))
        assert not app.publication.task_summary(app.ledger.attempt(task['attemptId']))['commitAllowed']
        with pytest.raises(CodingDeadline):
            app.publication.prepare('role-a', task['id'], action='commit', message='Expired',
                author_name='Owner', author_email='owner@example.invalid')
        assert len(tasks.list(origin, kind='coding_commit')) == 1
        assert len(tasks.list(origin, kind='coding_codex')) == 1
        assert git(worktree, 'rev-parse', 'HEAD') == original['baseHead']
        monkeypatch.setattr(evidence, 'time', real_clock)
        continued = app.continue_task(task['id'], TaskOrigin('role-a', 'role:role-a', 'explicit-new-parent', 'explicit-new-call'),
            requirement='work', executor='codex_exec')
        after = settle(app)
        assert after['status'] == 'validated_diff' and after['attemptId'] == continued['attemptId']
        assert after['nativeSession'] == original['nativeSession'] and after['baseHead'] == original['baseHead']
        assert len(tasks.list(origin, kind='coding_codex')) == 2
    finally:
        asyncio.run(app.close())
        tasks.close()


def test_two_distinct_concurrent_approvals_consume_one_exact_review_and_commit(tmp_path):
    app, tasks, sessions, origin = owned_application(tmp_path)
    try:
        task = app.start_task(origin, repository='fixture-repo', requirement='work', executor='codex_exec')
        settle(app)
        candidate = app.publication.prepare('role-a', task['id'], action='commit', message='One concurrent winner',
            author_name='Owner', author_email='owner@example.invalid')
        barrier = threading.Barrier(2)
        def approve(request):
            barrier.wait(timeout=5)
            try:
                return app.publication.approve('role-a', candidate['id'], request_id=request), request
            except ValueError:
                return None, request
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(approve, ('owner-request-a', 'owner-request-b')))
        winners = [(result, request) for result, request in outcomes if result is not None]
        assert len(winners) == 1
        operation, request = winners[0]
        result = settle(app, operation)
        assert result['status'] == 'validated_commit' and result['commitSha'] == candidate['commitSha']
        assert app.publication.approve('role-a', candidate['id'], request_id=request)['id'] == operation['id']
        assert len(tasks.list(origin, kind='coding_commit')) == 1
    finally:
        asyncio.run(app.close())
        tasks.close()


def test_owner_http_reports_public_credential_state_and_rejects_nonexplicit_approval(tmp_path):
    from backend.app.coding_tasks.api import coding_router
    app, tasks, sessions, origin = owned_application(tmp_path)
    http = FastAPI()
    http.include_router(coding_router(lambda: app, lambda: SimpleNamespace(get=lambda role_id: object() if role_id == 'role-a' else None)))
    try:
        with TestClient(http) as client:
            response = client.get('/api/coding/publication')
            assert response.status_code == 200
            assert response.json() == {'revision': 1, 'configured': False, 'cleanupPending': False}
            rejected = client.post('/api/coding/roles/role-a/reviews/missing/approve',
                json={'requestId': 'owner-id', 'approved': False})
            assert rejected.status_code == 422
            assert client.post('/api/coding/roles/role-a/tasks/missing/reviews',
                json={'action': 'publish', 'title': 'Title', 'body': '', 'token': 'forged'}).status_code == 422
            assert not any(tasks.list(origin, kind=kind) for kind in ('coding_codex', 'coding_herdr', 'coding_commit', 'coding_publish'))
            assert client.post('/api/coding/roles/role-a/reviews/missing/approve',
                json={'requestId': 'owner-id', 'approved': 1}).status_code == 422
    finally:
        asyncio.run(app.close())
        tasks.close()


@pytest.mark.parametrize('executor', ['codex_exec', 'herdr'])
def test_explicit_continue_after_host_commit_gets_a_fresh_budget_in_the_same_native_session(tmp_path, executor):
    app, tasks, sessions, origin = owned_application(tmp_path, executor)
    try:
        task = app.start_task(origin, repository='fixture-repo', requirement='work', executor=executor)
        before = settle(app)
        review = app.publication.prepare('role-a', task['id'], action='commit', message='Host commit',
            author_name='Owner', author_email='owner@example.invalid')
        committed = settle(app, app.publication.approve('role-a', review['id'], request_id='host-commit'))
        assert committed['status'] == 'validated_commit'
        continued = app.continue_task(task['id'], TaskOrigin('role-a', 'role:role-a', 'next-parent', 'next-call'),
            requirement='continue work', executor=executor)
        after = settle(app)
        assert after['attemptId'] == continued['attemptId'] != before['attemptId']
        assert after['status'] == 'validated_diff', after
        assert after['nativeSession'] == before['nativeSession']
        assert after['baseHead'] == before['baseHead']
        assert after['startHead'] == committed['commitSha']
        assert after['validationEvidence']['attemptId'] == continued['attemptId']
        assert after['originalMonotonicDeadline'] > before['originalMonotonicDeadline']
        assert app.publication.inspect('role-a', committed['id'])['deliveryStatus'] == 'sent'
        assert len(tasks.list(origin, kind=app._runtime_kind(after))) == 2
    finally:
        asyncio.run(app.close())
        tasks.close()


def test_native_business_result_cannot_be_downgraded_by_a_terminal_query_race(tmp_path, monkeypatch):
    app, tasks, sessions, origin = owned_application(tmp_path)
    release = threading.Event()
    class ResultExecutor:
        def __init__(self, record, ledger): self.record, self.ledger = record, ledger
        def run(self, execution):
            assert release.wait(timeout=5)
            self.ledger.update_attempt(self.record['attemptId'], status='validated_diff', cleanupState='complete',
                nativeSession='fixture-native', summary='business committed before runtime terminal')
            return TaskOutcome('succeeded', 0, 'bounded copied result')
        def cancel(self): release.set()
    app.executor_factory = ResultExecutor
    query = tasks.query
    observed = []
    def query_after_business_commit(identifier, source, **kwargs):
        if identifier.startswith('coding_codex_') and not observed:
            observed.append(identifier)
            release.set()
            return query(identifier, source, wait_seconds=5)
        return query(identifier, source, **kwargs)
    monkeypatch.setattr(tasks, 'query', query_after_business_commit)
    try:
        accepted = app.start_task(origin, repository='fixture-repo', requirement='copy race only', executor='codex_exec')
        asyncio.run(app.tick())
        result = app.inspect(accepted['id'], origin, executor='codex_exec')
        assert result['status'] == 'validated_diff', result
        assert result['runtimeStatus'] == 'succeeded' and not result['runtimeAckDirty']
        assert len(tasks.list(origin, kind='coding_codex')) == 1
    finally:
        release.set()
        asyncio.run(app.close())
        tasks.close()


@pytest.mark.parametrize('fault', ['approval-transaction', 'submit-return', 'runtime-id-write', 'copy-before-ack', 'ack-dirty-clear', 'final-business-write'])
def test_commit_sql_handover_faults_recover_one_approval_and_one_runtime_without_replay(tmp_path, monkeypatch, fault):
    app, tasks, sessions, origin = owned_application(tmp_path)
    fired = []
    try:
        task = app.start_task(origin, repository='fixture-repo', requirement='work', executor='codex_exec')
        settle(app)
        review = app.publication.prepare('role-a', task['id'], action='commit', message='Only approved commit',
            author_name='Owner', author_email='owner@example.invalid')
        with sqlite3.connect(sessions.database_path) as db:
            if fault == 'approval-transaction':
                db.execute("""CREATE TRIGGER injected_fault BEFORE INSERT ON coding_owner_operations
                    BEGIN SELECT RAISE(ABORT,'approval insert failed'); END""")
            elif fault == 'runtime-id-write':
                db.execute("""CREATE TRIGGER injected_fault BEFORE UPDATE ON coding_owner_operations
                    WHEN json_extract(NEW.body,'$.runtimeTaskId') IS NOT NULL AND json_extract(OLD.body,'$.runtimeTaskId') IS NULL
                    BEGIN SELECT RAISE(ABORT,'runtime identity write failed'); END""")
            elif fault == 'copy-before-ack':
                db.execute("""CREATE TRIGGER injected_fault BEFORE UPDATE ON coding_owner_operations
                    WHEN json_extract(NEW.body,'$.runtimeAckDirty')=1 AND json_extract(OLD.body,'$.runtimeAckDirty')=0
                    BEGIN SELECT RAISE(ABORT,'durable result copy failed'); END""")
            elif fault == 'final-business-write':
                db.execute("""CREATE TRIGGER injected_fault BEFORE UPDATE ON coding_owner_operations
                    WHEN json_extract(NEW.body,'$.status')='validated_commit'
                    BEGIN SELECT RAISE(ABORT,'final business write failed after real commit'); END""")
        original_submit, original_ack = tasks.submit, tasks.acknowledge_result
        if fault == 'submit-return':
            def lost_return(*args, **kwargs):
                identifier = original_submit(*args, **kwargs)
                if not fired:
                    fired.append(identifier)
                    raise OSError('owner runtime accepted but return lost')
                return identifier
            monkeypatch.setattr(tasks, 'submit', lost_return)
        if fault == 'ack-dirty-clear':
            tasks.retention_seconds = 0
            def ack_then_sql_failure(identifier, source):
                result = original_ack(identifier, source)
                if not fired:
                    fired.append(identifier)
                    with sqlite3.connect(sessions.database_path) as db:
                        db.execute("""CREATE TRIGGER injected_fault BEFORE UPDATE ON coding_owner_operations
                            WHEN json_extract(NEW.body,'$.runtimeAckDirty')=0 AND json_extract(OLD.body,'$.runtimeAckDirty')=1
                            BEGIN SELECT RAISE(ABORT,'dirty clear failed after ACK'); END""")
                return result
            monkeypatch.setattr(tasks, 'acknowledge_result', ack_then_sql_failure)
        if fault in {'approval-transaction', 'runtime-id-write'}:
            with pytest.raises(sqlite3.IntegrityError):
                app.publication.approve('role-a', review['id'], request_id='same-exact-owner-request')
            if fault == 'approval-transaction':
                assert not tasks.list(origin, kind='coding_commit')
                with sqlite3.connect(sessions.database_path) as db:
                    assert db.execute('SELECT consumed_by FROM coding_reviews WHERE id=?', (review['id'],)).fetchone()[0] is None
            with sqlite3.connect(sessions.database_path) as db:
                db.execute('DROP TRIGGER injected_fault')
        operation = app.publication.approve('role-a', review['id'], request_id='same-exact-owner-request')
        runtime = tasks.find_submission(origin, kind='coding_commit', submission_key=operation['id'])
        assert runtime is not None
        tasks.query(runtime, origin, wait_seconds=30)
        if fault == 'final-business-write':
            pending = settle(app, operation)
            assert pending['status'] == 'unknown' and pending['cleanupState'] == 'complete'
            with sqlite3.connect(sessions.database_path) as db:
                db.execute('DROP TRIGGER injected_fault')
            app.publication.observe('role-a', operation['id'])
        if fault in {'copy-before-ack', 'ack-dirty-clear'}:
            asyncio.run(app.tick())
            pending = app.publication._record(operation['id'])
            assert not pending.get('runtimeResult') if fault == 'copy-before-ack' else pending['runtimeAckDirty']
            if fault == 'copy-before-ack':
                assert tasks.query(runtime, origin).status == 'succeeded', 'un-copied result must remain retained'
            with sqlite3.connect(sessions.database_path) as db:
                db.execute('DROP TRIGGER injected_fault')
        current = settle(app, operation)
        assert current['status'] == 'validated_commit' and current['commitSha'] == review['commitSha']
        assert current['cleanupState'] == 'complete' and not current['runtimeAckDirty']
        assert app.publication.approve('role-a', review['id'], request_id='same-exact-owner-request')['id'] == operation['id']
        assert len([message for message in sessions.list_messages(origin.session_key)
            if message.metadata.get('internalEvent') == 'coding_commit']) == 1
        assert git(next((tmp_path / 'coding/t').glob('*/*/w')), 'rev-list', '--count', review['baseHead'] + '..HEAD') == '1'
    finally:
        with sqlite3.connect(sessions.database_path) as db:
            db.execute('DROP TRIGGER IF EXISTS injected_fault')
        asyncio.run(app.close())
        tasks.close()


@contextmanager
def platform(tmp_path, bare):
    database = tmp_path / 'platform.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE pulls(number INTEGER PRIMARY KEY,body TEXT NOT NULL)')
    state = {'posts': 0, 'lost': False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, value, code=200):
            content = json.dumps(value).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(content)
        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path == '/repos/meido-fixture/publication':
                return self.respond({'id': 7, 'full_name': 'meido-fixture/publication',
                    'owner': {'login': 'meido-fixture'}, 'clone_url': 'https://github.com/meido-fixture/publication.git'})
            prefix = '/repos/meido-fixture/publication/git/ref/heads/'
            if path.startswith(prefix):
                branch = path[len(prefix):]
                try:
                    sha = git(bare, 'rev-parse', '--verify', 'refs/heads/' + branch)
                except ValueError:
                    return self.respond({'message': 'not found'}, 404)
                return self.respond({'ref': 'refs/heads/' + branch, 'object': {'type': 'commit', 'sha': sha}})
            with sqlite3.connect(database) as db:
                values = [json.loads(row[0]) for row in db.execute('SELECT body FROM pulls ORDER BY number')]
            if path == '/repos/meido-fixture/publication/pulls':
                before_query = state.pop('before_pr_query', None)
                if before_query is not None:
                    before_query()
                if state.get('block_pulls'):
                    return self.respond({'message': 'fixture bounded read temporarily unavailable'}, 503)
                query = parse_qs(urlsplit(self.path).query)
                head = query.get('head', [''])[0].split(':', 1)[-1]
                return self.respond([value for value in values if value['head']['ref'] == head])
            if path.startswith('/repos/meido-fixture/publication/pulls/'):
                number = int(path.rsplit('/', 1)[1])
                return self.respond(next(value for value in values if value['number'] == number))
            return self.respond({}, 404)
        def do_POST(self):
            assert self.path == '/repos/meido-fixture/publication/pulls'
            assert self.headers['Authorization'] == 'Bearer fixture-broker-secret'
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['posts'] += 1
            sha = git(bare, 'rev-parse', 'refs/heads/' + value['head'])
            response = {'number': 1, 'html_url': 'https://github.com/meido-fixture/publication/pull/1',
                'state': 'open', 'draft': value['draft'], 'title': value['title'], 'body': value['body'],
                'head': {'ref': value['head'], 'sha': sha, 'repo': {'id': 7, 'full_name': 'meido-fixture/publication'}},
                'base': {'ref': value['base'], 'sha': git(bare, 'rev-parse', 'refs/heads/' + value['base']),
                         'repo': {'id': 7, 'full_name': 'meido-fixture/publication'}}}
            if state.get('mutate'):
                state['mutate'](response)
            with sqlite3.connect(database) as db:
                db.execute('INSERT INTO pulls VALUES(1,?)', (json.dumps(response),))
            if state['lost']:
                self.close_connection = True
                return
            if state.get('hold') is not None:
                assert state['hold'].wait(timeout=15)
            self.respond(response, 201)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_address[1]), state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


@pytest.mark.parametrize('lost_receipt', [False, True], ids=['received', 'lost-restart-observe'])
def test_reviewed_publication_pushes_one_exact_sha_and_verifies_a_real_local_http_draft(tmp_path, monkeypatch, lost_receipt):
    from backend.app.coding_tasks.github_publication import GithubPlatform, GitTransport
    app, tasks, sessions, origin = owned_application(tmp_path)
    bare = tmp_path / 'bare.git'
    bare.mkdir()
    git(bare, 'init', '--bare', '-b', 'main')
    git(tmp_path / 'repo', 'push', str(bare), 'HEAD:refs/heads/main')
    class Credentials:
        token = ''
        def get(self, key): return self.token
        def set(self, key, value): self.token = value
    try:
        task = app.start_task(origin, repository='fixture-repo', requirement='work', executor='codex_exec')
        settle(app)
        commit_review = app.publication.prepare('role-a', task['id'], action='commit',
            message='Ready', author_name='Fixture Owner', author_email='fixture@example.invalid')
        committed = settle(app, app.publication.approve('role-a', commit_review['id'], request_id='commit'))
        assert committed['status'] == 'validated_commit'
        app.publication.credentials = Credentials()
        app.publication.set_token('fixture-broker-secret')
        with platform(tmp_path, bare) as (endpoint, state):
            state['lost'] = lost_receipt
            if not lost_receipt:
                state['hold'] = threading.Event()
                query = tasks.query
                released = []
                def query_after_terminal(task_id, source, **kwargs):
                    if task_id.startswith('coding_publish_') and state['posts'] and not released:
                        released.append(True)
                        state['hold'].set()
                        return query(task_id, source, wait_seconds=10)
                    return query(task_id, source, **kwargs)
                monkeypatch.setattr(tasks, 'query', query_after_terminal)
            app.publication.platform_factory = lambda token, remote: GithubPlatform(token, remote, base_url=endpoint)
            app.publication.transport_factory = lambda *args: GitTransport(*args, destination=str(bare))
            review = app.publication.prepare('role-a', task['id'], action='publish', title='Exact change', body='Reviewed body\n')
            accepted = app.publication.approve('role-a', review['id'], request_id='publication')
            result = settle(app, accepted)
            if lost_receipt:
                assert result['status'] == 'unknown', result
                assert state['posts'] == 1
                previous = app
                asyncio.run(app.close())
                tasks.close()
                tasks = TaskRuntime(tmp_path / 'runtime')
                app = CodingApplication(tmp_path / 'coding', tasks, sessions,
                    role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
                    config=previous.config, publication_credentials=previous.publication.credentials,
                    executor_factory=lambda *_: (_ for _ in ()).throw(AssertionError('restart must not execute native CLI')),
                    publication_platform_factory=lambda token, remote: GithubPlatform(token, remote, base_url=endpoint),
                    publication_transport_factory=lambda *_: (_ for _ in ()).throw(AssertionError('restart must not push')))
                app.publication.observe('role-a', accepted['id'])
                asyncio.run(app.tick())
                result = app.publication.inspect('role-a', accepted['id'])
            assert result['status'] == 'delivered_draft', result
            assert state['posts'] == 1
            assert git(bare, 'rev-parse', 'refs/heads/' + review['publishBranch']) == committed['commitSha']
            assert git(bare, 'rev-parse', 'refs/heads/main') == commit_review['baseHead']
            assert result['prUrl'] == 'https://github.com/meido-fixture/publication/pull/1'
            assert app.publication.approve('role-a', review['id'], request_id='publication')['id'] == accepted['id']
            asyncio.run(app.tick())
            assert state['posts'] == 1
            messages = sessions.list_messages(origin.session_key)
            assert len(messages) == 3
            assert messages[-1].metadata['internalEvent'] == 'coding_publication'
            if not lost_receipt:
                assert result['prUrl'] in messages[-1].content
    finally:
        asyncio.run(app.close())
        tasks.close()


@pytest.mark.parametrize('changed', ['base-head', 'head-repository', 'draft', 'closed', 'url', 'title'])
def test_local_platform_response_must_match_every_approved_delivery_fact(tmp_path, changed):
    from backend.app.coding_tasks.github_publication import GithubPlatform
    root = repository(tmp_path)
    bare = tmp_path / 'bare.git'
    bare.mkdir()
    git(bare, 'init', '--bare', '-b', 'main')
    git(root, 'push', str(bare), 'HEAD:refs/heads/main', 'HEAD:refs/heads/codex/fixture')
    sha = git(root, 'rev-parse', 'HEAD')
    candidate = {'title': 'Reviewed title', 'body': 'Reviewed body', 'publishBranch': 'codex/fixture',
        'baseRef': 'main', 'baseHead': sha, 'commitSha': sha}
    def mutate(value):
        if changed == 'base-head': value['base']['sha'] = '0' * 40
        elif changed == 'head-repository': value['head']['repo']['id'] = 999
        elif changed == 'draft': value['draft'] = False
        elif changed == 'closed': value['state'] = 'closed'
        elif changed == 'url': value['html_url'] = 'https://github.com/other/repo/pull/1'
        elif changed == 'title': value['title'] = 'Changed after approval'
    with platform(tmp_path, bare) as (endpoint, state):
        state['mutate'] = mutate
        client = GithubPlatform('fixture-broker-secret', {'url': 'https://github.com/meido-fixture/publication.git',
            'owner': 'meido-fixture', 'repository': 'publication'}, base_url=endpoint)
        try:
            repository_value = client.repository()
            value = client.create(candidate)
            with pytest.raises(ValueError):
                client.verify(value, candidate, repository_value)
            assert state['posts'] == 1
        finally:
            client.close()


def test_owner_can_review_only_remaining_pr_after_confirmed_push_without_repeating_push(tmp_path):
    from backend.app.coding_tasks.github_publication import GithubPlatform, GitTransport
    app, tasks, sessions, origin = owned_application(tmp_path)
    bare = tmp_path / 'bare.git'
    bare.mkdir()
    git(bare, 'init', '--bare', '-b', 'main')
    git(tmp_path / 'repo', 'push', str(bare), 'HEAD:refs/heads/main')
    class Credentials:
        def get(self, key): return 'fixture-broker-secret'
        def set(self, key, value): pass
    pushes = []
    class CountingTransport(GitTransport):
        def push(self, worktree):
            pushes.append(self.candidate['commitSha'])
            super().push(worktree)
    try:
        task = app.start_task(origin, repository='fixture-repo', requirement='work', executor='codex_exec')
        settle(app)
        review = app.publication.prepare('role-a', task['id'], action='commit', message='Ready',
            author_name='Owner', author_email='owner@example.invalid')
        committed = settle(app, app.publication.approve('role-a', review['id'], request_id='commit'))
        app.publication.credentials = Credentials()
        app.publication.set_token('fixture-broker-secret')
        with platform(tmp_path, bare) as (endpoint, state):
            state['block_pulls'] = True
            app.publication.platform_factory = lambda token, remote: GithubPlatform(token, remote, base_url=endpoint)
            app.publication.transport_factory = lambda *args: CountingTransport(*args, destination=str(bare))
            candidate = app.publication.prepare('role-a', task['id'], action='publish', title='Original', body='Original body')
            operation = settle(app, app.publication.approve('role-a', candidate['id'], request_id='first-publish'))
            assert operation['status'] == 'unknown' and operation['pushConfirmed'] and not operation.get('prDispatched')
            assert state['posts'] == 0 and len(pushes) == 1
            state['block_pulls'] = False
            observed = app.publication.observe('role-a', operation['id'])
            assert observed['status'] == 'push_confirmed'
            remaining = app.publication.prepare('role-a', task['id'], action='publish', title='Fresh reviewed PR',
                body='Explicitly approved remaining action', previous_operation_id=operation['id'])
            assert remaining['publishBranch'] == candidate['publishBranch'] and remaining['pushRequired'] is False
            result = settle(app, app.publication.approve('role-a', remaining['id'], request_id='explicit-remaining-only'))
            assert result['status'] == 'delivered_draft' and result['commitSha'] == committed['commitSha']
            assert len(pushes) == 1 and state['posts'] == 1
            assert app.publication.inspect('role-a', operation['id'])['status'] == 'superseded'
    finally:
        asyncio.run(app.close())
        tasks.close()


def test_remote_base_advancing_after_push_blocks_pr_before_dispatch(tmp_path):
    from backend.app.coding_tasks.github_publication import GithubPlatform, GitTransport
    app, tasks, sessions, origin = owned_application(tmp_path)
    bare = tmp_path / 'bare.git'
    bare.mkdir()
    git(bare, 'init', '--bare', '-b', 'main')
    git(tmp_path / 'repo', 'push', str(bare), 'HEAD:refs/heads/main')
    class Credentials:
        def get(self, key): return 'fixture-broker-secret'
        def set(self, key, value): pass
    try:
        task = app.start_task(origin, repository='fixture-repo', requirement='work', executor='codex_exec')
        settle(app)
        review = app.publication.prepare('role-a', task['id'], action='commit', message='Ready',
            author_name='Owner', author_email='owner@example.invalid')
        settle(app, app.publication.approve('role-a', review['id'], request_id='commit'))
        app.publication.credentials = Credentials()
        app.publication.set_token('fixture-broker-secret')
        with platform(tmp_path, bare) as (endpoint, state):
            app.publication.platform_factory = lambda token, remote: GithubPlatform(token, remote, base_url=endpoint)
            app.publication.transport_factory = lambda *args: GitTransport(*args, destination=str(bare))
            candidate = app.publication.prepare('role-a', task['id'], action='publish', title='Exact old base', body='Approved')
            def advance_base():
                git(tmp_path / 'repo', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--allow-empty', '-m', 'concurrent base advance')
                git(tmp_path / 'repo', 'push', str(bare), 'HEAD:refs/heads/main')
            state['before_pr_query'] = advance_base
            result = settle(app, app.publication.approve('role-a', candidate['id'], request_id='publish'))
            assert result['status'] == 'unknown' and result['pushConfirmed']
            assert not result.get('prDispatched') and state['posts'] == 0
            assert git(bare, 'rev-parse', 'refs/heads/main') != candidate['baseHead']
    finally:
        asyncio.run(app.close())
        tasks.close()
