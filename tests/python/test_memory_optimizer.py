import asyncio
from datetime import datetime, timezone

import pytest

from backend.app.memory_optimizer import MemoryOptimizer, MemoryOptimizerWorker, _Optimization, _Record
from backend.app.memory_service import MemoryService, MemoryWorker
from backend.app.memory_store import MemoryStore
from backend.app.models import Message
from backend.app import memory_optimizer as memory_optimizer_module
from backend.app import main
from backend.app.models import ModelConfiguration, RoleInput, RoleProfile
from backend.app.role_store import RoleStore


def _candidate(memory_type: str, summary: str, source: str) -> str:
    return (
        f"\n- [{memory_type}] {summary} (来源：消息 1)\n"
        f"  <!-- source: {source} -->\n"
    )


def test_optimizer_merges_candidates_deduplicates_and_consumes_pending(tmp_path):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    pending = _candidate("preference", "主人喜欢红茶", "source-a")
    pending += _candidate("preference", "主人喜欢 红茶。", "source-b")
    (memory_dir / "PENDING.md").write_text(pending, encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text("# 长期记忆\n", encoding="utf-8")

    optimizer = MemoryOptimizer(tmp_path / "roles")
    assert optimizer.optimize("role-a") is True
    assert optimizer.optimize("role-a") is False

    memory = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert memory.count("主人喜欢") == 1
    assert "source-a" in memory and "source-b" in memory
    assert "source:" not in (memory_dir / "PENDING.md").read_text(encoding="utf-8")


def test_optimizer_keeps_new_pending_candidates_when_snapshot_changes(tmp_path):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    pending_path = memory_dir / "PENDING.md"
    pending_path.write_text(_candidate("fact", "主人住在海边", "old"), encoding="utf-8")

    class CandidateArrivesDuringBuild(MemoryOptimizer):
        injected = False

        def _before_commit(self) -> None:
            if not self.injected:
                self.injected = True
                pending_path.write_text(
                    pending_path.read_text(encoding="utf-8")
                    + _candidate("event", "我们一起看过海上日出", "new"),
                    encoding="utf-8",
                )

    optimizer = CandidateArrivesDuringBuild(tmp_path / "roles")
    assert optimizer.optimize("role-a") is True
    memory = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "主人住在海边" in memory
    assert "一起看过海上日出" in memory
    assert "source:" not in pending_path.read_text(encoding="utf-8")


def test_optimizer_failure_preserves_existing_documents_and_pending(tmp_path, monkeypatch):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    pending = _candidate("fact", "主人住在海边", "source-a")
    pending_path = memory_dir / "PENDING.md"
    memory_path = memory_dir / "MEMORY.md"
    self_path = memory_dir / "SELF.md"
    pending_path.write_text(pending, encoding="utf-8")
    memory_path.write_text("# 长期记忆\n\n- [fact] 旧记忆\n", encoding="utf-8")
    self_path.write_text("# 自我认识\n\n稳定内容\n", encoding="utf-8")
    before = {path: path.read_text(encoding="utf-8") for path in (pending_path, memory_path, self_path)}

    optimizer = MemoryOptimizer(tmp_path / "roles")

    def fail(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(optimizer, "_build_documents", fail)
    with pytest.raises(RuntimeError, match="model unavailable"):
        optimizer.optimize("role-a")
    assert {path: path.read_text(encoding="utf-8") for path in before} == before


def test_optimizer_structured_persistence_failure_restores_markdown_snapshot(tmp_path):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    pending_path = memory_dir / "PENDING.md"
    memory_path = memory_dir / "MEMORY.md"
    self_path = memory_dir / "SELF.md"
    pending_path.write_text(_candidate("fact", "主人住在海边", "source-a"), encoding="utf-8")
    memory_path.write_text("# 长期记忆\n", encoding="utf-8")
    self_path.write_text("# 自我认识\n", encoding="utf-8")
    before = {path: path.read_text(encoding="utf-8") for path in (pending_path, memory_path, self_path)}

    def fail_persistence(role_id, records):
        raise RuntimeError("structured store unavailable")

    optimizer = MemoryOptimizer(tmp_path / "roles", persist_structured=fail_persistence)
    with pytest.raises(RuntimeError, match="structured store unavailable"):
        optimizer.optimize("role-a")
    assert {path: path.read_text(encoding="utf-8") for path in before} == before


def test_optimizer_replays_structured_outbox_after_restart(tmp_path, monkeypatch):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "PENDING.md").write_text(_candidate("fact", "主人住在海边", "source-a"), encoding="utf-8")
    calls = []

    def persist(role_id, records):
        calls.append((role_id, records))

    optimizer = MemoryOptimizer(tmp_path / "roles", persist_structured=persist)
    original_write_state = optimizer._write_state

    def fail_clearing_state(path, state):
        if "structuredPending" not in state and calls:
            raise OSError("simulated crash before outbox acknowledgement")
        original_write_state(path, state)

    monkeypatch.setattr(optimizer, "_write_state", fail_clearing_state)
    with pytest.raises(OSError, match="outbox acknowledgement"):
        optimizer.optimize("role-a")
    assert len(calls) == 1

    monkeypatch.setattr(optimizer, "_write_state", original_write_state)
    assert optimizer.optimize("role-a") is False
    assert len(calls) == 2
    assert calls[1][1][0].sources == ["source-a"]


def test_optimizer_atomic_commit_restores_documents_on_write_failure(tmp_path, monkeypatch):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    pending_path = memory_dir / "PENDING.md"
    memory_path = memory_dir / "MEMORY.md"
    self_path = memory_dir / "SELF.md"
    pending_path.write_text(_candidate("fact", "主人住在海边", "source-a"), encoding="utf-8")
    memory_path.write_text("# 长期记忆\n\n- [fact] 旧记忆\n", encoding="utf-8")
    self_path.write_text("# 自我认识\n\n稳定内容\n", encoding="utf-8")
    before = {path: path.read_text(encoding="utf-8") for path in (pending_path, memory_path, self_path)}
    original_replace = memory_optimizer_module.os.replace
    replacements = 0

    def fail_second_replace(source, target):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("simulated write failure")
        return original_replace(source, target)

    monkeypatch.setattr(memory_optimizer_module.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="simulated write failure"):
        MemoryOptimizer(tmp_path / "roles").optimize("role-a")
    assert {path: path.read_text(encoding="utf-8") for path in before} == before


def test_optimizer_updates_self_without_touching_role_profile(tmp_path):
    roles_root = tmp_path / "roles"
    role_dir = roles_root / "role-a"
    memory_dir = role_dir / "memory"
    memory_dir.mkdir(parents=True)
    (role_dir / "role.json").write_text('{"personality":"calm"}\n', encoding="utf-8")
    (memory_dir / "PENDING.md").write_text(_candidate("preference", "主人喜欢清晨散步", "source-a"), encoding="utf-8")

    optimizer = MemoryOptimizer(roles_root)
    assert optimizer.optimize("role-a") is True
    assert "主人喜欢清晨散步" in (memory_dir / "SELF.md").read_text(encoding="utf-8")
    assert (role_dir / "role.json").read_text(encoding="utf-8") == '{"personality":"calm"}\n'


def test_optimizer_uses_role_scoped_model_result_and_preserves_source_keys(tmp_path):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "PENDING.md").write_text(_candidate("fact", "主人住在海边", "source-a"), encoding="utf-8")
    calls = []

    def consolidate(role_id, existing, pending, history, current_self):
        calls.append((role_id, existing, pending, history, current_self))
        return _Optimization([_Record("fact", "主人住在海边，喜欢海风", ["source-a"])], "我们因海边散步建立了共同回忆。")

    optimizer = MemoryOptimizer(tmp_path / "roles", consolidate)
    assert optimizer.optimize("role-a") is True
    assert calls[0][0] == "role-a"
    assert "主人住在海边，喜欢海风" in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "共同回忆" in (memory_dir / "SELF.md").read_text(encoding="utf-8")


