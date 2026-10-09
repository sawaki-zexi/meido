import os
import asyncio
import json
import inspect
import logging
import uuid
import sqlite3
import secrets
from dataclasses import asdict
from datetime import datetime, timezone
from time import monotonic
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx

from .model_adapter import ModelAdapter, OpenAICompatibleAdapter
from .model_config import ModelConfigurationStore, PROVIDER_PRESETS, public_configuration, validate_model_configuration
from .memory_service import MemoryService, MemoryWorker
from .memory_engine import DefaultMemoryEngine, MemoryMutation, MemoryQuery, MemoryQueryFilters, MemoryScope
from .embeddings import OpenAICompatibleEmbeddingAdapter
from .memory_maintenance import MemoryMaintenance
from .memory_optimizer import MemoryOptimizer, MemoryOptimizerLoop, MemoryOptimizerWorker, MemoryOptimizationResult, MemoryRecord
from .memory_store import MemoryStore
from .memory_events import ConsolidationCommitted, TurnCommitted, MemoryEventBus, MemoryWritten
from .memory_documents import MemoryDocuments
from .owner_knowledge import (
    ApplicationPluginManager,
    FeishuApiConnector,
    FeishuApiError,
    LocalEmbeddingAdapter,
    OAuthCallbackAccessLogFilter,
    OwnerKnowledgePlugin,
    OwnerKnowledgeScheduler,
    SyncReport,
    OwnerKnowledgeStore,
    OwnerKnowledgeTool,
    RoleBoundOwnerKnowledgeReader,
)
from .models import AgentRun, MemoryAdminUpdateInput, MemoryBatchDeleteInput, MemoryList, MemoryItem, MemorySourceRef, Message, RememberMemoryInput, UpdateMemoryInput, ModelConfiguration, ModelConfigurationInput, ProviderPresetList, RoleInput, RoleList, RoleResponse, RoleUpdateInput, SendMessageInput, SessionResponse
from .models import RoleModelConfigurationInput
from .role_store import RoleStore
from .storage import initialize_databases, resolve_owner_knowledge_database
from .session_manager import SessionManager
from .session_store import SessionStore
from .agent_runtime import (
    ActiveRunError,
    CapabilityRegistry,
    AgentEndEvent,
    AssistantMessage,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    MeidoProvider,
    MemoryRecallTool,
    RoleScopedMemoryReadPort,
    PluginRegistry,
    RuntimeManager,
    ShellTool,
    summarize_tool_arguments,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolResultMessage,
)
from .agent_runtime.capabilities import CapabilityResolution
from .agent_runtime.skills import SkillRegistry

logging.getLogger("uvicorn.access").addFilter(OAuthCallbackAccessLogFilter())
logger = logging.getLogger(__name__)

