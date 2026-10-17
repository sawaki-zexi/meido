"""A01 uses the actual memory engine and network safety through the main factory."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.memory_config import MemorySettingsStore
from backend.app.memory_documents import MemoryDocuments
from backend.app.memory_engine import DefaultMemoryEngine, MemoryMutation, MemoryScope
from backend.app.memory_events import MemoryEventBus, RetrievalCompleted
from backend.app.memory_service import MemoryService
from backend.app.memory_store import MemoryStore
from backend.app.mcp_runtime import McpHost
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import MemorySourceRef
from backend.app.network_tools import NetworkToolsConfig
from backend.app.proactive_runtime.identity import AutonomyOrigin
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from test_mcp_http import process_alive
from test_proactive_mcp import source  # noqa: F401
from test_proactive_snapshot import grant_for
from test_proactive_tool_protocol import ScriptedProtocol, call, skip


@pytest.fixture(autouse=True)
def dependency_environment(tmp_path, monkeypatch, local_platform_only):
    monkeypatch.setenv("MEIDO_DATA_DIR", str(tmp_path / "isolated-data"))
    monkeypatch.setenv("MEIDO_ROLES_DIR", str(tmp_path / "isolated-roles"))
    monkeypatch.setenv("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", str(tmp_path / "isolated-owner"))
    monkeypatch.delenv("MEIDO_PROJECT_ROOT", raising=False)
    yield


@pytest.fixture
def dependencies(source, tmp_path, monkeypatch, dependency_environment):
    from backend.app import main
    root, mcp_store, server_input, *_ = source
    data = tmp_path / "dependency-data"
    initialize_databases(data)
    roles, sessions = RoleStore(tmp_path / "roles"), SessionStore(data / "sessions.db")
    tasks, host = TaskRuntime(tmp_path / "tasks"), McpHost(mcp_store)
    settings = MemorySettingsStore(data / "memory-settings.json")
    settings.update(enabled=True, embedding_enabled=False)
    memories, bus = MemoryStore(data / "memory.db"), MemoryEventBus()
    service = MemoryService(memories, implicit_extraction_enabled=False,
        memory_enabled=lambda: settings.get().enabled, event_bus=bus)
    engine = DefaultMemoryEngine(service, role_exists=lambda key: roles.get(key) is not None, event_bus=bus)
    adapter, now = ScriptedProtocol(), [1780000000.0]
    for name, value in {"store": roles, "roles_root": roles.root, "session_store": sessions,
        "task_runtime": tasks, "mcp_host": host, "memory_settings_store": settings,
        "memory_store": memories, "memory_service": service, "memory_engine": engine,
        "memory_event_bus": bus, "network_tools_config": NetworkToolsConfig(),
        "model_configuration_store": ModelConfigurationStore(data / "models.json"),
        "model_adapter": adapter, "_proactive": None, "_proactive_clock": lambda: now[0]}.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    configuration = client.post("/api/model/configurations", json={"providerId": "custom", "provider": "local",
        "baseUrl": "http://127.0.0.1:1/v1", "model": "controlled-no-provider-I/O"}).json()["configuration"]
    response = client.post("/api/roles", json={"name": "真实依赖角色", "profile": {"profile": "local fixture"},
        "agentConfig": {"memoryRecall": {"enabled": True}, "enabledTools": ["web_fetch", "web_search"]}})
    assert response.status_code == 201, response.text
    role_id = response.json()["role"]["id"]
    foreign = client.post("/api/roles", json={"name": "另一角色", "profile": {"profile": "not shared"}}).json()["role"]["id"]
    saved_server = mcp_store.save(server_input.model_copy(update={"roleIds": [role_id]}))
    grant = grant_for(source).model_copy(update={"configRevision": saved_server.revision, "grantRevision": saved_server.grant_id})
    response = client.put(f"/api/roles/{role_id}/autonomy", json={"expectedRevision": 0, "config": {
        "activeDecisionEnabled": True, "decisionModelConfigurationId": configuration["id"],
        "consentRevision": 1, "sourceGrants": [grant.model_dump(mode="json")]}})
    assert response.status_code == 200, response.text
    application = main._proactive_application()
    yield SimpleNamespace(main=main, client=client, roles=roles, sessions=sessions, tasks=tasks, host=host,
        app=application, adapter=adapter, now=now, role_id=role_id, foreign_role=foreign, settings=settings,
        memories=memories, service=service, engine=engine, bus=bus, root=root)
    asyncio.run(application.close())
    asyncio.run(host.close())
    tasks.close()
    client.close()
    if (root / "pid").exists():
        assert not process_alive(int((root / "pid").read_text()))


def run_tick(rig, owner_actor=None):
    async def exercise():
        rig.now[0] += 1800
        assert await rig.app.on_clock() == 1
        async with asyncio.timeout(10):
            if owner_actor is not None:
                await owner_actor()
            while True:
                await rig.app.recover()
                state = rig.app.query(rig.role_id)
                if state["runs"] and all(run["runtimeAcknowledged"] for run in state["runs"]):
                    return state
                await asyncio.sleep(.01)
    return asyncio.run(exercise())


def update_policy(rig, **changes):
    role = rig.roles.get(rig.role_id)
    policy = role.agentConfig.model_dump(mode="json")
    policy.update(changes)
    response = rig.client.put(f"/api/roles/{rig.role_id}", json={"name": role.name,
        "profile": role.profile.model_dump(mode="json"), "agentConfig": policy})
    assert response.status_code == 200, response.text


def memory_snapshot(rig):
    return {role: [item.model_dump(mode="json") for item in rig.memories.list_all(role)]
        for role in (rig.role_id, rig.foreign_role)}


def actual_origin(run):
    value = run["origin"]
    return AutonomyOrigin(value["roleId"], value["tickId"], value["grantRevision"], value["generation"], tuple(value["allowedModes"]))


def test_real_main_tick_reads_only_two_current_role_memories_without_mutation(dependencies):
    rig = dependencies
    async def seed():
        owned = []
        for owner, suffix in [(rig.role_id, "first"), (rig.role_id, "second"), (rig.role_id, "third"),
                              (rig.foreign_role, "foreign")]:
            result = await rig.engine.mutate(MemoryMutation(kind="remember", scope=MemoryScope(owner, f"role:{owner}"),
                summary=f"scope731 {suffix}", memory_kind="fact", source_ref=f"local-{suffix}"))
            if owner == rig.role_id:
                owned.append(result.item_id)
        return set(owned)
    owned = asyncio.run(seed())
    documents = MemoryDocuments(rig.roles.root)
    documents.sync_structured_memory(rig.role_id, rig.memories.list_active(rig.role_id))
    before, before_document = memory_snapshot(rig), documents.read_document(rig.role_id, "MEMORY.md")
    steps = [call("recall_memory", query="scope731"), call("recall_memory", query="scope731"), skip()]
    rig.adapter.script = lambda step, messages: steps[step - 1]
    state = run_tick(rig)
    assert state["runs"][0]["status"] == "skipped"
    for request in (2, 3):
        records = json.loads(rig.adapter.result(request).content[0].text)
        assert len(records) == 2 and {item["id"] for item in records} <= owned
        assert all(item["source"]["session_key"] == f"role:{rig.role_id}" for item in records)
        assert "foreign" not in json.dumps(records)
    retrievals = [event for event in rig.bus.events if isinstance(event, RetrievalCompleted)]
    assert len(retrievals) == 2
    assert all(event.role_id == rig.role_id and event.session_key == f"role:{rig.role_id}"
        and event.trace["effect"] == "read_only" and event.result_count == 2 for event in retrievals)
    assert memory_snapshot(rig) == before
    assert documents.read_document(rig.role_id, "MEMORY.md") == before_document
    assert rig.sessions.list_messages(f"role:{rig.role_id}") == []


@pytest.mark.parametrize("foreign_source", ["another-role", "unshared-channel"])
def test_real_engine_and_store_refuse_foreign_source_before_memory_can_be_recalled(dependencies, foreign_source):
    rig = dependencies
    session = f"role:{rig.foreign_role}" if foreign_source == "another-role" else "channel:qq:unshared"
    source_ref = MemorySourceRef(kind="manual", sessionKey=session, stableSourceKey="forbidden-source")
    before = memory_snapshot(rig)
    with pytest.raises(PermissionError, match="source scope"):
        asyncio.run(rig.engine.mutate(MemoryMutation(kind="remember", scope=MemoryScope(rig.role_id, f"role:{rig.role_id}"),
            source=source_ref, summary="forbidden memory", memory_kind="fact")))
    with pytest.raises(ValueError, match="sessionKey"):
        rig.service.remember(rig.role_id, "forbidden memory", "fact", session_key=session)
    assert memory_snapshot(rig) == before


def test_real_memory_global_pause_is_enforced_by_main_autonomous_wrapper(dependencies):
    rig = dependencies
    rig.service.remember(rig.role_id, "scope731 retained", "fact")
    before = memory_snapshot(rig)
    rig.settings.update(enabled=False)
    rig.adapter.script = lambda step, messages: call("recall_memory", query="scope731") if step == 1 else skip()
    assert run_tick(rig)["runs"][0]["status"] == "skipped"
    assert rig.adapter.result(2).is_error
    assert [event for event in rig.bus.events if isinstance(event, RetrievalCompleted)] == []
    assert memory_snapshot(rig) == before


@pytest.fixture
def denied_peer(monkeypatch, dependency_environment):
    requests, connections = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"must not be fetched")
        do_POST = do_GET
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == server.server_port:
            connections.append(address)
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/private", requests=requests, connections=connections)
    server.shutdown()
    server.server_close()
    worker.join(5)
    assert not worker.is_alive()


@pytest.mark.parametrize("tool", ["web_fetch", "web_search"])
def test_real_network_tools_reject_local_peer_before_any_socket_dispatch(dependencies, denied_peer, monkeypatch, tool):
    rig = dependencies
    if tool == "web_search":
        monkeypatch.setattr(rig.main, "network_tools_config", NetworkToolsConfig(search_provider="exa_api", search_endpoint=denied_peer.url))
    arguments = {"url": denied_peer.url} if tool == "web_fetch" else {"query": "local synthetic endpoint"}
    rig.adapter.script = lambda step, messages: call(tool, **arguments) if step == 1 else skip()
    state = run_tick(rig)
    assert state["runs"][0]["status"] == "skipped"
    assert rig.app.tool_permitted(actual_origin(state["runs"][0]), tool)
    assert rig.adapter.result(2).is_error
    assert denied_peer.requests == [] and denied_peer.connections == []
    assert state["acknowledgements"] == []


@pytest.mark.parametrize("web_granted", [True, False])
def test_real_ssrf_body_failure_remains_candidate_without_implicit_negative_interest(dependencies, denied_peer, web_granted):
    rig = dependencies
    if not web_granted:
        update_policy(rig, enabledTools=[])
    (rig.root / "events.json").write_text(json.dumps([{"id": "body-ssrf", "kind": "content", "url": denied_peer.url}]), encoding="utf-8")
    identifier = []
    def script(step, messages):
        if step == 1:
            return call("get_content_events")
        if step == 2:
            identifier.append(json.loads(messages[-1].content[0].text)[0]["id"])
            return call("get_content", item_ids=identifier)
        return skip()
    rig.adapter.script = script
    state = run_tick(rig)
    assert state["runs"][0]["status"] == "skipped"
    assert json.loads(rig.adapter.result(3).content[0].text) == {identifier[0]: {"body": "", "error": "body prefetch failed"}}
    assert state["acknowledgements"] == []
    assert denied_peer.requests == [] and denied_peer.connections == []


@pytest.mark.parametrize("tool", ["web_fetch", "web_search"])
def test_actual_network_host_switch_still_denies_role_granted_autonomous_tools(dependencies, denied_peer, monkeypatch, tool):
    rig = dependencies
    monkeypatch.setattr(rig.main, "network_tools_config", NetworkToolsConfig(enabled=False,
        search_provider="exa_api", search_endpoint=denied_peer.url))
    arguments = {"url": denied_peer.url} if tool == "web_fetch" else {"query": "local fixture"}
    rig.adapter.script = lambda step, messages: call(tool, **arguments) if step == 1 else skip()
    state = run_tick(rig)
    assert state["runs"][0]["status"] == "skipped" and rig.adapter.result(2).is_error
    assert denied_peer.requests == [] and denied_peer.connections == []


@pytest.mark.parametrize("change", ["missing", "revoked", "widened"])
def test_actual_saved_role_grant_never_expands_or_survives_revocation_in_tick(dependencies, denied_peer, monkeypatch, change):
    rig = dependencies
    if change in {"missing", "widened"}:
        update_policy(rig, enabledTools=[])
    actor = None
    entered, release = threading.Event(), threading.Event()
    if change == "missing":
        steps = [call("get_content_events"), call("web_fetch", url=denied_peer.url), skip()]
        rig.adapter.script = lambda step, messages: steps[step - 1]
    else:
        class AwaitIndependentOwner(ScriptedProtocol):
            async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
                from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone
                self.requests.append((messages, tools, max_tokens))
                if len(self.requests) == 1:
                    action = call("get_content_events")
                else:
                    entered.set()
                    while not release.is_set():
                        await asyncio.sleep(.01)
                    action = call("web_fetch", url=denied_peer.url)
                yield ModelStreamDone(ModelAssistantMessage(content=(action,)))
        rig.adapter = AwaitIndependentOwner()
        monkeypatch.setattr(rig.main, "model_adapter", rig.adapter)
        async def actor():
            while not entered.is_set():
                await asyncio.sleep(.01)
            # The owner is an independent HTTP caller. Blocking a model worker
            # on the request that cancels that same worker would be a fixture
            # self-join, unlike actual owner configuration in the application.
            await asyncio.to_thread(update_policy, rig, enabledTools=[] if change == "revoked" else ["web_fetch"])
    try:
        state = run_tick(rig, actor)
    finally:
        release.set()
    assert state["runs"][0]["status"] == ("skipped" if change == "missing" else "aborted")
    if change == "missing":
        assert rig.adapter.result(3).is_error
    else:
        origin = actual_origin(state["runs"][0])
        with pytest.raises(PermissionError):
            asyncio.run(rig.main._proactive_web(origin, "web_fetch", {"url": denied_peer.url}))
    assert denied_peer.requests == [] and denied_peer.connections == []
    assert state["acknowledgements"] == []
