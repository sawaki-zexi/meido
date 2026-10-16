"""Real Windows trees must retain cleanup ownership after OS failures."""
import asyncio
import ctypes
import subprocess
import sys
import uuid

import pytest

from backend.app.browser_use.process import WindowsJob
from backend.app.mcp_stdio import McpStdioClient
from test_mcp_stdio import configure


pytestmark = [pytest.mark.skipif(sys.platform != "win32", reason="Windows Job API"),
              pytest.mark.usefixtures("local_platform_only")]


@pytest.fixture
def named_tree():
    name = "Local\\Meido.Test.OwnedCleanup." + uuid.uuid4().hex
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(600)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000 | 0x00000004)
    owner = None
    try:
        owner = WindowsJob(child.pid, resume=True, name=name)
        yield name, owner, child
    finally:
        if owner is not None:
            owner.close()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_failed_termination_retains_named_tree_until_retry(named_tree, monkeypatch):
    name, owner, child = named_tree
    assert WindowsJob.named_job_active(name)
    with monkeypatch.context() as patch:
        def deny_termination(*_):
            ctypes.set_last_error(5)
            return 0
        patch.setattr(owner._kernel, "TerminateJobObject", deny_termination)
        with pytest.raises(OSError):
            owner.close()
        assert not owner.exit_confirmed
        assert child.poll() is None
        assert WindowsJob.named_job_active(name)
    owner.close()
    child.wait(timeout=10)
    assert owner.exit_confirmed
    assert not WindowsJob.named_job_active(name)


def test_failed_exit_query_requires_retry_before_confirming_release(named_tree, monkeypatch):
    name, owner, child = named_tree
    with monkeypatch.context() as patch:
        def deny_query(*_):
            ctypes.set_last_error(5)
            return 0
        patch.setattr(owner._kernel, "QueryInformationJobObject", deny_query)
        with pytest.raises(OSError):
            owner.close()
        assert not owner.exit_confirmed
    owner.close()
    child.wait(timeout=10)
    assert owner.exit_confirmed
    assert not WindowsJob.named_job_active(name)


def test_stdio_close_retries_failed_owned_cleanup(tmp_path, monkeypatch):
    store, config = configure(tmp_path)

    async def exercise():
        client = McpStdioClient(config, store.environment(config),
            job_name="Local\\Meido.Test.McpCleanup." + uuid.uuid4().hex)
        try:
            await client.connect()
            assert not client.exit_confirmed
            original = WindowsJob.close
            attempts = 0

            def fail_once(owner):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("injected owned-tree cleanup failure")
                return original(owner)

            with monkeypatch.context() as patch:
                patch.setattr(WindowsJob, "close", fail_once)
                with pytest.raises(OSError, match="cleanup failure"):
                    await client.close()
                assert not client.exit_confirmed
                await client.close()
                assert client.exit_confirmed
                assert attempts == 2
        finally:
            await client.close()

    asyncio.run(exercise())