roles_root = Path(os.getenv("MEIDO_ROLES_DIR", "roles"))
project_root = Path(os.getenv("MEIDO_PROJECT_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()
data_root = Path(os.getenv("MEIDO_DATA_DIR", ".data"))
MAX_ROLE_AVATAR_BYTES = 10 * 1024 * 1024
ROLE_AVATAR_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp"}
initialize_databases(data_root)
store = RoleStore(roles_root)
session_store = SessionStore(data_root / "sessions.db")
session_manager = SessionManager(store, session_store)
memory_store = MemoryStore(data_root / "memory2.db")
memory_event_bus = MemoryEventBus()
model_adapter: ModelAdapter = OpenAICompatibleAdapter()
model_configuration_store = ModelConfigurationStore(data_root / "model-config.json")
plugin_registry = PluginRegistry()
owner_knowledge_store = OwnerKnowledgeStore(resolve_owner_knowledge_database(project_root, data_root))
async def _refresh_feishu_access_token() -> None:
    refresh = owner_knowledge_store.get_state("feishu_refresh_token")
    app_id = owner_knowledge_store.get_state("feishu_app_id") or os.getenv("MEIDO_FEISHU_APP_ID", "").strip()
    app_secret = owner_knowledge_store.get_state("feishu_app_secret") or os.getenv("MEIDO_FEISHU_APP_SECRET", "").strip()
    if not refresh or not app_id or not app_secret:
        raise RuntimeError("飞书授权已过期，请重新连接")
    payload = await owner_knowledge_connector.refresh_token(refresh, app_id, app_secret)
    owner_knowledge_store.set_state("feishu_access_token", str(payload.get("access_token", "")))
    owner_knowledge_store.set_state("feishu_refresh_token", str(payload.get("refresh_token", refresh)))


owner_knowledge_connector = FeishuApiConnector(
    lambda: owner_knowledge_store.get_state("feishu_access_token"),
    refresh_token=_refresh_feishu_access_token,
)
owner_knowledge_plugin = OwnerKnowledgePlugin(
    owner_knowledge_store,
    owner_knowledge_connector,
    LocalEmbeddingAdapter(os.getenv("MEIDO_OWNER_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")),
)
application_plugin_manager = ApplicationPluginManager()
application_plugin_manager.register("owner-knowledge", owner_knowledge_plugin)


def _embedding_configuration(role_id: str):
    role = store.get(role_id)
    return model_configuration_store.get(role.modelConfigurationId) if role and role.modelConfigurationId else model_configuration_store.get()


def _role_model_configuration(role_id: str):
    role = store.get(role_id)
    return model_configuration_store.get(role.modelConfigurationId) if role and role.modelConfigurationId else model_configuration_store.get()


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _complete_model_json(messages: list[dict[str, str]], configuration, *, max_tokens: int) -> object:
    if configuration is None:
        raise RuntimeError("memory model is not configured")

    async def collect() -> str:
        complete = getattr(model_adapter, "complete_messages", None)
        if complete is not None:
            return await complete(messages, configuration, max_tokens=max_tokens)
        parts: list[str] = []
        async for delta in model_adapter.stream_messages(messages, configuration, max_tokens=max_tokens):
            parts.append(delta)
        return "".join(parts)

    text = asyncio.run(asyncio.wait_for(collect(), timeout=8.0)).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    return json.loads(text)


def _post_response_provider(role_id, session_key, user_message, assistant_message, active):
    result = _complete_model_json([
        {"role": "system", "content": (
            "从本轮用户和助手消息中提取值得长期保存的隐式记忆。只返回 JSON："
            "{\"memories\":[{\"memoryType\":\"fact|preference|profile|procedure|event\","
            "\"summary\":string,\"supersedeKey\":string|null}]}。"
            "不要记录一次性闲聊、助手推测或执行结果。"
        )},
        {"role": "user", "content": json.dumps({"user": user_message.content, "assistant": assistant_message.content, "activeMemories": active}, ensure_ascii=False)},
    ], _role_model_configuration(role_id), max_tokens=700)
    if not isinstance(result, dict) or not isinstance(result.get("memories"), list):
        raise ValueError("post-response JSON schema invalid")
    return result["memories"]


def _consolidation_provider(source_key: str, window: list) -> object:
    parts = source_key.split(":", 2)
    role_id = parts[1] if len(parts) > 1 else ""
    result = _complete_model_json([
        {"role": "system", "content": (
            "用 LLM 整理对话窗口。只返回 JSON："
            "{\"history\":string,\"recentContext\":string,\"pendingItems\":[{\"memoryType\":string,\"summary\":string,\"sourceKey\":string}]}。"
            "history 和 recentContext 必须是可直接写入 Markdown 的简洁内容；sourceKey 必须以前缀 consolidation: 开头，禁止虚构事实。"
        )},
        {"role": "user", "content": json.dumps({"sourceKey": source_key, "messages": [{"role": m.role, "content": m.content, "sequence": m.sequence} for m in window]}, ensure_ascii=False)},
    ], _role_model_configuration(role_id), max_tokens=900)
    if not isinstance(result, dict) or not isinstance(result.get("pendingItems", result.get("memories")), list):
        raise ValueError("consolidation JSON schema invalid")
    normalized: list[dict[str, str]] = []
    raw_items = result.get("pendingItems", result.get("memories"))
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        memory_type, summary, item_source = item.get("memoryType"), item.get("summary"), item.get("sourceKey")
        if isinstance(memory_type, str) and memory_type.strip() in {"fact", "preference", "profile", "procedure", "event"} and isinstance(summary, str) and isinstance(item_source, str) and summary.strip() and item_source.startswith(source_key):
            normalized.append({"memoryType": memory_type.strip(), "summary": summary.strip(), "sourceKey": item_source.strip()})
    return {
        "pendingItems": normalized,
        "history": result.get("history", "") if isinstance(result.get("history", ""), str) else "",
        "recentContext": result.get("recentContext", "") if isinstance(result.get("recentContext", ""), str) else "",
    }


def _hyde_provider(role_id: str, query: str) -> list[str]:
    result = _complete_model_json([
        {"role": "system", "content": "为记忆检索生成最多两个简短假设查询，分别关注事件和一般事实。只返回 JSON：{\"queries\":[string,string]}。"},
        {"role": "user", "content": query},
    ], _role_model_configuration(role_id), max_tokens=120)
    if not isinstance(result, dict) or not isinstance(result.get("queries"), list):
        raise ValueError("HyDE JSON schema invalid")
    return [value.strip() for value in result["queries"] if isinstance(value, str) and value.strip()][:2]


def _consolidate_role_memories(
    role_id: str,
    existing: list[MemoryRecord],
    pending: list[MemoryRecord],
    history: str,
    current_self: str,
) -> MemoryOptimizationResult:
    role = store.get(role_id)
    if role is None:
        raise ValueError("角色不存在")
    configuration = model_configuration_store.get(role.modelConfigurationId) if role.modelConfigurationId else model_configuration_store.get()
    if configuration is None:
        raise RuntimeError("长期记忆归并需要可用的模型连接")
    payload = {
        "existingMemories": [
            {"memoryType": item.memory_type, "summary": item.summary, "sourceKeys": item.sources}
            for item in existing
        ],
        "pendingCandidates": [
            {"memoryType": item.memory_type, "summary": item.summary, "sourceKeys": item.sources}
            for item in pending
        ],
        "recentHistory": history[-6000:],
        "currentSelfUnderstanding": current_self,
    }
    request = [
        {
            "role": "system",
            "content": (
                "你负责归并一个角色的长期记忆。只根据输入内容总结，不得推测或添加事实。"
                "合并重复或相近事实，保留不同事实；保留每条记忆原有的 sourceKeys，禁止新造来源。"
                "selfUnderstanding 只描述关系认识和共同经历，不写入或建议修改角色性格、行为规则或回复限制。"
                "只返回 JSON：{\"memories\":[{\"memoryType\":string,\"summary\":string,\"sourceKeys\":string[]}],"
                "\"selfUnderstanding\":string}。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    async def collect() -> str:
        parts: list[str] = []
        async for delta in model_adapter.stream_messages(request, configuration, max_tokens=1200):
            parts.append(delta)
        return "".join(parts)

    text = asyncio.run(collect())
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ValueError("Optimizer 返回内容不是有效 JSON") from error
    if not isinstance(result, dict) or not isinstance(result.get("memories"), list):
        raise ValueError("Optimizer 返回的记忆格式无效")
    allowed_sources = {source for item in [*existing, *pending] for source in item.sources}
    records: list[MemoryRecord] = []
    for item in result["memories"]:
        if not isinstance(item, dict):
            raise ValueError("Optimizer 返回的记忆条目格式无效")
        memory_type = item.get("memoryType")
        summary = item.get("summary")
        sources = item.get("sourceKeys")
        if not isinstance(memory_type, str) or not memory_type.strip() or not isinstance(summary, str) or not summary.strip() or not isinstance(sources, list):
            raise ValueError("Optimizer 返回的记忆条目缺少必要字段")
        if any(not isinstance(source, str) or source not in allowed_sources for source in sources):
            raise ValueError("Optimizer 返回了无效的记忆来源")
        records.append(MemoryRecord(memory_type.strip(), summary.strip(), list(dict.fromkeys(sources))))
    self_understanding = result.get("selfUnderstanding")
    if not isinstance(self_understanding, str):
        raise ValueError("Optimizer 返回的自我认识格式无效")
    return MemoryOptimizationResult(MemoryOptimizer._merge_records([], records), self_understanding[:2000])


def _persist_consolidated_memories(role_id: str, records: list[MemoryRecord]) -> None:
    """Mirror optimizer output into the role-scoped structured memory store."""
    writes = [
        (record.memory_type, record.summary, _consolidation_source_ref(role_id, source_key))
        for record in records
        for source_key in dict.fromkeys(record.sources)
    ]
    for item in memory_store.consolidate_batch(role_id, writes):
        memory_service.index_embedding(role_id, item)


def _consolidation_source_ref(role_id: str, source_key: str) -> MemorySourceRef:
    """Decode the maintenance source key into a navigable consolidation origin."""
    parts = source_key.split(":")
    message_ids: list[str] = []
    message_range: tuple[int, int] | None = None
    if len(parts) >= 6 and parts[0] == "consolidation" and parts[1] == role_id:
        try:
            start, end = (int(value) for value in parts[2].split("-", 1))
            message_range = (start, end)
            message_ids = [parts[4]]
        except (TypeError, ValueError):
            message_ids = []
            message_range = None
    return MemorySourceRef(
        kind="consolidation",
        sessionKey=f"role:{role_id}",
        messageIds=message_ids,
        messageRange=message_range,
        stableSourceKey=source_key,
    )


def _consume_consolidation_event(event: ConsolidationCommitted) -> None:
    records = [
        (
            candidate.memory_type,
            candidate.summary,
            MemorySourceRef(
                kind="consolidation",
                sessionKey=event.session_key,
                messageIds=list(event.message_ids),
                messageRange=event.message_range,
                stableSourceKey=candidate.source_key,
            ),
            None,
        )
        for candidate in event.candidates
    ]
    items = memory_store.consume_consolidation_event(event.role_id, event.source_key, records)
    for item in items:
        memory_service.index_embedding(event.role_id, item)
    memory_event_bus.publish(MemoryWritten(
        event.role_id,
        event.session_key,
        event.source_key,
        tuple(item.id for item in items),
        "consolidation",
    ))


def _observe_consolidation_event(event: ConsolidationCommitted) -> None:
    """Publish the commit boundary before consumers run, then consume it."""
    memory_event_bus.publish(event)
    try:
        _consume_consolidation_event(event)
    except Exception as error:
        memory_event_bus.publish(MemoryWritten(
            event.role_id,
            event.session_key,
            event.source_key,
            operation="consolidation",
            error=str(error),
        ))
        raise


embedding_provider = OpenAICompatibleEmbeddingAdapter(_embedding_configuration)
memory_service = MemoryService(
    memory_store,
    embedding_provider,
    _post_response_provider,
    memory_event_bus,
    implicit_extraction_enabled=_env_bool("MEIDO_MEMORY_IMPLICIT_EXTRACTION_ENABLED", True),
)
memory_engine = DefaultMemoryEngine(memory_service, role_exists=lambda role_id: store.get(role_id) is not None, event_bus=memory_event_bus, hyde_provider=_hyde_provider)
memory_optimizer_enabled = os.getenv("MEIDO_MEMORY_OPTIMIZER_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
try:
    memory_optimizer_interval = float(os.getenv("MEIDO_MEMORY_OPTIMIZER_INTERVAL_SECONDS", str(MemoryOptimizer.DEFAULT_INTERVAL_SECONDS)))
except ValueError:
    memory_optimizer_interval = MemoryOptimizer.DEFAULT_INTERVAL_SECONDS

memory_optimizer_worker = MemoryOptimizerWorker(
    MemoryOptimizer(
        roles_root,
        lambda role_id, existing, pending, history, current_self: _consolidate_role_memories(
            role_id,
            existing,
            pending,
            history,
            current_self,
        ),
    )
)
memory_optimizer_loop = MemoryOptimizerLoop(
    memory_optimizer_worker,
    roles_root,
    enabled=memory_optimizer_enabled,
    interval_seconds=memory_optimizer_interval,
    model_available=lambda role_id: (
        (role := store.get(role_id)) is not None
        and (
            model_configuration_store.get(role.modelConfigurationId)
            if role.modelConfigurationId
            else model_configuration_store.get()
        ) is not None
    ),
)
memory_maintenance = MemoryMaintenance(
    roles_root,
    session_store,
    _observe_consolidation_event,
    _consolidation_provider,
    role_exists=lambda role_id: store.get(role_id) is not None,
)
memory_worker = MemoryWorker(
    memory_service,
    memory_maintenance,
    memory_optimizer_worker,
)
connection_test_adapter = OpenAICompatibleAdapter(timeout=20)
runtime_manager = RuntimeManager(session_store)
role_locks = runtime_manager.role_locks


def _runtime_role_lock(role_id: str) -> asyncio.Lock:
    """Keep test/application store rebinding behind the RuntimeManager seam."""

    runtime_manager.role_locks = role_locks
    runtime_manager.sessions = session_store
    return runtime_manager.lock_for(role_id)


def _current_memory_engine() -> DefaultMemoryEngine:
    global memory_engine
    if memory_engine.service is not memory_service or memory_engine.store is not memory_store:
        memory_engine = DefaultMemoryEngine(
            memory_service,
            role_exists=lambda role_id: store.get(role_id) is not None,
            event_bus=memory_event_bus,
            hyde_provider=_hyde_provider,
        )
    return memory_engine


async def _mutate_and_sync_memory(role_id: str, operation):
    if memory_worker.role_deletion_blocked(role_id):
        raise HTTPException(status_code=409, detail="角色正在删除，请稍后再试")
    async with memory_worker.role_lock_for(role_id):
        if memory_worker.role_deletion_blocked(role_id):
            raise HTTPException(status_code=409, detail="角色正在删除，请稍后再试")
        return await _mutate_and_sync_memory_locked(role_id, operation)


async def _mutate_and_sync_memory_locked(role_id: str, operation):
    snapshot = memory_store.snapshot_role(role_id)
    documents = MemoryDocuments(roles_root)
    memory_dir = documents.memory_dir(role_id)
    document_paths = (memory_dir / "MEMORY.md", memory_dir / "PENDING.md")
    previous_documents = {
        path: path.read_text(encoding="utf-8") if path.exists() else None
        for path in document_paths
    }
    def restore(error: Exception) -> None:
        try:
            memory_store.restore_role(role_id, snapshot)
            writes = {path: content for path, content in previous_documents.items() if content is not None}
            for path, content in previous_documents.items():
                if content is None:
                    path.unlink(missing_ok=True)
            if writes:
                MemoryOptimizer.commit_documents(writes)
        except Exception as restore_error:
            raise HTTPException(status_code=500, detail=f"记忆同步失败，且回滚失败：{restore_error}") from error

    try:
        result = operation()
        if inspect.isawaitable(result):
            result = await result
    except Exception as error:
        restore(error)
        raise
    try:
        documents.sync_structured_memory(role_id, memory_store.list_all(role_id))
    except Exception as error:
        restore(error)
        raise HTTPException(status_code=500, detail=f"记忆同步失败，原记忆已恢复：{error}") from error
    return result


def _role_memory_context(role_id: str) -> str:
    documents = MemoryDocuments(roles_root)
    try:
        documents.memory_dir(role_id)
    except ValueError:
        return ""
    sections = []
    for filename in ("SELF.md", "MEMORY.md", "RECENT_CONTEXT.md"):
        try:
            content = documents.read_document(role_id, filename).strip()
        except (OSError, RuntimeError, ValueError):
            continue
        if content:
            sections.append(f"[{filename}]\n{content}")
    return "\n\n".join(sections)


def _role_owner_understanding_memory(role_id: str) -> str:
    sections = [_role_memory_context(role_id)]
    records = memory_store.list_all(role_id)
    if records:
        summaries = [
            f"- [{item.memoryType}] {item.summary}"
            for item in records
            if getattr(item, "status", "active") == "active"
        ]
        if summaries:
            sections.append("[结构化角色记忆]\n" + "\n".join(summaries[:120]))
    return "\n\n".join(part for part in sections if part)[:12000]


async def _memory_engine_item_mutation(mutation: MemoryMutation) -> MemoryItem:
    result = await _current_memory_engine().mutate(mutation)
    item = result.raw.get("item")
    if not result.accepted or not isinstance(item, dict):
        raise KeyError(mutation.ids[0] if mutation.ids else "memory")
    return MemoryItem.model_validate(item)


async def _memory_engine_status_mutation(mutation: MemoryMutation) -> MemoryItem:
    result = await _current_memory_engine().mutate(mutation)
    items = result.raw.get("items", [])
    if not result.accepted or not items or not isinstance(items[0], dict):
        raise KeyError(mutation.ids[0] if mutation.ids else "memory")
    return MemoryItem.model_validate(items[0])


async def _memory_engine_delete(mutation: MemoryMutation) -> None:
    result = await _current_memory_engine().mutate(mutation)
    if not result.accepted:
        raise KeyError(result.missing_ids[0] if result.missing_ids else "memory")


async def _close_memory_workers() -> None:
    await owner_knowledge_scheduler.close()
    await owner_knowledge_plugin.close()
    await memory_optimizer_loop.close()
    await memory_worker.close()
    await memory_optimizer_worker.drain()


async def _start_memory_workers() -> None:
    memory_worker.start()
    store.cleanup_staged_role_files()
    memory_worker.errors.extend(store.recovery_errors)
    for role_id in store.pending_role_deletions():
        try:
            await memory_worker.begin_role_deletion(role_id)
            session_store.delete_role_session(role_id)
            memory_store.delete_role(role_id)
            for marker in store.root.glob(f".deleting-{role_id}-*.json"):
                store.complete_role_deletion(marker)
        except Exception as error:
            memory_worker.errors.append(f"{role_id}: interrupted deletion recovery failed: {error}")
        finally:
            memory_worker.end_role_deletion(role_id, deleted=True)
    await asyncio.to_thread(memory_maintenance.resume_pending)
    memory_optimizer_loop.start()
    owner_startup_report = await owner_knowledge_plugin.startup()
    if owner_startup_report and (owner_startup_report.created or owner_startup_report.updated or owner_startup_report.deleted):
        _queue_owner_understanding_refresh(owner_startup_report)
    owner_knowledge_scheduler.start()


app = FastAPI(title="Meido API", on_startup=[_start_memory_workers], on_shutdown=[_close_memory_workers])
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5288", "http://localhost:5288"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


@app.get("/api/owner-knowledge")
def get_owner_knowledge_status() -> dict[str, object]:
    status = owner_knowledge_plugin.status()
    return {
        "enabled": status.enabled,
        "connected": status.connected,
        "oauthConfigured": bool(
            (owner_knowledge_store.get_state("feishu_app_id") or os.getenv("MEIDO_FEISHU_APP_ID"))
            and (owner_knowledge_store.get_state("feishu_app_secret") or os.getenv("MEIDO_FEISHU_APP_SECRET"))
        ),
        "embeddingReady": getattr(owner_knowledge_plugin.embedder, "ready", False),
        "appId": owner_knowledge_store.get_state("feishu_app_id"),
        "redirectUri": os.getenv(
            "MEIDO_FEISHU_REDIRECT_URI",
            "http://127.0.0.1:4288/api/owner-knowledge/oauth/callback",
        ),
        "lastError": status.last_error,
        "lastSync": asdict(status.last_sync) if status.last_sync else None,
    }


@app.get("/api/plugins")
def list_application_plugins() -> dict[str, object]:
    return {"plugins": application_plugin_manager.list()}


@app.put("/api/owner-knowledge/oauth-config")
def save_owner_knowledge_oauth_config(data: dict[str, object]) -> dict[str, object]:
    app_id = data.get("appId")
    app_secret = data.get("appSecret")
    if not isinstance(app_id, str) or not app_id.strip():
        raise HTTPException(status_code=422, detail="飞书应用 ID 不能为空")
    if isinstance(app_secret, str) and app_secret.strip():
        owner_knowledge_store.set_state("feishu_app_secret", app_secret.strip())
    elif not owner_knowledge_store.get_state("feishu_app_secret"):
        raise HTTPException(status_code=422, detail="飞书应用 Secret 不能为空")
    owner_knowledge_store.set_state("feishu_app_id", app_id.strip())
    return {"oauthConfigured": True, "appId": app_id.strip(), "appSecretConfigured": True}


@app.post("/api/owner-knowledge/authorize")
def authorize_owner_knowledge() -> dict[str, str]:
    from .owner_knowledge import feishu_authorization_url

    app_id = owner_knowledge_store.get_state("feishu_app_id") or os.getenv("MEIDO_FEISHU_APP_ID", "").strip()
    redirect_uri = os.getenv(
        "MEIDO_FEISHU_REDIRECT_URI",
        "http://127.0.0.1:4288/api/owner-knowledge/oauth/callback",
    ).strip()
    app_secret = owner_knowledge_store.get_state("feishu_app_secret") or os.getenv("MEIDO_FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise HTTPException(status_code=409, detail="尚未配置飞书自建应用 ID 和 Secret")
    state = secrets.token_urlsafe(32)
    owner_knowledge_store.set_state("oauth_state", state)
    try:
        return {"authorizationUrl": feishu_authorization_url(app_id, redirect_uri, state)}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/owner-knowledge/oauth/callback")
async def owner_knowledge_oauth_callback(code: str = "", state: str = "", error: str = "") -> Response:
    if error:
        raise HTTPException(status_code=400, detail="飞书授权未完成")
    if not code or not state or state != owner_knowledge_store.get_state("oauth_state"):
        raise HTTPException(status_code=400, detail="飞书授权状态无效或已过期")
    owner_knowledge_store.set_state("oauth_state", "")
    app_id = owner_knowledge_store.get_state("feishu_app_id") or os.getenv("MEIDO_FEISHU_APP_ID", "").strip()
    app_secret = owner_knowledge_store.get_state("feishu_app_secret") or os.getenv("MEIDO_FEISHU_APP_SECRET", "").strip()
    redirect_uri = os.getenv(
        "MEIDO_FEISHU_REDIRECT_URI",
        "http://127.0.0.1:4288/api/owner-knowledge/oauth/callback",
    ).strip()
    try:
        payload = await owner_knowledge_connector.exchange_code(code, app_id, app_secret, redirect_uri)
    except FeishuApiError as error:
        logger.warning("飞书 OAuth 令牌交换失败：%s", error)
        raise HTTPException(status_code=502, detail=str(error)) from error
    except Exception as error:
        logger.error("飞书 OAuth 令牌交换异常：%s", type(error).__name__)
        raise HTTPException(status_code=502, detail="飞书授权交换失败") from error
    try:
        access_token = str(payload.get("access_token", "")).strip()
        if not access_token:
            raise RuntimeError("飞书响应缺少用户访问令牌")
        owner_knowledge_store.set_state("feishu_access_token", access_token)
        refresh_token = str(payload.get("refresh_token", "")).strip()
        if refresh_token:
            owner_knowledge_store.set_state("feishu_refresh_token", refresh_token)
        expires_in = payload.get("expires_in", 0)
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            expires_at = datetime.now(timezone.utc).timestamp() + expires_in
            owner_knowledge_store.set_state("feishu_token_expires_at", str(expires_at))
    except Exception as error:
        logger.error("飞书 OAuth 令牌保存异常：%s", type(error).__name__)
        raise HTTPException(status_code=502, detail="飞书授权成功，但本地凭据保存失败，请检查系统凭据库") from error
    return Response(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>飞书连接成功</title>"
        "<body><main><h1>飞书已连接</h1><p>可以关闭此页面，返回 Meido 启用主人资料插件。</p></main></body></html>",
        media_type="text/html",
    )


@app.post("/api/owner-knowledge/enable")
async def enable_owner_knowledge() -> dict[str, object]:
    try:
        report = await application_plugin_manager.enable("owner-knowledge")
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if report.created or report.updated or report.deleted:
        _queue_owner_understanding_refresh(report)
    return get_owner_knowledge_status()


@app.post("/api/owner-knowledge/pause")
async def pause_owner_knowledge() -> dict[str, object]:
    await application_plugin_manager.pause("owner-knowledge")
    return get_owner_knowledge_status()


@app.post("/api/owner-knowledge/sync")
async def sync_owner_knowledge() -> dict[str, object]:
    try:
        report = await owner_knowledge_plugin.sync()
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if report.created or report.updated or report.deleted:
        _queue_owner_understanding_refresh(report)
    return {**get_owner_knowledge_status(), "report": asdict(report)}


@app.post("/api/owner-knowledge/disconnect")
async def disconnect_owner_knowledge() -> dict[str, object]:
    await application_plugin_manager.pause("owner-knowledge")
    owner_knowledge_store.clear_source()
    return get_owner_knowledge_status()


@app.get("/api/owner-knowledge/documents")
def list_owner_knowledge_documents() -> dict[str, object]:
    return {"documents": owner_knowledge_store.list_document_statuses()}


@app.get("/api/owner-knowledge/sync-runs")
def list_owner_knowledge_sync_runs() -> dict[str, object]:
    return {"runs": owner_knowledge_store.list_sync_runs()}


@app.get("/api/roles/{role_id}/owner-understanding")
def get_owner_understanding(role_id: str) -> dict[str, object]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return {"understanding": owner_knowledge_store.get_understanding(role_id)}


def _queue_owner_understanding_refresh(report: SyncReport | None = None, *, full_refresh: bool = False) -> None:
    for role in store.list():
        owner_knowledge_plugin.queue_understanding_refresh(
            role.id,
            role,
            model_adapter,
            _role_model_configuration(role.id),
            _role_owner_understanding_memory(role.id),
            changed_document_ids=report.changed_document_ids if report else (),
            deleted_document_titles=report.deleted_document_titles if report else (),
            full_refresh=full_refresh,
        )


def _owner_knowledge_changed(
    role_id: str | None,
    changed_document_ids: tuple[str, ...],
    deleted_document_titles: tuple[str, ...],
    full_refresh: bool,
) -> None:
    if role_id is None:
        _queue_owner_understanding_refresh(
            SyncReport(
                trigger="scheduled",
                complete=True,
                changed_document_ids=changed_document_ids,
                deleted_document_titles=deleted_document_titles,
            ),
            full_refresh=full_refresh,
        )
        return
    role = store.get(role_id)
    if role is None:
        return
    job = owner_knowledge_store.understanding_job(role_id)
    payload, job_full_refresh = job[:2] if job else ({}, False)
    owner_knowledge_plugin.queue_understanding_refresh(
        role_id,
        role,
        model_adapter,
        _role_model_configuration(role_id),
        _role_owner_understanding_memory(role_id),
        changed_document_ids=payload.get("changedDocumentIds", changed_document_ids),
        deleted_document_titles=payload.get("deletedDocumentTitles", deleted_document_titles),
        full_refresh=full_refresh or job_full_refresh,
    )


owner_knowledge_scheduler = OwnerKnowledgeScheduler(owner_knowledge_plugin, _owner_knowledge_changed)


@app.post("/api/roles/{role_id}/owner-understanding/regenerate")
async def regenerate_owner_understanding(role_id: str) -> dict[str, object]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if not owner_knowledge_plugin.status().enabled:
        raise HTTPException(status_code=409, detail="主人资料插件已暂停")
    try:
        owner_knowledge_store.enqueue_understanding(role_id, {"kind": "manual"}, full_refresh=True)
        await owner_knowledge_plugin.generate_understanding(
            store.get(role_id), model_adapter, _role_model_configuration(role_id), _role_owner_understanding_memory(role_id)
        )
    except Exception as error:
        raise HTTPException(status_code=502, detail="角色理解生成失败，已保留上一版本") from error
    return {"understanding": owner_knowledge_store.get_understanding(role_id)}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/model/providers", response_model=ProviderPresetList)
def list_model_providers() -> ProviderPresetList:
    return ProviderPresetList(providers=PROVIDER_PRESETS)


@app.get("/api/model/configuration")
def get_model_configuration() -> dict[str, object]:
    return {"configuration": public_configuration(model_configuration_store.get())}


@app.get("/api/model/configurations")
def list_model_configurations() -> dict[str, object]:
    active_id = model_configuration_store.active_id()
    return {
        "activeId": active_id,
        "configurations": [
            {**public_configuration(item), "active": item.id == active_id}
            for item in model_configuration_store.list()
        ],
    }


def _configuration_with_saved_key(data: ModelConfigurationInput) -> ModelConfigurationInput:
    if data.apiKey:
        return data
    current = model_configuration_store.get()
    if current and (current.providerId, current.provider, current.baseUrl.rstrip("/")) == (
        data.providerId,
        data.provider,
        data.baseUrl.rstrip("/"),
    ):
        return data.model_copy(update={"apiKey": current.apiKey})
    return data


def _safe_model_error(error: Exception, api_key: str = "") -> str:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        message = {
            401: "认证失败 (HTTP 401)：请检查 API Key 是否有效，以及是否属于当前服务商账号。",
            403: "服务商拒绝了请求 (HTTP 403)：请检查账号权限和可用额度。",
            404: "服务地址或模型不存在 (HTTP 404)：请检查 API 地址和模型 ID。",
            429: "请求过于频繁或额度不足 (HTTP 429)：请稍后重试并检查服务商额度。",
        }.get(status, f"模型服务请求失败 (HTTP {status})")
        if 500 <= status <= 599:
            message = f"模型服务暂时不可用 (HTTP {status})，请稍后重试。"
    else:
        message = str(error).strip() or "模型服务请求失败"
    if api_key:
        message = message.replace(api_key, "***")
    return message[:500]


@app.put("/api/model/configuration")
def save_model_configuration(data: ModelConfigurationInput) -> dict[str, object]:
    try:
        candidate = _configuration_with_saved_key(data)
        configuration = model_configuration_store.save(candidate)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="模型配置保存失败，当前生效配置未改变") from error
    return {"configuration": public_configuration(configuration)}


@app.post("/api/model/configurations")
def create_model_configuration(data: ModelConfigurationInput) -> dict[str, object]:
    try:
        candidate = _configuration_with_saved_key(data)
        configuration = model_configuration_store.create(candidate)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="模型配置保存失败") from error
    return {"configuration": public_configuration(configuration)}


@app.put("/api/model/configurations/{configuration_id}")
def update_model_configuration(configuration_id: str, data: ModelConfigurationInput) -> dict[str, object]:
    try:
        current = model_configuration_store.get(configuration_id)
        if current and not data.apiKey:
            data = data.model_copy(update={"apiKey": current.apiKey})
        configuration = model_configuration_store.update(configuration_id, data)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="模型配置不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="模型配置保存失败") from error
    return {"configuration": public_configuration(configuration)}


@app.post("/api/model/configurations/{configuration_id}/activate")
def activate_model_configuration(configuration_id: str) -> dict[str, object]:
    try:
        configuration = model_configuration_store.activate(configuration_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="模型配置不存在") from error
    return {"configuration": public_configuration(configuration)}


@app.delete("/api/model/configurations/{configuration_id}")
def delete_model_configuration(configuration_id: str) -> dict[str, object]:
    try:
        active_id = model_configuration_store.delete(configuration_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="模型配置不存在") from error
    return {"activeId": active_id}


@app.post("/api/model/configuration/test")
async def test_model_configuration(data: ModelConfigurationInput) -> dict[str, object]:
    try:
        candidate = _configuration_with_saved_key(data)
        validate_model_configuration(candidate)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    configuration = ModelConfiguration.model_validate(candidate.model_dump())
    started_at = monotonic()
    try:
        async for _ in connection_test_adapter.stream_messages(
            [{"role": "user", "content": "ping"}], configuration, max_tokens=8
        ):
            pass
    except Exception as error:
        return {"ok": False, "message": _safe_model_error(error, configuration.apiKey)}
    return {
        "ok": True,
        "message": "连接成功",
        "latencyMs": round((monotonic() - started_at) * 1000),
    }


@app.get("/api/roles", response_model=RoleList)
def list_roles() -> RoleList:
    return RoleList(roles=store.list())


@app.post("/api/roles", response_model=RoleResponse, status_code=201)
def create_role(data: RoleInput) -> RoleResponse:
    try:
        return RoleResponse(role=store.create(data))
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"角色保存失败：{error}") from error


@app.get("/api/roles/{role_id}", response_model=RoleResponse)
def get_role(role_id: str) -> RoleResponse:
    role = store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return RoleResponse(role=role)


@app.put("/api/roles/{role_id}", response_model=RoleResponse)
def update_role(role_id: str, data: RoleUpdateInput) -> RoleResponse:
    try:
        return RoleResponse(role=store.update(role_id, data))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"角色保存失败：{error}") from error


def _avatar_signature_matches(content: bytes, media_type: str) -> bool:
    if media_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    return False


@app.post("/api/roles/{role_id}/avatar", response_model=RoleResponse)
async def upload_role_avatar(role_id: str, request: Request) -> RoleResponse:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in ROLE_AVATAR_MEDIA_TYPES:
        raise HTTPException(status_code=415, detail="仅支持 PNG、JPEG 或 WebP 图片")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_ROLE_AVATAR_BYTES:
            raise HTTPException(status_code=413, detail="头像图片不能超过 10 MB")
        content.extend(chunk)
    if not _avatar_signature_matches(content, media_type):
        raise HTTPException(status_code=415, detail="图片内容与文件格式不匹配")
    try:
        return RoleResponse(role=store.set_avatar(role_id, bytes(content), media_type))
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"头像保存失败：{error}") from error


@app.get("/api/roles/{role_id}/avatar")
def get_role_avatar(role_id: str) -> Response:
    role = store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if not role.avatarUrl or not role.avatarMediaType:
        raise HTTPException(status_code=404, detail="角色没有头像")
    try:
        content = store.avatar_path(role_id).read_bytes()
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="角色没有头像") from error
    return Response(content, media_type=role.avatarMediaType, headers={"Cache-Control": "no-cache"})


@app.post("/api/roles/{role_id}/avatar-original", response_model=RoleResponse)
async def upload_role_avatar_original(role_id: str, request: Request) -> RoleResponse:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in ROLE_AVATAR_MEDIA_TYPES:
        raise HTTPException(status_code=415, detail="仅支持 PNG、JPEG 或 WebP 图片")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_ROLE_AVATAR_BYTES:
            raise HTTPException(status_code=413, detail="头像图片不能超过 10 MB")
        content.extend(chunk)
    if not _avatar_signature_matches(content, media_type):
        raise HTTPException(status_code=415, detail="图片内容与文件格式不匹配")
    try:
        return RoleResponse(role=store.set_avatar(role_id, bytes(content), media_type, original=True))
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"原图保存失败：{error}") from error


