"""Real CLI wrappers own only the processes launched by that invocation."""

import asyncio
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from backend.app.herdr_control.cli import CommandError, HerdrCli
from backend.app.herdr_control.config import HerdrConfig


def process_running(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        stat = Path(f'/proc/{pid}/stat')
        return not stat.exists() or stat.read_text().split(') ')[1][0] != 'Z'
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87  # process no longer exists
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def stop_fixture_process(pid):
    if not process_running(pid):
        return
    if os.name != 'nt':
        os.kill(pid, signal.SIGKILL)
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100001, False, pid)
    if not handle:
        assert ctypes.get_last_error() == 87
        return
    try:
        assert kernel.TerminateProcess(handle, 1)
        assert kernel.WaitForSingleObject(handle, 2000) == 0
    finally:
        kernel.CloseHandle(handle)


@pytest.fixture(params=['native', pytest.param('cmd', marks=pytest.mark.skipif(
    os.name != 'nt', reason='Windows npm-style batch launcher'))])
def wrapper(tmp_path, request, monkeypatch):
    script = tmp_path / 'wrapper.py'
    script.write_text('''import json, os, subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
mode = sys.argv[1]
streams = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL} if mode == 'success' else {}
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(4)'],
    stdin=subprocess.DEVNULL, creationflags=0x08000000 if os.name == 'nt' else 0, **streams)
(root / 'pids.json').write_text(json.dumps([os.getpid(), child.pid]))
with (root / 'inputs.txt').open('a') as log:
    log.write('input\\n')
print('complete', flush=True)
if mode == 'hold':
    time.sleep(30)
''', encoding='utf-8')
    argv = [sys.executable, '-I', str(script)]
    if request.param == 'cmd':
        batch = tmp_path / 'wrapper.cmd'
        batch.write_text(f'@echo off\n"{sys.executable}" -I "{script}" %*\n', encoding='utf-8')
        argv = [str(batch)]
    popen = subprocess.Popen
    sentinel = popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    launched = []

    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    try:
        yield argv, tmp_path, sentinel, launched
    finally:
        try:
            marker = tmp_path / 'pids.json'
            if marker.exists():
                for pid in json.loads(marker.read_text()):
                    stop_fixture_process(pid)
            for process in launched:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
        finally:
            sentinel.kill()
            sentinel.wait(timeout=2)


def assert_owned_processes_stopped(root, sentinel, launched):
    assert launched and all(process.poll() is not None for process in launched)
    pids = json.loads((root / 'pids.json').read_text())
    # POSIX killpg sends the signal without waiting for the orphan's reaper.
    deadline = time.monotonic() + .5
    while any(process_running(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(.01)
    assert all(not process_running(pid) for pid in pids)
    assert sentinel.poll() is None, 'CLI cleanup terminated an unrelated process'
    assert (root / 'inputs.txt').read_text().splitlines() == ['input']


@pytest.mark.parametrize('mode', ['hold', 'early_exit'])
def test_timeout_reaps_wrapper_descendants_with_inherited_pipes(wrapper, mode):
    argv, root, sentinel, launched = wrapper

    async def run():
        cli = HerdrCli(HerdrConfig(), root)
        started = time.monotonic()
        with pytest.raises(CommandError) as failure:
            await cli.command([*argv, mode], timeout=.7)
        assert failure.value.code == 'timeout'
        assert time.monotonic() - started < 2, 'inherited pipes extended the CLI deadline'
        assert_owned_processes_stopped(root, sentinel, launched)

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.parametrize('mode', ['hold', 'early_exit'])
def test_cancellation_joins_wrapper_descendants_with_inherited_pipes(wrapper, mode):
    argv, root, sentinel, launched = wrapper

    async def run():
        cli = HerdrCli(HerdrConfig(), root)
        operation = asyncio.create_task(cli.command([*argv, mode], timeout=10))
        try:
            for _ in range(200):
                if (root / 'inputs.txt').exists():
                    break
                await asyncio.sleep(.01)
            assert (root / 'inputs.txt').exists(), 'controlled CLI did not start'
            if mode == 'early_exit':
                for _ in range(200):
                    if launched[0].poll() is not None:
                        break
                    await asyncio.sleep(.01)
                assert launched[0].poll() is not None
            started = time.monotonic()
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert time.monotonic() - started < 2, 'cancellation waited for orphaned pipe owners'
            assert_owned_processes_stopped(root, sentinel, launched)
        finally:
            if not operation.done():
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await operation

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


def test_successful_wrapper_still_reaps_its_descendants(wrapper):
    argv, root, sentinel, launched = wrapper

    async def run():
        cli = HerdrCli(HerdrConfig(), root)
        assert (await cli.command([*argv, 'success'])).strip() == 'complete'
        assert_owned_processes_stopped(root, sentinel, launched)

    asyncio.run(run())


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job cleanup acknowledgement')
def test_cleanup_error_preserves_input_uncertainty_without_replay(wrapper, monkeypatch):
    from backend.app.herdr_control.cli import WindowsJob

    argv, root, sentinel, launched = wrapper
    close = WindowsJob.close

    def uncertain_close(owner):
        close(owner)
        raise OSError('fixture: cleanup acknowledgement failed')

    monkeypatch.setattr(WindowsJob, 'close', uncertain_close)

    async def run():
        cli = HerdrCli(HerdrConfig(), root)
        with pytest.raises(CommandError, match='输入可能已送达') as failure:
            await cli.command([*argv, 'success'])
        assert failure.value.code == 'invalid_response'
        assert_owned_processes_stopped(root, sentinel, launched)

    asyncio.run(run())
