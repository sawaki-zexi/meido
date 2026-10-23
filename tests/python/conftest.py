"""Opt-in network boundary for directory protocol fixtures."""
import asyncio
import socket

import pytest


@pytest.fixture
def local_platform_only(monkeypatch, request):
    blocked = []
    local = {"127.0.0.1", "localhost", "::1", b"127.0.0.1", b"localhost", b"::1"}

    def check(host):
        if host is not None and host not in local:
            blocked.append(str(host))
            raise AssertionError("Directory fixtures forbid non-local platform I/O")

    resolve, connect, connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
    create_connection = asyncio.BaseEventLoop.create_connection

    def guarded_resolve(host, *args, **kwargs):
        check(host)
        return resolve(host, *args, **kwargs)

    def guarded_connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address[0])
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address[0])
        return connect_ex(sock, address)

    async def guarded_create(loop, protocol_factory, host=None, port=None, **kwargs):
        check(host)
        return await create_connection(loop, protocol_factory, host=host, port=port, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_resolve)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection", guarded_create)
    yield blocked
    # Check after dependent fixtures and their owned background work close.
    assert blocked == [], f"Unexpected non-local connection attempts: {blocked}"


@pytest.fixture(autouse=True)
def enable_autonomy_for_proactive_integration_tests(request, monkeypatch, tmp_path):
    """Give legacy proactive integration fixtures their explicit global grant.

    The application now keeps autonomy behind a global plugin switch, which is
    intentionally off by default for users.  These tests exercise the runtime
    itself and historically enabled only the role-level grants, so opt them in
    here without changing the product default or the plugin configuration
    contract tests.
    """
    if not (request.node.path.name.startswith("test_proactive")
            or request.node.path.name == "test_drift_meme_api.py"):
        return
    from backend.app import main
    from backend.app.plugin_configuration import PluginConfigurationStore

    # Keep this fixture's file separate from tests that intentionally create a
    # fresh store at the test root to verify the disabled default.
    store = PluginConfigurationStore(tmp_path / "__autonomy_fixture" / "plugins.json")
    state = store.prepare("autonomy", expected_revision=0, enabled=True)
    store.finish("autonomy", state.revision)
    monkeypatch.setattr(main, "plugin_configuration", store)