@app.get("/api/roles/{role_id}/avatar-original")
def get_role_avatar_original(role_id: str) -> Response:
    role = store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    media_type = role.avatarOriginalMediaType or role.avatarMediaType
    path = store.avatar_original_path(role_id) if role.avatarOriginalMediaType else store.avatar_path(role_id)
    try:
        content = path.read_bytes()
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="角色没有头像") from error
    return Response(content, media_type=media_type, headers={"Cache-Control": "no-cache"})

@app.post("/api/roles/{role_id}/card-image", response_model=RoleResponse)
async def upload_role_card_image(role_id: str, request: Request) -> RoleResponse:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in ROLE_AVATAR_MEDIA_TYPES:
        raise HTTPException(status_code=415, detail="仅支持 PNG、JPEG 或 WebP 图片")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_ROLE_AVATAR_BYTES:
            raise HTTPException(status_code=413, detail="卡片图片不能超过 10 MB")
        content.extend(chunk)
    if not _avatar_signature_matches(content, media_type):
        raise HTTPException(status_code=415, detail="图片内容与文件格式不匹配")
    try:
        return RoleResponse(role=store.set_avatar(role_id, bytes(content), media_type, card=True))
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"卡片图片保存失败：{error}") from error

@app.get("/api/roles/{role_id}/card-image")
def get_role_card_image(role_id: str) -> Response:
    role = store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if not role.cardImageMediaType:
        raise HTTPException(status_code=404, detail="角色没有独立卡片图片")
    try:
        content = (store.root / role_id / "card-image").read_bytes()
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="角色没有独立卡片图片") from error
    return Response(content, media_type=role.cardImageMediaType, headers={"Cache-Control": "no-cache"})

