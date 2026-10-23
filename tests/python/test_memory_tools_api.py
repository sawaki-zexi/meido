import json
import asyncio
import time
import threading
import httpx
import pytest

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime.types import ToolResultMessage
from backend.app.memory_config import MemorySettingsStore
from backend.app.memory_documents import MemoryDocuments
from backend.app.memory_engine import DefaultMemoryEngine
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_store import MemoryStore
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import AgentToolConfig, MemoryRecallToolConfig, RoleInput, RoleProfile
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


def test_owner_memory_write_recall_forget_roundtrip_uses_real_engine_and_updates_documents(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="记忆角色", profile=RoleProfile(profile="设定")))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    memories = MemoryStore(tmp_path / "data" / "memory.db")
    settings = MemorySettingsStore(tmp_path / "data" / "settings.json")
    service = MemoryService(memories, implicit_extraction_enabled=False, memory_enabled=lambda: settings.get().enabled)
    engine = DefaultMemoryEngine(service, role_exists=lambda value: roles.get(value) is not None)

    class Provider:
        requests = []

        async def stream_structured_messages(self, messages, configuration, tools):
            self.requests.append((messages, tools))
            turn = len(self.requests)
            if turn == 1:
                assert {"memorize", "recall_memory", "forget_memory"} <= {item["function"]["name"] for item in tools}
                yield ModelToolCall("save", "memorize", {"summary": "主人喜欢海边", "memory_kind": "preference"})
            elif turn == 2:
                self.item_id = json.loads(next(item["content"] for item in reversed(messages) if item.get("role") == "tool"))["item_id"]
                assert "主人喜欢海边" in MemoryDocuments(tmp_path / "roles").read_document(role.id, "MEMORY.md")
                yield ModelToolCall("find", "recall_memory", {"query": "海边"})
            elif turn == 3:
                recalled = json.loads(next(item["content"] for item in reversed(messages) if item.get("role") == "tool"))
                assert recalled["items"][0]["id"] == self.item_id
                source = sessions.user_message_for_run(sessions.list_messages(f"role:{role.id}")[0].runId)
                assert recalled["items"][0]["evidence"][0]["refs"] == [source.id]
                yield ModelToolCall("forget", "forget_memory", {"ids": [self.item_id]})
            elif turn == 4:
                assert "主人喜欢海边" not in MemoryDocuments(tmp_path / "roles").read_document(role.id, "MEMORY.md")
                yield ModelToolCall("find-again", "recall_memory", {"query": "海边"})
            else:
                payload = json.loads(next(item["content"] for item in reversed(messages) if item.get("role") == "tool"))
                assert payload["count"] == 0
                yield ModelTextDelta("已保存、核验并撤销该记忆")

    provider = Provider()
    for name, value in {"store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions),
            "model_adapter": provider, "model_configuration_store": ModelConfigurationStore(tmp_path / "data" / "models.json"),
            "roles_root": tmp_path / "roles", "role_locks": {}, "memory_store": memories, "memory_service": service,
            "memory_engine": engine, "memory_worker": MemoryWorker(service), "memory_settings_store": settings}.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    before = {item["name"]: item for item in client.get(f"/api/roles/{role.id}/tools").json()["tools"]}
    assert before["memorize"]["enabled"] and before["forget_memory"]["enabled"]
    saved = client.put(f"/api/roles/{role.id}", json={"name": role.name, "profile": role.profile.model_dump(),
        "agentConfig": {"enabledTools": ["memorize", "forget_memory"], "memoryRecall": {"enabled": True}}})
    assert saved.status_code == 200
    assert client.get(f"/api/roles/{role.id}").json()["role"]["agentConfig"]["enabledTools"] == ["memorize", "forget_memory"]
    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "我喜欢海边；用工具保存、核验再撤销该记录。"})
    assert "event: assistant_completed" in response.text
    results = [item for item in sessions.runtime_messages(f"role:{role.id}") if isinstance(item, ToolResultMessage)]
    assert len(results) == 4 and all(not item.is_error for item in results)
    assert "已保存、核验并撤销" in client.get(f"/api/roles/{role.id}/session").text


