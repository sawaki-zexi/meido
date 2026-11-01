"""Public runner CLI, including real reload and launcher-owned graceful stop."""
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import time

import httpx
import pytest


@pytest.mark.parametrize('reload', [False, True])
def test_runner_drains_open_sse_on_stdin_stop_and_reload(tmp_path, reload):
    fixture = tmp_path / 'runner_fixture.py'
    fixture.write_text('''import asyncio, os
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
app = FastAPI()
closing = asyncio.Event()
async def drain():
    await asyncio.sleep(.1)
    with Path(os.environ['MEIDO_TEST_DRAIN_LOG']).open('a') as log:
        log.write(str(os.getpid()) + "\\n")
    closing.set()
app.state.before_server_drain = drain
@app.get('/pid')
async def pid(): return {'pid':os.getpid()}
@app.get('/events')
async def events():
    async def stream():
        yield 'data: ready\\n\\n'
        await closing.wait()
        yield 'data: closed\\n\\n'
    return StreamingResponse(stream(), media_type='text/event-stream')
''', encoding='utf-8')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    log = tmp_path / 'drained.txt'
    environment = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(tmp_path), str(Path(__file__).resolve().parents[2])]),
        'MEIDO_TEST_DRAIN_LOG': str(log)}
    command = [sys.executable, '-m', 'backend.app.server', 'runner_fixture:app', '--host', '127.0.0.1',
        '--port', str(port), '--shutdown-stdin', '--log-level', 'warning']
    if reload:
        command += ['--reload', '--reload-dir', str(tmp_path)]

    def wait_pid(client, previous=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                current = client.get('/pid', timeout=.5).json()['pid']
                if current != previous:
                    return current
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            time.sleep(.05)
        raise AssertionError('runner did not start/reload: ' + (tmp_path / 'runner.log').read_text(encoding='utf-8'))

    with (tmp_path / 'runner.log').open('w', encoding='utf-8') as output:
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2], env=environment,
            stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=5, trust_env=False) as client:
                first = wait_pid(client)
                if reload:
                    with client.stream('GET', '/events') as response:
                        lines = response.iter_lines()
                        assert next(lines) == 'data: ready'
                        # Touch only the isolated fixture; the real file watcher
                        # restarts its worker while this subscription stays open.
                        time.sleep(.5)
                        with fixture.open('a', encoding='utf-8') as source:
                            source.write('\n# reload the isolated fixture\n')
                        second = wait_pid(client, first)
                        assert 'data: closed' in list(lines)
                        assert log.read_text().splitlines() == [str(first)]
                else:
                    second = first
                with client.stream('GET', '/events') as response:
                    lines = response.iter_lines()
                    assert next(lines) == 'data: ready'
                    process.stdin.write('shutdown\n')
                    process.stdin.flush()
                    assert 'data: closed' in list(lines)
                    assert process.wait(timeout=10) == 0
                assert log.read_text().splitlines() == ([str(first), str(second)] if reload else [str(first)])
        finally:
            if process.poll() is None:
                process.stdin.close()
                process.wait(timeout=15)


def test_development_launcher_check_keeps_isolated_paths_without_starting(tmp_path):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['node', str(root / 'scripts/local-runtime/start-dev.mjs'),
        '--code-root', str(tmp_path / 'isolated checkout'), '--check'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['apiPort'] not in {4288, 5288, 5289}
    assert data['webPort'] != data['apiPort']
    assert Path(data['dataDir']) == tmp_path / 'isolated checkout/.data/development'
    assert not (tmp_path / 'isolated checkout').exists()


def test_development_launcher_stops_runner_and_vite_with_exit_zero(tmp_path):
    root = Path(__file__).resolve().parents[2]
    vite = root / 'node_modules/vite/bin/vite.js'
    if not vite.exists():
        pytest.skip('npm ci is required for the real Vite launcher integration')
    code = tmp_path / 'isolated checkout'
    (code / 'backend/app').mkdir(parents=True)
    (code / 'backend/__init__.py').write_text('')
    (code / 'backend/app/__init__.py').write_text('')
    shutil.copyfile(root / 'backend/app/server.py', code / 'backend/app/server.py')
    (code / 'backend/app/main.py').write_text('''import asyncio, os
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
app = FastAPI()
closing = asyncio.Event()
async def drain():
    await asyncio.sleep(.1)
    Path(os.environ['MEIDO_DATA_DIR'], 'drained').write_text('completed')
    closing.set()
app.state.before_server_drain = drain
@app.get('/api/health')
async def health(): return {'status':'ok'}
@app.get('/events')
async def events():
    async def stream():
        yield 'data: ready\\n\\n'
        await closing.wait()
        yield 'data: closed\\n\\n'
    return StreamingResponse(stream(), media_type='text/event-stream')
''', encoding='utf-8')
    (code / 'node_modules/vite/bin').mkdir(parents=True)
    (code / 'node_modules/vite/bin/vite.js').write_text(f'import({json.dumps(vite.as_uri())});', encoding='utf-8')
    (code / 'index.html').write_text('<p>isolated launcher</p>', encoding='utf-8')
    with socket.socket() as api_sock, socket.socket() as web_sock:
        api_sock.bind(('127.0.0.1', 0))
        web_sock.bind(('127.0.0.1', 0))
        api_port, web_port = api_sock.getsockname()[1], web_sock.getsockname()[1]
    environment = {**os.environ, 'MEIDO_DEV_API_PORT': str(api_port), 'MEIDO_DEV_WEB_PORT': str(web_port),
        'MEIDO_DEV_PYTHON': sys.executable}
    with (tmp_path / 'launcher.log').open('w', encoding='utf-8') as output:
        process = subprocess.Popen(['node', str(root / 'scripts/local-runtime/start-dev.mjs'),
            '--code-root', str(code), '--shutdown-stdin'], env=environment, stdin=subprocess.PIPE,
            stdout=output, stderr=subprocess.STDOUT, text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        try:
            with httpx.Client(timeout=5, trust_env=False) as client:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        assert client.get(f'http://127.0.0.1:{api_port}/api/health', timeout=.5).status_code == 200
                        assert client.get(f'http://127.0.0.1:{web_port}', timeout=.5).status_code == 200
                        break
                    except (httpx.HTTPError, AssertionError):
                        if time.monotonic() > deadline:
                            raise AssertionError((tmp_path / 'launcher.log').read_text(encoding='utf-8'))
                        time.sleep(.05)
                with client.stream('GET', f'http://127.0.0.1:{api_port}/events') as response:
                    lines = response.iter_lines()
                    assert next(lines) == 'data: ready'
                    process.stdin.write('shutdown\n')
                    process.stdin.flush()
                    assert 'data: closed' in list(lines)
                    assert process.wait(timeout=20) == 0
            assert (code / '.data/development/drained').read_text() == 'completed'
            for port in (api_port, web_port):
                with socket.socket() as probe:
                    assert probe.connect_ex(('127.0.0.1', port)) != 0
        finally:
            if process.poll() is None:
                process.stdin.close()
                process.wait(timeout=20)
