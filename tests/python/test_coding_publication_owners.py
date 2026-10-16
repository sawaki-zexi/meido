"""Actual Runtime ownership and SQLite failures at owner admission gates."""
import asyncio
from contextlib import contextmanager
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from backend.app.coding_tasks.github_publication import GithubPlatform
from backend.app.coding_tasks.owned_process import CleanupFailed
from backend.app.task_runtime import TaskBudget, TaskOrigin, TaskOutcome, TaskRuntime
from test_coding_publication_http import response_peer
from test_coding_publication_recovery import recovery_fixture

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_runtime_worker_owns_a_non_daemon_http_thread_and_joins_real_get(tmp_path, monkeypatch):
    threads = []
    start = threading.Thread.start
    def observe(thread):
        if thread.name.startswith('Meido.Coding.HTTP.'): threads.append(thread)
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', observe)
    with response_peer('complete') as (endpoint, _):
        class Work:
            platform = None
            worker_daemon = None
            def run(self, execution):
                self.worker_daemon = threading.current_thread().daemon
                self.platform = GithubPlatform('synthetic-only', {'owner': 'fixture', 'repository': 'repo'}, base_url=endpoint)
                try:
                    assert self.platform._request('GET', '/synthetic') == {'fixture': 'bounded-response'}
                    return TaskOutcome('succeeded', 0, 'real localhost GET')
                finally:
                    self.platform.close()
            def cancel(self):
                if self.platform is not None: self.platform.close()
        owner, runtime = Work(), TaskRuntime(tmp_path / 'runtime')
        origin = TaskOrigin('role-a', 'role:role-a', 'parent', 'call')
        try:
            identifier = runtime.submit(origin, owner, kind='coding_publish', completion=False,
                budget=TaskBudget(10, 1024, 1024))
            assert runtime.query(identifier, origin, wait_seconds=5).status == 'succeeded'
            assert owner.worker_daemon is True  # Existing Runtime policy is unchanged.
            assert threads and all(thread.daemon is False and not thread.is_alive() for thread in threads)
        finally:
            runtime.close()
            assert owner.platform is not None and owner.platform.client.is_closed
            assert not [thread for thread in threading.enumerate() if thread.name.startswith('Meido.Coding.HTTP.')]


@contextmanager
def failed_cleanup_write(tmp_path):
    app, tasks, _, _, _, _, native, operation = recovery_fixture(tmp_path)
    values, writes, clients, release = {}, [], [], threading.Event()
    def set_secret(key, value):
        writes.append(value)
        values[key] = value
    app.publication.credentials = SimpleNamespace(get=lambda key: values.get(key, ''), set=set_secret)
    app.publication.set_token('synthetic-before')
    candidate = operation['candidate'] | {'credentialRevision': app.publication.settings()['revision'],
        'remote': {'owner': 'fixture', 'repository': 'repo'}}
    # A historical synced/sent outbox must not be necessary to discover a
    # newly retained in-memory cleanup responsibility.
    app.publication._update(operation['id'], action='publish', candidate=candidate,
        status='push_confirmed', pushDispatched=True, pushConfirmed=True, platformRepositoryId=7,
        messageSynced=True, deliveryStatus='sent')
    class UnconfirmedPlatform(GithubPlatform):
        def repository(self): raise ValueError('controlled readback failure')
        def close(self):
            if not release.is_set(): raise CleanupFailed('controlled HTTP cleanup unconfirmed')
            super().close()
    def construct(token, remote):
        if clients: assert clients[-1].client.is_closed, 'new platform preceded actual old-client close'
        client = UnconfirmedPlatform(token, remote, base_url='http://127.0.0.1:1')
        clients.append(client)
        return client
    app.publication.platform_factory = construct
    with app.ledger.transaction() as db:
        db.execute("""CREATE TRIGGER fail_cleanup_write BEFORE UPDATE ON coding_owner_operations
            WHEN json_extract(NEW.body,'$.cleanupState')='cleanup_failed'
            BEGIN SELECT RAISE(ABORT,'controlled cleanup state write failed'); END""")
    try:
        with pytest.raises(sqlite3.IntegrityError): app.publication.observe('role-a', operation['id'])
        retained = app.publication._owners[operation['id']]
        assert app.publication._record(operation['id'])['cleanupState'] == 'complete'
        yield app, tasks, native, operation, retained, values, writes, clients, release
    finally:
        release.set()
        with app.ledger.transaction() as db: db.execute('DROP TRIGGER fail_cleanup_write')
        # Independently close every fixture client, including a wrongly
        # replaced owner on the old source, before application/DB shutdown.
        for client in clients:
            client.close()
            assert client.client.is_closed
        asyncio.run(app.close())
        tasks.close()


def test_retry_cannot_replace_retained_owner_when_cleanup_write_failed(tmp_path):
    with failed_cleanup_write(tmp_path) as (app, _, _, operation, owner, _, _, clients, release):
        for _ in range(2):
            with pytest.raises(ValueError, match='owned resources'):
                app.publication.observe('role-a', operation['id'])
            assert len(clients) == 1 and app.publication._owners[operation['id']] is owner
            assert not clients[0].client.is_closed
        release.set()
        asyncio.run(app.tick())
        assert clients[0].client.is_closed and not app.publication._owners
        with pytest.raises(ValueError, match='controlled readback failure'):
            app.publication.observe('role-a', operation['id'])
        assert len(clients) == 2 and clients[1].client.is_closed


def test_remaining_resolution_cannot_skip_a_retained_owner_with_stale_sql_complete(tmp_path):
    with failed_cleanup_write(tmp_path) as (app, _, native, operation, owner, _, _, _, _):
        with pytest.raises(ValueError, match='unresolved effects'):
            app.publication._ensure_available(native['id'], resolving=operation['id'])
        assert app.publication._owners[operation['id']] is owner


def test_tick_closes_retained_owner_even_when_persisted_outbox_has_no_pending_work(tmp_path):
    with failed_cleanup_write(tmp_path) as (app, _, _, operation, owner, _, _, clients, release):
        asyncio.run(app.tick())
        assert app.publication._owners[operation['id']] is owner and not clients[0].client.is_closed
        release.set()
        asyncio.run(app.tick())
        assert clients[0].client.is_closed and not app.publication._owners
        app.publication.ensure_native_admission('role-a', 'repo-a')
        assert not app.publication.role_resources_pending('role-a')


@pytest.mark.parametrize('replacement', ['synthetic-after', ''])
def test_credential_change_waits_for_retained_owner_and_does_not_write_secret_early(tmp_path, replacement):
    with failed_cleanup_write(tmp_path) as (app, _, _, operation, owner, values, writes, clients, release):
        revision = app.publication.settings()['revision']
        with pytest.raises(CleanupFailed): app.publication.set_token(replacement)
        setting = app.publication.settings()
        assert setting == {'revision': revision + 1, 'configured': False, 'cleanupPending': True}
        assert values['token'] == 'synthetic-before' and writes == ['synthetic-before']
        assert app.publication._owners[operation['id']] is owner and not clients[0].client.is_closed
        with pytest.raises(ValueError, match='owned resources'):
            app.publication.observe('role-a', operation['id'])
        assert len(clients) == 1
        release.set()
        asyncio.run(app.tick())
        assert clients[0].client.is_closed and not app.publication._owners
        result = app.publication.set_token(replacement)
        assert result['configured'] is bool(replacement) and result['cleanupPending'] is False
        assert writes == ['synthetic-before', replacement] and values['token'] == replacement