def test_global_memory_switch_hides_all_tools_and_rejects_manual_enabled_tools(tmp_path, monkeypatch):
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="关闭记忆", profile=RoleProfile(profile="设定"), agentConfig=AgentToolConfig(
        enabledTools=["memorize", "recall_memory", "forget_memory"], memoryRecall=MemoryRecallToolConfig(enabled=True))))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    settings = MemorySettingsStore(tmp_path / "data" / "settings.json")
    settings.update(enabled=False)
    memories = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memories, memory_enabled=lambda: settings.get().enabled)
    engine = DefaultMemoryEngine(service, role_exists=lambda value: roles.get(value) is not None)

    class Provider:
        calls = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            assert {"memorize", "recall_memory", "forget_memory"}.isdisjoint({item["function"]["name"] for item in tools})
            if self.calls == 1:
                yield ModelToolCall("forged-write", "memorize", {"summary": "不应保存"})
            elif self.calls == 2:
                yield ModelToolCall("forged-read", "recall_memory", {"query": "test"})
            elif self.calls == 3:
                yield ModelToolCall("forged-forget", "forget_memory", {"ids": ["unknown"]})
            else:
                yield ModelTextDelta("全局记忆关闭")

    for name, value in {"store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions),
            "model_adapter": Provider(), "model_configuration_store": ModelConfigurationStore(tmp_path / "data" / "models.json"),
            "roles_root": tmp_path / "roles", "role_locks": {}, "memory_store": memories, "memory_service": service,
            "memory_engine": engine, "memory_worker": MemoryWorker(service), "memory_settings_store": settings}.items():
        monkeypatch.setattr(main, name, value)
    client = TestClient(main.app)
    catalog = {item["name"]: item for item in client.get(f"/api/roles/{role.id}/tools").json()["tools"]}
    for name in ("memorize", "forget_memory"):
        assert catalog[name]["enabled"] and not catalog[name]["authorized"]
        assert catalog[name]["reason"] == "全局记忆已关闭"
        assert catalog[name]["dependency"]["status"] == "unavailable"
    response = client.post(f"/api/roles/{role.id}/messages", json={"content": "尝试调用"})
    assert "event: assistant_completed" in response.text
    results = [item for item in sessions.runtime_messages(f"role:{role.id}") if isinstance(item, ToolResultMessage)]
    assert len(results) == 3 and all(item.is_error for item in results)
    assert engine.list_active_items(role.id) == []