def test_model_consolidation_replaces_existing_record_when_sources_are_merged(tmp_path, monkeypatch):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        "# 长期记忆\n\n- [fact] 主人住在海边\n  <!-- source: source-old -->\n",
        encoding="utf-8",
    )
    (memory_dir / "PENDING.md").write_text(_candidate("fact", "主人喜欢海风", "source-new"), encoding="utf-8")

    def consolidate(role_id, existing, pending, history, current_self):
        return _Optimization([_Record("fact", "主人住在海边并喜欢海风", ["source-old", "source-new"])], "")

    optimizer = MemoryOptimizer(tmp_path / "roles", consolidate)
    assert optimizer.optimize("role-a") is True
    memory = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert memory.count("主人住在海边") == 1
    assert "主人喜欢海风" not in memory


def test_model_consolidation_uses_role_configuration_and_rejects_unknown_sources(tmp_path, monkeypatch):
    role_store = RoleStore(tmp_path / "roles")
    role = role_store.create(RoleInput(name="角色", profile=RoleProfile(profile="设定")))
    configuration = ModelConfiguration(
        providerId="test", provider="custom", baseUrl="https://model.test/v1", model="memory-model", apiKey="secret"
    )
    monkeypatch.setattr(main, "store", role_store)
    monkeypatch.setattr(main.model_configuration_store, "get", lambda configuration_id=None: configuration)

    class Adapter:
        async def stream_messages(self, messages, selected_configuration, *, max_tokens=None):
            assert selected_configuration is configuration
            assert max_tokens == 1200
            yield '{"memories":[{"memoryType":"fact","summary":"主人住在海边","sourceKeys":["source-a"]}],'
            yield '"selfUnderstanding":"我们有共同回忆。"}'

    monkeypatch.setattr(main, "model_adapter", Adapter())
    optimized = main._consolidate_role_memories(
        role.id,
        [],
        [_Record("fact", "主人住在海边", ["source-a"])],
        "",
        "",
    )
    assert optimized.records == [_Record("fact", "主人住在海边", ["source-a"])]
    assert optimized.self_understanding == "我们有共同回忆。"

    source_ref = main._consolidation_source_ref(
        role.id,
        f"consolidation:{role.id}:3-8:digest:message-7:fact",
    )
    assert source_ref.messageIds == ["message-7"]
    assert source_ref.messageRange == (3, 8)

    class HallucinatingAdapter:
        async def stream_messages(self, messages, selected_configuration, *, max_tokens=None):
            yield '{"memories":[{"memoryType":"fact","summary":"推断的内容","sourceKeys":["other-source"]}],'
            yield '"selfUnderstanding":""}'

    monkeypatch.setattr(main, "model_adapter", HallucinatingAdapter())
    with pytest.raises(ValueError, match="无效的记忆来源"):
        main._consolidate_role_memories(
            role.id,
            [],
            [_Record("fact", "主人住在海边", ["source-a"])],
            "",
            "",
        )