@app.delete("/api/roles/{role_id}/card-image", status_code=204)
def delete_role_card_image(role_id: str) -> Response:
    try:
        store.remove_card_image(role_id)
        return Response(status_code=204)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"卡片图片移除失败：{error}") from error


@app.delete("/api/roles/{role_id}/avatar", status_code=204)
def delete_role_avatar(role_id: str) -> Response:
    try:
        store.remove_avatar(role_id)
        return Response(status_code=204)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"头像移除失败：{error}") from error


@app.get("/api/roles/{role_id}/model-configuration")
def get_role_model_configuration(role_id: str) -> dict[str, str | None]:
    role = store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return {
        "configurationId": role.modelConfigurationId,
        "effectiveConfigurationId": role.modelConfigurationId or model_configuration_store.active_id(),
    }


@app.put("/api/roles/{role_id}/model-configuration")
def set_role_model_configuration(
    role_id: str,
    data: RoleModelConfigurationInput,
) -> dict[str, str | None]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if data.configurationId and model_configuration_store.get(data.configurationId) is None:
        raise HTTPException(status_code=404, detail="模型连接不存在")
    try:
        role = store.set_model_configuration(role_id, data.configurationId)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"角色模型绑定保存失败：{error}") from error
    return {
        "configurationId": role.modelConfigurationId,
        "effectiveConfigurationId": role.modelConfigurationId or model_configuration_store.active_id(),
    }