@pytest.mark.parametrize("interrupt", ["deadline", "cancel_request"])
def test_memory_io_deadline_rolls_back_http_sqlite_and_documents_after_embedding_finishes(tmp_path, monkeypatch, interrupt):
    from backend.app.memory_engine import MemoryMutation, MemoryScope

    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="超时记忆", profile=RoleProfile(profile="设定"),
        agentConfig=AgentToolConfig(enabledTools=["memorize"])))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    settings = MemorySettingsStore(tmp_path / "data" / "settings.json")
    memories = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memories, implicit_extraction_enabled=False, memory_enabled=lambda: settings.get().enabled)
    engine = DefaultMemoryEngine(service, role_exists=lambda value: roles.get(value) is not None)
    saved = asyncio.run(engine.mutate(MemoryMutation(kind="remember", scope=MemoryScope(role.id, f"role:{role.id}"), summary="保留的原始记忆")))
    documents = MemoryDocuments(tmp_path / "roles")
    documents.sync_structured_memory(role.id, engine.list_active_items(role.id))
    original = documents.read_document(role.id, "MEMORY.md")
    embedding_started = threading.Event()

    class Embeddings:
        def embed(self, role_id, text):
            if text == "本次不应写入":
                embedding_started.set()
                time.sleep(.5)
            return [1.0, 0.0]

    class Provider:
        calls = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall("write-timeout", "memorize", {"summary": "本次不应写入"})
            else:
                yield ModelTextDelta("操作超时，原记忆已保留")

    service.embedding_provider = Embeddings()
    monkeypatch.setenv("MEIDO_MEMORY_TOOL_IO_TIMEOUT_SECONDS", ".2" if interrupt == "deadline" else "15")
    for name, value in {"store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions),
            "model_adapter": Provider(), "model_configuration_store": ModelConfigurationStore(tmp_path / "data" / "models.json"),
            "roles_root": tmp_path / "roles", "role_locks": {}, "memory_store": memories, "memory_service": service,
            "memory_engine": engine, "memory_worker": MemoryWorker(service), "memory_settings_store": settings}.items():
        monkeypatch.setattr(main, name, value)

    async def run():
        ticks = []

        async def wait_for_embedding():
            end = asyncio.get_running_loop().time() + 3
            while not embedding_started.is_set():
                assert asyncio.get_running_loop().time() < end, "embedding was never reached"
                await asyncio.sleep(.001)

        async def heartbeat():
            await wait_for_embedding()
            started = asyncio.get_running_loop().time()
            await asyncio.sleep(.01)
            ticks.append(asyncio.get_running_loop().time() - started)

        timer = asyncio.create_task(heartbeat())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            request = asyncio.create_task(client.post(f"/api/roles/{role.id}/messages", json={"content": "执行一次显式工具操作"}))
            if interrupt == "cancel_request":
                await wait_for_embedding()
                await asyncio.sleep(.05)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                response = await request
        await timer
        assert ticks[0] < .15
        if interrupt == "deadline":
            assert "event: assistant_completed" in response.text
        results = [item for item in sessions.runtime_messages(f"role:{role.id}") if isinstance(item, ToolResultMessage)]
        if interrupt == "deadline":
            assert len(results) == 1 and results[0].is_error
        await asyncio.sleep(.4)
        assert [item.id for item in engine.list_active_items(role.id)] == [saved.item_id]
        assert documents.read_document(role.id, "MEMORY.md") == original

    asyncio.run(run())


