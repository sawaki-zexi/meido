"""The private broker sends the approved Git object over real smart HTTP."""
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shutil
import threading
import time
from urllib.parse import urlsplit

import pytest

from backend.app.coding_tasks.git_repository import git, git_environment
from backend.app.coding_tasks.github_publication import GitTransport
from backend.app.coding_tasks.owned_process import OwnedProcess
from backend.app.coding_tasks.publication import OwnedGit

pytestmark = pytest.mark.usefixtures('local_platform_only')


@contextmanager
def smart_http(root, authorization):
    requests, processes, errors = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self): self.dispatch()
        def do_POST(self): self.dispatch()
        def dispatch(self):
            requests.append((self.command, self.headers.get('Authorization')))
            if self.headers.get('Authorization') != authorization:
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            process = None
            try:
                path = urlsplit(self.path)
                size = int(self.headers.get('Content-Length', '0'))
                assert size < 1024 * 1024 and not self.headers.get('Transfer-Encoding')
                body = self.rfile.read(size)
                env = git_environment() | {'GIT_PROJECT_ROOT': str(root), 'GIT_HTTP_EXPORT_ALL': '1',
                    'PATH_INFO': path.path, 'QUERY_STRING': path.query, 'REQUEST_METHOD': self.command,
                    'CONTENT_TYPE': self.headers.get('Content-Type', ''), 'CONTENT_LENGTH': str(size),
                    'REMOTE_USER': 'fixture', 'REMOTE_ADDR': '127.0.0.1', 'SERVER_PROTOCOL': 'HTTP/1.1'}
                process = OwnedProcess([shutil.which('git'), 'http-backend'], root, env, output_bytes=1024 * 1024)
                processes.append(process)
                result = process.communicate(timeout=10, input=body)
                assert result.returncode == 0
                headers, payload = result.stdout.split(b'\r\n\r\n', 1)
                parsed = [line.decode().split(':', 1) for line in headers.split(b'\r\n')]
                self.send_response(next((int(value.strip().split()[0]) for key, value in parsed if key.lower() == 'status'), 200))
                for key, value in parsed:
                    if key.lower() != 'status': self.send_header(key, value.strip())
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except BaseException as error:
                errors.append(error)
                self.close_connection = True
            finally:
                if process is not None: process.close()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    worker = threading.Thread(target=server.serve_forever, name='fixture-git-smart-http')
    worker.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port) + '/remote.git', requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive() and not errors, errors
        for process in processes:
            process.close()
            assert process.cleanup_state == 'complete'


def test_private_git_smart_http_sends_basic_and_only_the_approved_sha_branch(tmp_path):
    worktree = tmp_path / 'source'
    worktree.mkdir()
    git(worktree, 'init', '-b', 'main')
    git(worktree, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'base')
    bare = tmp_path / 'remote.git'
    bare.mkdir()
    git(bare, 'init', '--bare', '-b', 'main')
    git(bare, 'config', 'http.receivepack', 'true')
    git(worktree, 'push', str(bare), 'HEAD:refs/heads/main')
    base = git(worktree, 'rev-parse', 'HEAD')
    (worktree / 'approved.txt').write_text('approved object', encoding='utf-8')
    git(worktree, 'add', 'approved.txt')
    git(worktree, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'approved')
    # Source configuration is never inherited by the private broker stage.
    git(worktree, 'config', 'http.extraheader', 'Authorization: source-poison')
    token = 'fixture-private-broker-token'
    header = 'Basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode()
    candidate = {'branch': 'main', 'commitSha': git(worktree, 'rev-parse', 'HEAD'),
        'tree': git(worktree, 'rev-parse', 'HEAD^{tree}'), 'publishBranch': 'codex/meido-approved',
        'remote': {'url': 'https://github.com/fixture/repo.git'}}
    deadline = time.monotonic() + 20
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0: raise TimeoutError('fixture Git deadline')
        return value
    owner = OwnedGit(tmp_path, 'smart-http-fixture', lambda *_: None, remaining)
    stage = tmp_path / 'private-stage'
    try:
        with smart_http(tmp_path, header) as (endpoint, requests):
            GitTransport(stage, owner, candidate, token, destination=endpoint).push(worktree)
        assert {method for method, _ in requests} == {'GET', 'POST'}
        assert all(value == header for _, value in requests)
        assert git(bare, 'rev-parse', 'refs/heads/codex/meido-approved') == candidate['commitSha']
        assert git(bare, 'rev-parse', 'refs/heads/main') == base
        assert git(bare, 'for-each-ref', '--format=%(refname)').splitlines() == ['refs/heads/codex/meido-approved', 'refs/heads/main']
        assert token not in git(worktree, 'config', '--local', '--list')
        assert token not in git(stage / 'objects.git', 'config', '--local', '--list')
        assert 'source-poison' not in git(stage / 'objects.git', 'config', '--local', '--list')
    finally:
        owner.close()
        if owner.process is not None: assert owner.process.cleanup_state == 'complete'