@app.delete("/api/roles/{role_id}", status_code=204)
async def delete_role(role_id: str) -> None:
    role = store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")

    lock = _runtime_role_lock(role_id)
    if lock.locked() or session_store.active_run(role_id) is not None:
        raise HTTPException(status_code=409, detail="该角色正在生成回复，暂时无法删除")

    await lock.acquire()
    if session_store.active_run(role_id) is not None:
        lock.release()
        raise HTTPException(status_code=409, detail="该角色正在生成回复，暂时无法删除")
    deletion_started = False
    session_snapshot = None
    audit_snapshot = None
    memory_snapshot = None
    owner_knowledge_snapshot = None
    session_snapshot_taken = False
    audit_snapshot_taken = False
    memory_snapshot_taken = False
    owner_knowledge_snapshot_taken = False
    role_delete_attempted = False
    deletion_succeeded = False
    deletion_marker = None
    deletion_lock = None
    deleted = None
    staged_path = None
    deletion_phase = "preflight"
    try:
        # Mark the gate before the first await so cancellation cannot leave a
        # role permanently blocked from future memory work.
        deletion_started = True
        await memory_worker.begin_role_deletion(role_id)
        await owner_knowledge_plugin.begin_role_deletion(role_id)
        deletion_lock = memory_worker.role_lock_for(role_id)
        await deletion_lock.acquire()
        session_snapshot = session_store.snapshot_role_session(role_id)
        session_snapshot_taken = True
        audit_snapshot = session_store.snapshot_tool_audits(role_id)
        audit_snapshot_taken = True
        memory_snapshot = memory_store.snapshot_role(role_id)
        memory_snapshot_taken = True
        owner_knowledge_snapshot = owner_knowledge_store.snapshot_role(role_id)
        owner_knowledge_snapshot_taken = True
        deletion_marker = store.begin_role_deletion(role_id)
        deletion_phase = "files"
        staged_path = store.stage_role_files_for_deletion(role_id)
        deletion_phase = "role"
        role_delete_attempted = True
        deleted = store.delete(role_id)
        deletion_phase = "session"
        session_store.delete_role_session(role_id)
        deletion_phase = "memory"
        memory_store.delete_role(role_id)
        owner_knowledge_store.delete_role(role_id)
        store.purge_staged_role_files(staged_path)
        deletion_succeeded = True
        try:
            store.complete_role_deletion(deletion_marker)
        except OSError as error:
            memory_worker.errors.append(f"{role_id}: deletion marker cleanup deferred: {error}")
    except Exception as error:
        rollback_errors: list[Exception] = []
        if session_snapshot_taken and session_snapshot is not None:
            try:
                session_store.restore_role_session(role_id, session_snapshot)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if owner_knowledge_snapshot_taken and owner_knowledge_snapshot is not None:
            try:
                owner_knowledge_store.restore_role(role_id, owner_knowledge_snapshot)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if audit_snapshot_taken and audit_snapshot is not None:
            try:
                session_store.restore_tool_audits(role_id, audit_snapshot)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if memory_snapshot_taken and memory_snapshot is not None:
            try:
                memory_store.restore_role(role_id, memory_snapshot)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if deleted is not None:
            try:
                store.restore_deleted(deleted)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if staged_path is not None:
            try:
                if deleted is not None:
                    store.remove_role_files(role_id)
                store.restore_staged_role_files(role_id, staged_path)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if deletion_marker is not None and not deletion_succeeded and not rollback_errors:
            try:
                store.complete_role_deletion(deletion_marker)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if isinstance(error, KeyError) and role_delete_attempted and deleted is None and not rollback_errors:
            raise HTTPException(status_code=404, detail="角色不存在") from error
        if rollback_errors:
            raise HTTPException(status_code=500, detail="删除失败，角色恢复也未能完成") from error
        detail = "删除失败，角色和聊天记录已保留" if deletion_phase == "session" else "删除失败，角色和记忆已保留"
        raise HTTPException(status_code=500, detail=detail) from error
    finally:
        await owner_knowledge_plugin.end_role_deletion(role_id)
        if deletion_started:
            memory_worker.end_role_deletion(role_id, deleted=deletion_succeeded)
            store.end_role_deletion(role_id)
        if deletion_lock is not None and deletion_lock.locked():
            deletion_lock.release()
        lock.release()