def test_cancelled_http_receive_actor_finishes_before_client_cleanup(tmp_path, monkeypatch, record_property, local_platform_only):
    import anyio
    from contextlib import suppress
    from backend.app.agent_runtime.runtime import RuntimeManager
    from backend.app.credentials import SystemCredentialStore
    from backend.app.memory_engine import MemoryMutation, MemoryScope

    monkeypatch.setattr(SystemCredentialStore, "get", lambda self, key: "")

    def reject_vault_write(self, key, value):
        raise AssertionError("HTTP fixture cannot write OS credentials")

    monkeypatch.setattr(SystemCredentialStore, "set", reject_vault_write)
    roles = RoleStore(tmp_path / "roles")
    role = roles.create(RoleInput(name="取消监听", profile=RoleProfile(profile="设定"),
        agentConfig=AgentToolConfig(enabledTools=["memorize"])))
    initialize_databases(tmp_path / "data")
    sessions = SessionStore(tmp_path / "data" / "sessions.db")
    settings = MemorySettingsStore(tmp_path / "data" / "settings.json")
    memories = MemoryStore(tmp_path / "data" / "memory.db")
    service = MemoryService(memories, implicit_extraction_enabled=False, memory_enabled=lambda: settings.get().enabled)
    engine = DefaultMemoryEngine(service, role_exists=lambda value: roles.get(value) is not None)
    saved = asyncio.run(engine.mutate(MemoryMutation(kind="remember", scope=MemoryScope(role.id, f"role:{role.id}"), summary="保留的原始记忆")))
    documents = MemoryDocuments(tmp_path / "roles")
    documents.sync_structured_memory(role.id, engine.list_active_items(role.id))
    original = documents.read_document(role.id, "MEMORY.md")
    embedding_started, embedding_release, embedding_finished = (threading.Event() for _ in range(3))

    class Embeddings:
        def embed(self, role_id, text):
            if text == "本次不应写入":
                embedding_started.set()
                try:
                    assert embedding_release.wait(5), "fixture embedding was not released"
                finally:
                    embedding_finished.set()
            return [1.0, 0.0]

    class Provider:
        calls = 0

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall("cancelled-write", "memorize", {"summary": "本次不应写入"})
            else:
                yield ModelTextDelta("原记忆已保留")

    service.embedding_provider = Embeddings()
    monkeypatch.setenv("MEIDO_MEMORY_TOOL_IO_TIMEOUT_SECONDS", "15")
    for name, value in {"store": roles, "session_store": sessions, "session_manager": SessionManager(roles, sessions),
            "runtime_manager": RuntimeManager(sessions), "model_adapter": Provider(),
            "model_configuration_store": ModelConfigurationStore(tmp_path / "data" / "models.json"),
            "roles_root": tmp_path / "roles", "role_locks": {}, "memory_store": memories, "memory_service": service,
            "memory_engine": engine, "memory_worker": MemoryWorker(service), "memory_settings_store": settings}.items():
        monkeypatch.setattr(main, name, value)

    async def run():
        loop = asyncio.get_running_loop()
        actor_cancel_delivered = asyncio.Event()
        actor: asyncio.Task[None] | None = None
        actor_in_receive = False
        actor_cancel_calls = 0
        valid_overlap = False
        transport_disconnected = False
        ticks = []

        async def wait_for_embedding():
            end = loop.time() + 3
            while not embedding_started.is_set():
                assert loop.time() < end, "embedding was never reached"
                await asyncio.sleep(.001)

        async def heartbeat():
            await wait_for_embedding()
            started = loop.time()
            await asyncio.sleep(.01)
            ticks.append(loop.time() - started)

        def cancel_receive_actor():
            nonlocal actor_cancel_calls, valid_overlap
            assert actor is not None
            actor_cancel_calls += 1
            valid_overlap = actor_in_receive
            record_property("actor_id", id(actor))
            record_property("actor_cancelling_before", actor.cancelling())
            actor.cancel("single owner cancellation during ASGI receive")
            record_property("actor_cancelling_after", actor.cancelling())
            actor_cancel_delivered.set()

        async def connected_client(scope, receive, send):
            async def controlled_receive():
                nonlocal actor, actor_in_receive
                probe = anyio.current_effective_deadline() == float("-inf")
                if probe and transport_disconnected:
                    return {"type": "http.disconnect"}
                selected = probe and embedding_started.is_set() and actor is None
                if selected:
                    actor = asyncio.current_task()
                    actor_in_receive = True
                    loop.call_soon(cancel_receive_actor)
                try:
                    return await receive()
                finally:
                    if selected:
                        actor_in_receive = False

            await main.app(scope, controlled_receive, send)

        timer = asyncio.create_task(heartbeat())
        ready = asyncio.create_task(actor_cancel_delivered.wait())
        actor_finished_before_cleanup = False
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=connected_client), base_url="http://test") as client:
            request = asyncio.create_task(client.post(f"/api/roles/{role.id}/messages", json={"content": "执行一次显式工具操作"}))
            try:
                done, _ = await asyncio.wait({ready, request}, timeout=3)
                assert ready in done and actor is not None, "real cancelled-deadline ASGI receive was never reached"
                assert valid_overlap and actor_cancel_calls == 1, "fixture did not deliver its single cancellation inside receive"
                assert not request.done() and not embedding_finished.is_set(), "HTTP and embedding must remain live during observation"
                await asyncio.wait({actor}, timeout=.25)
                actor_finished_before_cleanup = actor.done()
                record_property("actor_done_before_client_cleanup", actor_finished_before_cleanup)
                record_property("actor_cancelling_before_client_cleanup", actor.cancelling())
            finally:
                # Only the client's own cancellation. The ASGI actor is never
                # cancelled again by this fixture, even if the assertion is red.
                if not request.done():
                    request.cancel()
                await asyncio.wait({request}, timeout=.05)
                transport_disconnected = True
                embedding_release.set()
                with suppress(asyncio.CancelledError):
                    await request
                if actor is not None:
                    with suppress(asyncio.CancelledError):
                        await actor
                actor_cancel_delivered.set()
                await ready
                await timer
        assert ticks[0] < .15
        assert embedding_finished.is_set()
        assert [item.id for item in engine.list_active_items(role.id)] == [saved.item_id]
        assert documents.read_document(role.id, "MEMORY.md") == original
        assert actor_finished_before_cleanup, "cancelled receive actor survived until client/transport cleanup"

    asyncio.run(run())