def test_optimizer_worker_runs_in_background_and_serializes_roles(tmp_path):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "PENDING.md").write_text(_candidate("fact", "主人住在海边", "source-a"), encoding="utf-8")
    worker = MemoryOptimizerWorker(MemoryOptimizer(tmp_path / "roles"))

    async def run() -> None:
        worker.submit("role-a")
        await worker.drain()

    asyncio.run(run())
    assert "主人住在海边" in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert worker.errors == []


def test_optimizer_worker_close_rejects_new_jobs(tmp_path):
    worker = MemoryOptimizerWorker(MemoryOptimizer(tmp_path / "roles"))

    async def run() -> None:
        await worker.close()
        assert worker.submit("role-a") is False

    asyncio.run(run())


def test_optimizer_worker_resumes_pending_candidates_on_startup(tmp_path):
    memory_dir = tmp_path / "roles" / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "PENDING.md").write_text(_candidate("fact", "待恢复的记忆", "source-a"), encoding="utf-8")
    worker = MemoryOptimizerWorker(MemoryOptimizer(tmp_path / "roles"))

    async def run() -> None:
        assert worker.resume_pending() == 1
        await worker.drain()

    asyncio.run(run())
    assert "待恢复的记忆" in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")


def test_memory_worker_schedules_optimizer_after_turn_maintenance(tmp_path):
    roles_root = tmp_path / "roles"
    memory_dir = roles_root / "role-a" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "PENDING.md").write_text(_candidate("fact", "主人住在海边", "source-a"), encoding="utf-8")
    optimizer_worker = MemoryOptimizerWorker(MemoryOptimizer(roles_root))
    memory_worker = MemoryWorker(MemoryService(MemoryStore(tmp_path / "memory.db")), optimizer=optimizer_worker)
    user = Message(
        id="user-1",
        sessionKey="role:role-a",
        sequence=1,
        role="user",
        content="你好",
        status="completed",
        createdAt=datetime.now(timezone.utc),
    )
    assistant = user.model_copy(update={"id": "assistant-1", "sequence": 2, "role": "assistant", "content": "你好"})

    async def run() -> None:
        memory_worker.submit("role-a", "role:role-a", user, assistant)
        await memory_worker.drain()

    asyncio.run(run())
    assert "主人住在海边" in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
