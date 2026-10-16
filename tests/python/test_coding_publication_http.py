"""Real streaming HTTP deadlines/revocation and owned connection shutdown."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from backend.app.coding_tasks.github_publication import GithubPlatform
from backend.app.coding_tasks import publication

pytestmark = pytest.mark.usefixtures('local_platform_only')


@contextmanager
def response_peer(mode, ready=None):
    release, disconnected = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            if mode == 'headers-stall':
                self.connection.settimeout(2)
                try:
                    if self.connection.recv(1) == b'': disconnected.set()
                except (ConnectionResetError, OSError):
                    disconnected.set()
                return
            body = json.dumps({'fixture': 'bounded-response'}).encode()
            self.send_response(200)
            self.send_header('Content-Length', '2' if mode == 'late-stall' else str(len(body)))
            self.end_headers()
            try:
                if mode == 'complete':
                    self.wfile.write(body)
                elif mode == 'drip':
                    for value in body:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        if release.wait(.025): break
                else:
                    # Almost all the budget is consumed before the first byte.
                    time.sleep(.16)
                    self.wfile.write(b'{')
                    self.wfile.flush()
                    if ready is not None: ready.set()
                    self.connection.settimeout(2)
                    if self.connection.recv(1) == b'': disconnected.set()
                    release.wait(2)
            except (BrokenPipeError, ConnectionResetError, OSError):
                disconnected.set()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, name='fixture-deadline-server')
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), disconnected
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize('mode', ['headers-stall', 'drip', 'late-stall'])
@pytest.mark.parametrize('cause', ['deadline', 'revocation'])
def test_streaming_read_obeys_same_absolute_deadline_and_joins_connection_owner(mode, cause):
    with response_peer(mode) as (endpoint, disconnected):
        platform = GithubPlatform('synthetic-only', {'owner': 'fixture', 'repository': 'repo'}, base_url=endpoint)
        started = time.monotonic()
        # The new header-wait case leaves room for the first actual TCP
        # connection on a cold library; the original four body cases retain
        # their 0.2-second deadlines and 0.18-second revocation unchanged.
        budget = 1 if mode == 'headers-stall' else .2
        deadline = started + (budget if cause == 'deadline' else 1)
        checks = []
        def remaining():
            now = time.monotonic()
            checks.append(now - started)
            if cause == 'revocation' and now - started >= .18:
                raise PermissionError('original fixture authority revoked')
            if now >= deadline:
                raise TimeoutError('same original absolute deadline')
            return deadline - now
        platform.check = remaining
        try:
            with pytest.raises((TimeoutError, httpx.TimeoutException) if cause == 'deadline' else PermissionError):
                platform._request('GET', '/synthetic')
            elapsed = time.monotonic() - started
            assert elapsed < (budget + .1 if cause == 'deadline' else .30), 'a late stalled read must not receive a fresh initial timeout'
            assert len(checks) > 1
            assert not [thread for thread in threading.enumerate() if thread.name.startswith('Meido.Coding.HTTP.')]
            if mode in {'headers-stall', 'late-stall'}:
                assert disconnected.wait(.1), 'cancelled request left its actual connection open'
        finally:
            platform.close()
            assert platform.client.is_closed


@pytest.mark.parametrize('cause', ['deadline', 'revocation'])
def test_parse_cannot_return_a_result_after_the_same_deadline_or_authority_ends(monkeypatch, cause):
    from backend.app.coding_tasks import owned_http
    ended = threading.Event()
    original_loads = json.loads
    def parse(body):
        value = original_loads(body)
        ended.set()
        return value
    def remaining():
        if ended.is_set():
            if cause == 'deadline': raise TimeoutError('original deadline ended during parse')
            raise PermissionError('original authorization revoked during parse')
        return 1
    with response_peer('complete') as (endpoint, _):
        platform = GithubPlatform('synthetic-only', {'owner': 'fixture', 'repository': 'repo'}, base_url=endpoint)
        platform.check = remaining
        monkeypatch.setattr(owned_http.json, 'loads', parse)
        try:
            with pytest.raises(TimeoutError if cause == 'deadline' else PermissionError):
                platform._request('GET', '/synthetic')
            assert ended.is_set()
        finally:
            platform.close()
            assert platform.client.is_closed


def test_owner_close_interrupts_a_pending_read_and_waits_for_actual_transport_exit():
    ready, done = threading.Event(), threading.Event()
    errors = []
    with response_peer('late-stall', ready) as (endpoint, disconnected):
        platform = GithubPlatform('synthetic-only', {'owner': 'fixture', 'repository': 'repo'}, base_url=endpoint)
        def request():
            try:
                platform._request('GET', '/synthetic')
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()
        caller = threading.Thread(target=request, name='fixture-coding-caller')
        caller.start()
        try:
            assert ready.wait(2)
            started = time.monotonic()
            platform.close()
            assert time.monotonic() - started < .3
            assert done.wait(.3)
            caller.join(timeout=1)
            assert len(errors) == 1 and isinstance(errors[0], PermissionError)
            assert platform.client.is_closed and disconnected.wait(.1)
            assert not [thread for thread in threading.enumerate() if thread.name.startswith('Meido.Coding.HTTP.')]
        finally:
            platform.close()
            caller.join(timeout=3)
            assert not caller.is_alive()


def test_observe_admission_waits_only_the_original_remaining_budget(monkeypatch):
    owner_lock = threading.RLock()
    ready, release = threading.Event(), threading.Event()
    def hold():
        with owner_lock:
            ready.set()
            release.wait(1)
    holder = threading.Thread(target=hold, name='fixture-coding-held-lock')
    holder.start()
    assert ready.wait(1)
    operations = publication.PublicationOperations.__new__(publication.PublicationOperations)
    operations.application = SimpleNamespace(_owner_lock=owner_lock)
    instant = time.monotonic()
    readings = iter([instant, instant + 29.9])
    # Model time already spent before lock admission. The real lock must use
    # the 0.1 seconds left from the unchanged production 30-second deadline.
    monkeypatch.setattr(publication, 'time', SimpleNamespace(monotonic=lambda: next(readings)))
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match='readback admission ended'):
            operations.observe('role-a', 'not-admitted')
        assert time.monotonic() - started < .3
    finally:
        release.set()
        holder.join(timeout=2)
        assert not holder.is_alive()