@app.get("/api/roles/{role_id}/session", response_model=SessionResponse)
def get_role_session(role_id: str) -> SessionResponse:
    try:
        return session_manager.open_role_session(role_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error


@app.get("/api/roles/{role_id}/memories", response_model=MemoryList)
async def list_role_memories(
    role_id: str,
    q: str = "",
    memoryType: str = "",
    memoryDomain: str = "",
    status: str = "active",
    sourceRef: str = "",
    hasEmbedding: bool | None = None,
    page: int = 1,
    pageSize: int = 50,
    sortBy: str = "updated_at",
    sortOrder: str = "desc",
) -> MemoryList:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        rows, total = _current_memory_engine().list_items_for_admin(
            role_id=role_id,
            q=q,
            memory_type=memoryType,
            memory_domain=memoryDomain,
            status=status,
            source_ref=sourceRef,
            has_embedding=hasEmbedding,
            page=page,
            page_size=pageSize,
            sort_by=sortBy,
            sort_order=sortOrder,
        )
        diagnostic_hits: list[dict[str, object]] = []
        diagnostic_trace: dict[str, object] | None = None
        if q.strip():
            diagnostic = await _current_memory_engine().query(MemoryQuery(
                text=q,
                intent="context",
                effect="read_only",
                scope=MemoryScope(role_id, f"role:{role_id}"),
                filters=MemoryQueryFilters(
                    kinds=(memoryType,) if memoryType else (),
                    domains=(memoryDomain,) if memoryDomain else (),
                ),
                limit=max(1, min(pageSize, 100)),
            ))
            diagnostic_hits = [asdict(record) for record in diagnostic.records]
            diagnostic_trace = diagnostic.trace
        return MemoryList(
            memories=[MemoryItem.model_validate(row) for row in rows],
            total=total,
            page=max(1, page),
            pageSize=max(1, pageSize),
            hits=diagnostic_hits,
            trace=diagnostic_trace,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/roles/{role_id}/memory-documents")
def list_role_memory_documents(role_id: str) -> dict[str, object]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    documents = MemoryDocuments(roles_root)
    try:
        return {
            "documents": [
                {"name": name, "content": documents.read_document(role_id, name)}
                for name in sorted(MemoryDocuments.DOCUMENTS)
            ],
            "journals": documents.read_journal(role_id),
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/roles/{role_id}/memory-documents/{document_name}")
def get_role_memory_document(role_id: str, document_name: str) -> dict[str, str]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return {"name": document_name, "content": MemoryDocuments(roles_root).read_document(role_id, document_name)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/roles/{role_id}/memory-admin/filters")
def list_memory_admin_filters(role_id: str) -> dict[str, object]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return {**_current_memory_engine().list_role_filter_values(role_id), "embedding_states": [False, True]}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/roles/{role_id}/memory-admin/events")
def list_memory_admin_events(
    role_id: str,
    timeStart: datetime,
    timeEnd: datetime,
    limit: int = 200,
) -> dict[str, object]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return {"events": _current_memory_engine().list_events_by_time_range(role_id, timeStart, timeEnd, limit=limit)}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/roles/{role_id}/memory-admin/observations")
def list_memory_observations(role_id: str, limit: int = 200) -> dict[str, object]:
    """Return the bounded in-process event observation stream for diagnostics."""
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    observations: list[dict[str, object]] = []
    for event in reversed(memory_event_bus.events):
        if getattr(event, "role_id", None) != role_id:
            continue
        observations.append({
            "type": type(event).__name__,
            "roleId": role_id,
            "sessionKey": getattr(event, "session_key", ""),
            "sourceKey": getattr(event, "source_key", getattr(event, "stable_source_key", "")),
            "error": getattr(event, "error", None),
            "memoryIds": list(getattr(event, "memory_ids", ())),
            "resultCount": getattr(event, "result_count", None),
        })
        if len(observations) >= max(0, min(limit, 1000)):
            break
    return {"events": observations}


@app.get("/api/roles/{role_id}/memory-admin/items/{memory_id}")
def get_memory_admin_item(role_id: str, memory_id: str, includeEmbedding: bool = False) -> dict[str, object]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        item = _current_memory_engine().get_item_for_admin(role_id, memory_id, include_embedding=includeEmbedding)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if item is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return item


@app.patch("/api/roles/{role_id}/memory-admin/items/{memory_id}", response_model=MemoryItem)
async def update_memory_admin_item(
    role_id: str,
    memory_id: str,
    data: MemoryAdminUpdateInput,
) -> MemoryItem:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    fields = data.model_fields_set
    source_ref = data.sourceRef.model_dump_json() if "sourceRef" in fields and data.sourceRef is not None else None
    kwargs = {
        "status": data.status if "status" in fields else None,
        "extra_json": data.extraJson if "extraJson" in fields else None,
        "source_ref": source_ref,
        "happened_at": data.happenedAt.isoformat() if "happenedAt" in fields and data.happenedAt is not None else None,
        "happened_at_provided": "happenedAt" in fields,
        "emotional_weight": data.emotionalWeight if "emotionalWeight" in fields else None,
    }
    try:
        async def update() -> MemoryItem:
            item = _current_memory_engine().update_item_for_admin(role_id, memory_id, **kwargs)
            if item is None:
                raise KeyError(memory_id)
            return MemoryItem.model_validate(item)

        return await _mutate_and_sync_memory(role_id, update)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="记忆不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/roles/{role_id}/memory-admin/items/batch-delete")
async def delete_memory_admin_items(role_id: str, data: MemoryBatchDeleteInput) -> dict[str, int]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        deleted = await _mutate_and_sync_memory(
            role_id,
            lambda: _current_memory_engine().delete_items_batch(role_id, data.ids),
        )
        return {"deleted": deleted}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/roles/{role_id}/memory-admin/invalidate")
async def invalidate_memory_admin_items(role_id: str) -> dict[str, int]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        invalidated = await _mutate_and_sync_memory(
            role_id,
            lambda: _current_memory_engine().invalidate_role_memories(role_id),
        )
        return {"invalidated": invalidated}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/roles/{role_id}/memory-admin/items/{memory_id}/similar")
def find_similar_memory_admin_items(
    role_id: str,
    memory_id: str,
    topK: int = 8,
    memoryType: str = "",
    scoreThreshold: float = 0.0,
    includeSuperseded: bool = False,
) -> dict[str, object]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return {"items": _current_memory_engine().find_similar_items_for_admin(
            role_id,
            memory_id,
            top_k=topK,
            memory_type=memoryType,
            score_threshold=scoreThreshold,
            include_superseded=includeSuperseded,
        )}
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/roles/{role_id}/memories", response_model=MemoryItem, status_code=201)
async def remember_role_memory(role_id: str, data: RememberMemoryInput) -> MemoryItem:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return await _mutate_and_sync_memory(
            role_id,
            lambda: _memory_engine_item_mutation(MemoryMutation(
                kind="remember",
                scope=MemoryScope(role_id, f"role:{role_id}"),
                summary=data.summary,
                memory_kind=data.memoryType,
                happened_at=data.happenedAt.isoformat() if data.happenedAt else "",
                source_ref=f"manual:{role_id}:{data.memoryType}:{data.summary.casefold()}",
            )),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.put("/api/roles/{role_id}/memories/{memory_id}", response_model=MemoryItem)
async def update_role_memory(role_id: str, memory_id: str, data: UpdateMemoryInput) -> MemoryItem:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return await _mutate_and_sync_memory(
            role_id,
            lambda: _memory_engine_item_mutation(MemoryMutation(
                kind="update", scope=MemoryScope(role_id, f"role:{role_id}"), ids=(memory_id,),
                summary=data.summary, memory_kind=data.memoryType,
                happened_at=data.happenedAt.isoformat() if data.happenedAt else "",
            )),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="记忆不存在") from error


@app.post("/api/roles/{role_id}/memories/{memory_id}/forget", response_model=MemoryItem)
async def forget_role_memory(role_id: str, memory_id: str) -> MemoryItem:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return await _mutate_and_sync_memory(role_id, lambda: _memory_engine_status_mutation(
            MemoryMutation(kind="state_change", scope=MemoryScope(role_id, f"role:{role_id}"), ids=(memory_id,), status="forgotten")
        ))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="记忆不存在") from error


@app.post("/api/roles/{role_id}/memories/{memory_id}/reject", response_model=MemoryItem)
async def reject_role_memory(role_id: str, memory_id: str) -> MemoryItem:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return await _mutate_and_sync_memory(role_id, lambda: _memory_engine_status_mutation(
            MemoryMutation(kind="reject", scope=MemoryScope(role_id, f"role:{role_id}"), ids=(memory_id,))
        ))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="记忆不存在") from error


@app.delete("/api/roles/{role_id}/memories/{memory_id}", status_code=204)
async def delete_role_memory(role_id: str, memory_id: str) -> None:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        await _mutate_and_sync_memory(role_id, lambda: _memory_engine_delete(
            MemoryMutation(kind="delete", scope=MemoryScope(role_id, f"role:{role_id}"), ids=(memory_id,))
        ))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="记忆不存在") from error


def _event(event_type: str, payload: dict[str, object]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_model_reply(role, history, configuration, memory_context):
    """Keep third-party/test adapters compatible while passing memory to native adapters."""
    stream_reply = model_adapter.stream_reply
    try:
        accepts_memory = "memory_context" in inspect.signature(stream_reply).parameters
    except (TypeError, ValueError):
        accepts_memory = False
    if accepts_memory:
        return stream_reply(role, history, configuration, memory_context=memory_context)
    return stream_reply(role, history, configuration)


def _memory_runtime_tool(role) -> MemoryRecallTool:
    return MemoryRecallTool(RoleScopedMemoryReadPort(_current_memory_engine(), role.id))


def _owner_knowledge_runtime_tool(role) -> OwnerKnowledgeTool:
    return OwnerKnowledgeTool(RoleBoundOwnerKnowledgeReader(role.id, owner_knowledge_plugin.search))


def _runtime_tool_candidates(role) -> tuple[ShellTool | MemoryRecallTool | OwnerKnowledgeTool, ...]:
    """Build every tool available to the host for a role snapshot."""
    shell = role.agentConfig.shell
    tools: list[ShellTool | MemoryRecallTool | OwnerKnowledgeTool] = [
        ShellTool(
            store.workspace_path(role.id),
            allowed_commands=shell.allowedCommands,
            timeout_seconds=shell.timeoutSeconds,
            max_output_chars=shell.maxOutputChars,
        ),
        _memory_runtime_tool(role),
    ]
    if owner_knowledge_plugin.status().enabled:
        tools.append(_owner_knowledge_runtime_tool(role))
    return tuple(tools)


def _enabled_runtime_tool_names(role) -> set[str]:
    shell = role.agentConfig.shell
    enabled: set[str] = set()
    if shell.enabled and shell.allowedCommands:
        enabled.add("shell")
    if role.agentConfig.memoryRecall.enabled:
        enabled.add("recall_memory")
    if owner_knowledge_plugin.status().enabled:
        enabled.add("search_owner_knowledge")
    return enabled


def _runtime_tools(
    role,
    *,
    candidates: tuple[ShellTool | MemoryRecallTool | OwnerKnowledgeTool, ...] | None = None,
) -> tuple[ShellTool | MemoryRecallTool | OwnerKnowledgeTool, ...]:
    """Filter host tool candidates using the request-time role policy."""
    available = _runtime_tool_candidates(role) if candidates is None else candidates
    enabled = _enabled_runtime_tool_names(role)
    return tuple(tool for tool in available if tool.definition.name in enabled)


async def _runtime_capabilities(
    role,
    *,
    session_key: str,
    run_id: str,
    available_tools: tuple[ShellTool | MemoryRecallTool | OwnerKnowledgeTool, ...] | None = None,
    prompt_text: str = "",
    explicit_skill_ids: list[str] | None = None,
) -> CapabilityResolution:
    agent_config = role.agentConfig
    enabled_tools = _enabled_runtime_tool_names(role)
    enabled_tools.update(agent_config.enabledTools)
    return await CapabilityRegistry(
        _runtime_tool_candidates(role) if available_tools is None else available_tools,
        skills=SkillRegistry.discover_many((
            (roles_root / role.id / "skills", "role"),
            (project_root / ".agents" / "skills", "project"),
        )),
        plugins=plugin_registry,
    ).resolve_async(
        role_id=role.id,
        session_key=session_key,
        run_id=run_id,
        enabled_tools=enabled_tools,
        allowed_risks={"read_only", "mutating", "external"},
        prompt_text=prompt_text,
        explicit_skill_ids=set(explicit_skill_ids or ()),
        enabled_plugin_ids=set(agent_config.enabledPlugins),
        granted_capabilities=set(agent_config.grantedCapabilities),
    )


@app.post("/api/roles/{role_id}/messages")
async def send_role_message(role_id: str, data: SendMessageInput, request: Request) -> StreamingResponse:
    try:
        role, session = session_manager.role_and_history(role_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
    configuration = model_configuration_store.get(role.modelConfigurationId)
    if role.modelConfigurationId and configuration is None:
        raise HTTPException(status_code=409, detail="该角色绑定的模型连接已不存在，请重新选择模型")
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")

    try:
        run_id = f"run-{uuid.uuid4().hex}"
        configuration_snapshot = configuration.model_dump(mode="json") if configuration else {}
        if isinstance(configuration_snapshot, dict):
            configuration_snapshot.pop("apiKey", None)
        role_snapshot = role.model_dump(mode="json", exclude={"modelConfig"})
        available_runtime_tools = _runtime_tool_candidates(role)
        runtime_tools = _runtime_tools(role, candidates=available_runtime_tools)
        shell_config = role.agentConfig.shell
        model_snapshot = {
            "modelConfiguration": configuration_snapshot,
            "role": role_snapshot,
        }
        run = AgentRun(
            runId=run_id,
            roleId=role_id,
            sessionKey=session.session.sessionKey,
            status="created",
            modelConfigurationId=configuration.id if configuration else None,
            modelSnapshot=model_snapshot,
            startedAt=datetime.now(timezone.utc),
        )
        runtime_manager.sessions = session_store
        runtime_manager.role_locks = role_locks
        _, user_message, assistant_message = await runtime_manager.reserve_run(run, data.content)
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="该角色正在生成回复，请稍后再试") from error
    except ActiveRunError as error:
        raise HTTPException(status_code=409, detail="该角色正在生成回复，请稍后再试") from error
    except Exception:
        raise
    context_limit = MemoryMaintenance.RECENT_MESSAGE_LIMIT * 2
    async def stream():
        if not runtime_manager.attach_current_task(run_id):
            runtime_manager.cancel_unstarted_run(run_id)
            raise asyncio.CancelledError
        content = ""
        terminal_reason = "failed"
        terminal_error: str | None = None
        sse_sequence = 0
        placeholder_id: str | None = assistant_message.id
        final_message: Message | None = None
        disconnect_watcher: asyncio.Task[None] | None = None
        capabilities: CapabilityResolution | None = None
        capabilities_owned_by_runtime = False

        def runtime_event_payload(payload: dict[str, object], runtime_sequence: int | None = None) -> dict[str, object]:
            nonlocal sse_sequence
            sse_sequence += 1
            payload.setdefault("runId", run_id)
            payload.setdefault("sequence", sse_sequence)
            if runtime_sequence is not None:
                payload["runtimeSequence"] = runtime_sequence
            return payload

        async def watch_disconnect() -> None:
            while True:
                await asyncio.sleep(0.1)
                if await request.is_disconnected():
                    runtime_manager.cancel_run(run_id)
                    return

        async def persist_runtime_message(event: MessageEndEvent) -> Message | None:
            nonlocal content, placeholder_id, final_message
            if isinstance(event.message, AssistantMessage):
                content = event.message.content
                if event.message.tool_calls:
                    # The streaming placeholder belongs to the final textual
                    # answer. Remove it before recording the first structured
                    # assistant tool-call message so SQLite order matches the
                    # provider transcript.
                    if placeholder_id is not None:
                        session_store.delete_message(placeholder_id)
                        placeholder_id = None
                        runtime_manager.set_assistant_message(run_id, None)
                    session_store.append_agent_message(
                        session.session.sessionKey,
                        event.message,
                        run_id=run_id,
                        status="completed" if event.message.stop_reason == "tool_use" else "failed",
                    )
                    return None
                if placeholder_id is not None:
                    final_message = session_store.update_message(
                        placeholder_id,
                        event.message.content,
                        "streaming",
                        metadata={"runtimeStopReason": event.message.stop_reason},
                    )
                else:
                    final_message = session_store.append_agent_message(
                        session.session.sessionKey,
                        event.message,
                        run_id=run_id,
                        status="streaming",
                    )[0]
                return final_message
            elif isinstance(event.message, ToolResultMessage):
                return session_store.append_agent_message(
                    session.session.sessionKey,
                    event.message,
                    run_id=run_id,
                    status="completed",
                )[0]
            return None

        try:
            disconnect_watcher = asyncio.create_task(watch_disconnect())
            yield _event("user_message_accepted", runtime_event_payload({"message": user_message.model_dump(mode="json")}))
            yield _event("assistant_generation_started", runtime_event_payload({"messageId": assistant_message.id}))
            try:
                # Run memory preparation after the response stream starts so
                # the disconnect watcher can cancel even a slow recall query.
                context_messages = session_store.context_messages(session.session.sessionKey)
                try:
                    if len(context_messages) > context_limit:
                        await asyncio.to_thread(
                            memory_maintenance.ensure_memory_for_window,
                            role_id,
                            session.session.sessionKey,
                            max_messages=context_limit,
                        )
                except Exception as error:
                    memory_worker.errors.append(f"{role_id}: input-window maintenance failed: {error}")
                try:
                    query_result = await _current_memory_engine().query(MemoryQuery(
                        text=data.content,
                        intent="context",
                        scope=MemoryScope(role_id, session.session.sessionKey),
                    ))
                    memory_context = memory_service.context_block([], _role_memory_context(role_id))
                    if query_result.text_block:
                        memory_context = "\n".join(part for part in (memory_context, query_result.text_block) if part)
                except Exception as error:
                    memory_worker.errors.append(f"{role_id}: recall failed: {error}")
                    memory_context = ""
                if owner_knowledge_plugin.status().enabled:
                    try:
                        owner_hits = await owner_knowledge_plugin.search(data.content, role_id=role_id, limit=4)
                        if owner_hits:
                            owner_sections = {
                                "owner_knowledge": [hit for hit in owner_hits if hit.kind == "owner_knowledge"],
                                "role_understanding": [hit for hit in owner_hits if hit.kind == "role_understanding"],
                            }
                            owner_blocks: list[str] = []
                            if owner_sections["owner_knowledge"]:
                                owner_blocks.append("[主人资料证据]\n" + "\n".join(
                                    f"- {hit.text}（{hit.title or '主人资料'}{': ' + hit.location if hit.location else ''}{', ' + hit.url if hit.url else ''}）"
                                    for hit in owner_sections["owner_knowledge"]
                                ))
                            if owner_sections["role_understanding"]:
                                owner_blocks.append("[当前角色对主人的想法]\n" + "\n".join(
                                    f"- {hit.text}" for hit in owner_sections["role_understanding"]
                                ))
                            memory_context = "\n".join(part for part in (memory_context, *owner_blocks) if part)
                    except Exception as error:
                        owner_knowledge_plugin.record_error(f"检索失败：{type(error).__name__}")
                runtime_messages = session_store.runtime_messages(
                    session.session.sessionKey,
                    max_messages=context_limit,
                )
                model_snapshot["context"] = {
                    "messages": [asdict(message) for message in runtime_messages],
                    "memoryContextMetadata": {
                        "included": bool(memory_context),
                        "characterCount": len(memory_context),
                    },
                    "toolAllowlist": list(shell_config.allowedCommands) if runtime_tools else [],
                }
                capabilities = await _runtime_capabilities(
                    role,
                    session_key=session.session.sessionKey,
                    run_id=run_id,
                    available_tools=available_runtime_tools,
                    prompt_text=data.content,
                    explicit_skill_ids=data.skillIds,
                )
                # Persist capability metadata and hashes, while keeping Skill bodies
                # in the in-memory provider context only.
                model_snapshot["capabilities"] = capabilities.snapshot.to_dict(include_prompt_sections=False)
                session_store.update_run(run_id, status="created", model_snapshot=model_snapshot)
                provider = MeidoProvider(
                    model_adapter,
                    role,
                    configuration,
                    memory_context,
                    capabilities.prompt_context(),
                )
                capabilities_owned_by_runtime = True
                async for runtime_event in runtime_manager.run(
                    AgentRun(
                        runId=run_id,
                        roleId=role_id,
                        sessionKey=session.session.sessionKey,
                        status="created",
                        modelConfigurationId=configuration.id if configuration else None,
                        modelSnapshot=model_snapshot,
                        startedAt=datetime.now(timezone.utc),
                    ),
                    provider=provider,
                    model=configuration.model if configuration else "",
                    system="",
                    messages=runtime_messages,
                    tools=runtime_tools,
                    capabilities=capabilities,
                    max_turns=8,
                    provider_timeout=120.0,
                    tool_timeout=shell_config.timeoutSeconds if runtime_tools else 30.0,
                    on_message_end=persist_runtime_message,
                ):
                    if isinstance(runtime_event, MessageStartEvent) and isinstance(runtime_event.message, AssistantMessage):
                        content = ""
                        if placeholder_id is None:
                            placeholder = session_store.append_message(
                                session.session.sessionKey,
                                "assistant",
                                "",
                                "streaming",
                                run_id=run_id,
                                metadata={"runtimePlaceholder": True},
                            )
                            placeholder_id = placeholder.id
                            runtime_manager.set_assistant_message(run_id, placeholder_id)
                    elif isinstance(runtime_event, MessageUpdateEvent):
                        content += runtime_event.delta
                        if placeholder_id is not None:
                            session_store.update_message(placeholder_id, content, "streaming")
                        yield _event("assistant_delta", runtime_event_payload({"messageId": placeholder_id or assistant_message.id, "delta": runtime_event.delta}, runtime_event.sequence))
                    elif isinstance(runtime_event, MessageEndEvent) and isinstance(runtime_event.message, AssistantMessage):
                        content = runtime_event.message.content
                    elif isinstance(runtime_event, ToolExecutionStartEvent):
                        definition = (
                            capabilities.get(runtime_event.tool_name).definition
                            if capabilities is not None and capabilities.get(runtime_event.tool_name) is not None
                            else None
                        )
                        policy_decision = (
                            capabilities.snapshot.policy_decisions.get(runtime_event.tool_name)
                            if capabilities is not None
                            else None
                        )
                        session_store.start_tool_audit(
                            run_id=run_id,
                            role_id=role_id,
                            tool_name=runtime_event.tool_name,
                            call_id=runtime_event.tool_call_id,
                            argument_summary=summarize_tool_arguments(runtime_event.arguments),
                            snapshot_id=capabilities.snapshot.snapshot_id if capabilities is not None else None,
                            tool_source=definition.source if definition is not None else None,
                            tool_version=definition.version if definition is not None else None,
                            policy_decision=str(policy_decision) if policy_decision is not None else None,
                        )
                        yield _event("tool_execution_start", runtime_event_payload({
                            "toolCallId": runtime_event.tool_call_id,
                            "toolName": runtime_event.tool_name,
                            "arguments": runtime_event.arguments,
                        }, runtime_event.sequence))
                    elif isinstance(runtime_event, ToolExecutionUpdateEvent):
                        yield _event("tool_execution_update", runtime_event_payload({
                            "toolCallId": runtime_event.tool_call_id,
                            "toolName": runtime_event.tool_name,
                            "result": runtime_event.result.content,
                            "isError": runtime_event.result.is_error,
                        }, runtime_event.sequence))
                    elif isinstance(runtime_event, ToolExecutionEndEvent):
                        details = runtime_event.result.details if isinstance(runtime_event.result.details, dict) else {}
                        session_store.finish_tool_audit(
                            run_id=run_id,
                            call_id=runtime_event.tool_call_id,
                            result_category=str(details.get("status") or ("error" if runtime_event.result.is_error else "succeeded")),
                            error_type=str(details["errorType"]) if details.get("errorType") else None,
                            error_message=str(details["message"]) if details.get("message") else None,
                        )
                        yield _event("tool_execution_end", runtime_event_payload({
                            "toolCallId": runtime_event.tool_call_id,
                            "toolName": runtime_event.tool_name,
                            "result": runtime_event.result.content,
                            "isError": runtime_event.result.is_error,
                        }, runtime_event.sequence))
                    elif isinstance(runtime_event, AgentEndEvent):
                        terminal_reason = runtime_event.reason
                        terminal_error = runtime_event.error
                if terminal_reason != "completed":
                    message = next(
                        item
                        for item in reversed(session_store.list_messages(session.session.sessionKey))
                        if item.runId == run_id and item.role == "assistant" and item.messageType == "text"
                    )
                    reason_text = {
                        "cancelled": "模型生成已取消",
                        "max_turns": "模型运行达到最大轮数",
                        "failed": terminal_error or "模型生成失败",
                    }.get(terminal_reason, terminal_error or "模型生成失败")
                    yield _event("assistant_failed", runtime_event_payload({
                        "message": message.model_dump(mode="json"),
                        "error": reason_text,
                    }))
                    return
                final_message = next(
                    item
                    for item in reversed(session_store.list_messages(session.session.sessionKey))
                    if item.runId == run_id and item.role == "assistant" and item.messageType == "text"
                )
                message = final_message
                try:
                    memory_worker.publish(TurnCommitted(
                        role_id,
                        session.session.sessionKey,
                        user_message,
                        message,
                        tuple(data.toolMemoryIds),
                        {"explicitMemoryIds": list(data.toolMemoryIds)} if data.toolMemoryIds else None,
                    ))
                except Exception as error:
                    memory_worker.errors.append(f"{role_id}: TurnCommitted publish failed: {error}")
                yield _event("assistant_completed", runtime_event_payload({"message": message.model_dump(mode="json")}))
            except Exception as error:
                runtime_manager.abandon_unstarted_run(run_id, str(error))
                message = next(
                    item
                    for item in reversed(session_store.list_messages(session.session.sessionKey))
                    if item.runId == run_id and item.role == "assistant" and item.messageType == "text"
                )
                yield _event("assistant_failed", runtime_event_payload({
                    "message": message.model_dump(mode="json"),
                    "error": _safe_model_error(error, configuration.apiKey if configuration else ""),
                }))
        except asyncio.CancelledError:
            terminal_reason = "cancelled"
            runtime_manager.cancel_run(run_id, interrupt=False)
            runtime_manager.cancel_unstarted_run(run_id)
            raise
        finally:
            if terminal_reason != "completed":
                audit_category = "cancelled" if terminal_reason == "cancelled" else "failed"
                try:
                    session_store.finish_open_tool_audits(
                        run_id,
                        result_category=audit_category,
                        error_type="CancelledError" if audit_category == "cancelled" else "RuntimeError",
                        error_message=(
                            "客户端取消，工具结果未完成"
                            if audit_category == "cancelled"
                            else "工具运行在结果记录前中断"
                        ),
                    )
                except Exception as error:
                    memory_worker.errors.append(f"{role_id}: tool audit cleanup failed: {error}")
            if capabilities is not None and not capabilities_owned_by_runtime:
                try:
                    await capabilities.close()
                except Exception:
                    pass
            if disconnect_watcher is not None:
                disconnect_watcher.cancel()
                await asyncio.gather(disconnect_watcher, return_exceptions=True)
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(runtime_manager.abandon_unstarted_run, run_id),
    )
