import os
import asyncio
import json
import inspect
import logging
import re
import threading
import uuid
import sqlite3
import secrets
import contextvars
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from time import monotonic
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
import httpx

from .ai.model_adapter import ModelAdapter
from .ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from .model_config import ModelConfigurationStore, PROVIDER_PRESETS, public_configuration, validate_model_configuration
from .memory_service import MemoryService, MemoryWorker
from .memory_engine import DefaultMemoryEngine, MemoryMutation, MemoryQuery, MemoryQueryFilters, MemoryScope
from .embeddings import SharedEmbeddingProvider
from .embedding_config import EmbeddingConfigurationStore, public_embedding_configuration, validate_embedding_configuration
from .memory_config import MemorySettings, MemorySettingsStore
from .memory_maintenance import MemoryMaintenance
from .memory_optimizer import MemoryOptimizer, MemoryOptimizerLoop, MemoryOptimizerWorker, MemoryOptimizationResult, MemoryRecord
from .memory_store import MemoryStore
from .memory_events import ConsolidationCommitted, TurnCommitted, MemoryEventBus, MemoryWritten
from .memory_documents import MemoryDocuments
from .owner_knowledge import (
    ApplicationPluginManager,
    FeishuApiConnector,
    FeishuApiError,
    OAuthCallbackAccessLogFilter,
    OwnerKnowledgePlugin,
    OwnerKnowledgeScheduler,
    SyncReport,
    OwnerKnowledgeStore,
    OwnerKnowledgeTool,
    RoleBoundOwnerKnowledgeReader,
)
from .mail import MailService, MailStore
from .mail.agent import MailManageTool
from .mail.api import router as mail_router, set_mail_service
from .models import AgentRun, EmbeddingConfiguration, EmbeddingConfigurationInput, MemoryAdminUpdateInput, MemoryBatchDeleteInput, MemoryList, MemoryItem, MemorySourceRef, Message, RememberMemoryInput, UpdateMemoryInput, ModelConfiguration, ModelConfigurationInput, ProviderPresetList, RoleInput, RoleList, RoleResponse, RoleUpdateInput, SendMessageInput, SessionResponse
from .models import RoleModelConfigurationInput
from .role_store import RoleStore
from .storage import initialize_databases, resolve_owner_knowledge_database
from .session_manager import SessionManager, public_message
from .session_store import SessionStore, load_runtime_attachments
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
from .agent_runtime.types import UserMessage
from .channels.attachment_tool import ReadChannelAttachmentTool
from .channels.consolidation import ChannelMemoryMaintenance, finish_io
from .agent_runtime.skills import SkillRegistry
from .todo import TodoPlugin, TodoStore
from .todo.agent import register_todo_agent_plugin
from .todo.application import TodoApplicationService
from .browser_use import BrowserSessions, BrowserUseConfig, register_browser_use_plugin
from .browser_use.safety import sanitize_browser_arguments, sanitize_browser_text
from .browser_use.tool import BROWSER_USE_TOOL_NAMES
from .todo.models import TodoCreate, TodoList, TodoReminderList, TodoReminderRoleInput, TodoSettingsInput, TodoStatusInput, TodoUpdate
from .role_card_import import MAX_SOURCE_BYTES, RoleCardImportError, parse_role_card
from .multichannel import ChannelAccount, MultichannelStore, TelegramChannelService
from .channels import (
    ChannelHost,
    ChannelAdapter,
    FeishuAdapter,
    FeishuConfig,
    NapCatAdapter,
    NapCatConfig,
    QQBotAdapter,
    QQBotConfig,
    TelegramAccountAdapter,
    ChannelAccountListTool,
    ChannelTargetsTool,
    ChannelSendTool,
    LookupGroupContextTool,
    LookupGroupMemberTool,
    MultichannelGroupRepository,
    SetGroupListeningTool,
    UpdateGroupContextTool,
    ChatType,
    TargetKind,
    target_from_chat,
)
from .channels.credentials import credential_key
from .channels.push import ChannelPushService, MessagePushTool
from .channels.transports import NapCatWebSocketTransport, QQBotGatewayTransport, FeishuWebhookTransport, ChannelTransportError
from .channels.media import MAX_CHANNEL_MEDIA_BYTES, media_filename
from .chat_attachments import public_attachment, read_request_bytes, resolve_attachment, save_attachment

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
multichannel_store = MultichannelStore(data_root / "sessions.db", session_store)
channel_host = ChannelHost(
    multichannel_store,
    attachment_resolver=lambda role_id, attachment_id: resolve_attachment(
        store.workspace_path(role_id), attachment_id
    ),
)
group_repository = MultichannelGroupRepository(multichannel_store)
_push_service_cache: ChannelPushService | None = None
_channel_runtime_loop: asyncio.AbstractEventLoop | None = None
channel_transports: dict[str, object] = {}
channel_adapters: dict[str, ChannelAdapter] = {}
telegram_services: dict[str, TelegramChannelService] = {}
telegram_tasks: dict[str, asyncio.Task[None]] = {}
telegram_stops: dict[str, asyncio.Event] = {}
channel_tasks: dict[str, asyncio.Task[None]] = {}
_channel_attachment_usage: dict[str, int] = {}
_channel_attachment_lock = threading.Lock()
_MAX_CHANNEL_ATTACHMENT_STORAGE_BYTES = 256 * 1024 * 1024
channel_event_locks: dict[str, asyncio.Lock] = {}
channel_lifecycle_locks: dict[str, asyncio.Lock] = {}
_channel_roles_deleting: set[str] = set()
_active_channel_lifecycles: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "active_channel_lifecycles", default=frozenset()
)
_channel_handover_accounts: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "channel_handover_accounts", default=frozenset()
)
memory_store = MemoryStore(data_root / "memory2.db")
memory_event_bus = MemoryEventBus()
model_adapter_registry = ModelAdapterRegistry()
model_adapter: ModelAdapter = RoutedModelAdapter(model_adapter_registry)
model_configuration_store = ModelConfigurationStore(data_root / "model-config.json")
embedding_configuration_store = EmbeddingConfigurationStore(data_root / "embedding-config.json")
memory_settings_store = MemorySettingsStore(data_root / "memory-settings.json")
embedding_provider = SharedEmbeddingProvider(
    embedding_configuration_store.get,
    enabled=lambda: memory_settings_store.get().embeddingEnabled,
)
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
    embedding_provider,
)
application_plugin_manager = ApplicationPluginManager()
application_plugin_manager.register("owner-knowledge", owner_knowledge_plugin)
register_todo_agent_plugin(plugin_registry)
register_browser_use_plugin(plugin_registry)
browser_use_sessions = BrowserSessions(
    Path(os.getenv("MEIDO_BROWSER_USE_DATA_DIR", str(data_root / "browser-use"))).resolve(),
    BrowserUseConfig(),
    resources=Path(os.getenv("MEIDO_BROWSER_USE_RESOURCES", str(Path(__file__).resolve().parent))).resolve(),
)
todo_store = TodoStore(data_root / "todos.db")
todo_application = TodoApplicationService(todo_store, session_store)
todo_plugin = TodoPlugin(
    todo_store,
    session_store,
    reminder_sender=lambda role_id, event_id, kind, payload: _generate_todo_reminder(role_id, event_id, kind, payload),
    external_delivery=lambda role_id, event_id, content: _deliver_todo_channel_reminder(role_id, event_id, content),
    available_roles=lambda: [role.id for role in store.list() if role.id not in store.pending_role_deletions()],
)


async def _mail_complete_model(messages: list[dict[str, object]], configuration) -> str:
    complete = getattr(model_adapter, "complete_messages", None)
    if not callable(complete):
        raise RuntimeError("当前模型适配器不支持邮件草稿生成")
    return await complete(messages, configuration, max_tokens=1800)


async def _mail_owner_search(query: str):
    return await owner_knowledge_plugin.search(query, role_id="mail-service", limit=5)


def _mail_reminder_sender(role_id: str, event_id: str, content: str) -> str:
    if store.get(role_id) is None:
        raise RuntimeError("指定的邮件提醒角色不存在")
    session = session_manager.open_role_session(role_id).sessionKey
    session_store.append_proactive_message_once(
        session,
        content,
        event_id=event_id,
        metadata={"proactiveType": "mail_draft_summary", "mailBatchId": event_id},
    )
    return event_id


mail_store = MailStore(data_root / "mail.db")
mail_service = MailService(
    mail_store,
    complete_model=_mail_complete_model,
    owner_search=_mail_owner_search,
    configuration_getter=model_configuration_store.get,
    reminder_sender=_mail_reminder_sender,
    available_roles=lambda: [role.id for role in store.list() if role.id not in store.pending_role_deletions()],
)
set_mail_service(mail_service)
mail_manage_tool = MailManageTool(mail_service)


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


def _generate_todo_reminder(role_id: str, event_id: str, kind: str, payload: list[dict[str, object]]) -> str:
    """Generate role-voiced reminder text from persisted facts only."""
    role = store.get(role_id)
    if role is None:
        raise ValueError("提醒角色不存在")
    configuration = _role_model_configuration(role_id)
    if configuration is None:
        raise RuntimeError("提醒角色没有可用模型")
    facts = json.dumps(payload, ensure_ascii=False)
    profile = role.profile
    fallback = "今天的待办：" + "、".join(str(item.get("title", "")) for item in payload)

    async def collect() -> str:
        memory_context = ""
        try:
            memory = await _current_memory_engine().query(MemoryQuery(
                text=" ".join(str(item.get(key, "")) for item in payload for key in ("title", "description")),
                intent="context",
                effect="read_only",
                scope=MemoryScope(role_id, f"role:{role_id}"),
                limit=3,
            ))
            memory_context = memory.text_block
        except Exception as error:
            todo_plugin.errors.append(f"{role_id}: reminder memory recall failed: {error}")
        prompt = (
            "你正在扮演提醒角色。请只根据给定待办事实生成一条简短自然的中文提醒。"
            "可以结合角色设定和相关记忆调整语气，但不能新增、删除或改写待办事实，也不要提及系统提示或工具。"
            f"提醒类型：{kind}；事件：{event_id}。\n"
            f"角色设定：{profile.profile}\n性格：{profile.personality}\n行为规则：{profile.behaviorRules}\n"
            f"相关记忆（仅用于表达）：{memory_context or '无'}\n待办事实（以此为准）：{facts}"
        )
        complete = getattr(model_adapter, "complete_messages", None)
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": "生成提醒"}]
        if complete is not None:
            return await complete(messages, configuration, max_tokens=300)
        parts: list[str] = []
        async for delta in model_adapter.stream_messages(messages, configuration, max_tokens=300):
            parts.append(delta)
        return "".join(parts)

    return asyncio.run(asyncio.wait_for(collect(), timeout=20.0)).strip() or fallback


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


memory_service = MemoryService(
    memory_store,
    embedding_provider,
    _post_response_provider,
    memory_event_bus,
    implicit_extraction_enabled=_env_bool("MEIDO_MEMORY_IMPLICIT_EXTRACTION_ENABLED", True),
    memory_enabled=lambda: memory_settings_store.get().enabled,
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
    ),
    enabled=lambda: memory_settings_store.get().enabled,
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
    enabled_provider=lambda: memory_settings_store.get().enabled,
)
memory_maintenance = MemoryMaintenance(
    roles_root,
    session_store,
    _observe_consolidation_event,
    _consolidation_provider,
    role_exists=lambda role_id: store.get(role_id) is not None,
    messages_provider=lambda role_id, session_key: (
        multichannel_store.owner_context_messages(role_id)
        if multichannel_store.sessions is session_store
        else session_store.context_messages(session_key)
    ),
)
memory_worker = MemoryWorker(
    memory_service,
    memory_maintenance,
    memory_optimizer_worker,
)
connection_test_adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=20))
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


def _todo_application() -> TodoApplicationService:
    """Keep web handlers aligned when tests or lifecycle code replace stores."""

    if todo_application.store is todo_store and todo_application.sessions is session_store:
        return todo_application
    return TodoApplicationService(todo_store, session_store)


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
    if not memory_settings_store.get().enabled:
        return ""
    recent_current = True
    if multichannel_store.sessions is session_store and not memory_worker.role_deletion_blocked(role_id):
        try:
            recent_current = memory_maintenance.refresh_recent_context(role_id, f"role:{role_id}")
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            recent_current = False
            logger.warning("主人近期上下文刷新失败 role=%s", role_id, exc_info=True)
    documents = MemoryDocuments(roles_root)
    try:
        documents.memory_dir(role_id)
    except ValueError:
        return ""
    sections = []
    for filename in ("SELF.md", "MEMORY.md", "RECENT_CONTEXT.md"):
        if filename == "RECENT_CONTEXT.md" and not recent_current:
            continue
        try:
            content = documents.read_document(role_id, filename).strip()
        except (OSError, RuntimeError, ValueError):
            continue
        if content:
            sections.append(f"[{filename}]\n{content}")
    return "\n\n".join(sections)


def _role_owner_understanding_memory(role_id: str) -> str:
    if not memory_settings_store.get().enabled:
        return ""
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
    await mail_service.close()
    await owner_knowledge_scheduler.close()
    await owner_knowledge_plugin.close()
    await todo_plugin.close()
    await memory_optimizer_loop.close()
    await memory_worker.close()
    await memory_optimizer_worker.drain()
    await browser_use_sessions.close()


async def _start_memory_workers() -> None:
    global _channel_runtime_loop
    _channel_runtime_loop = asyncio.get_running_loop()
    mail_service.start()
    todo_plugin.sessions = session_store
    await todo_plugin.start()
    memory_worker.start()
    store.cleanup_staged_role_files()
    memory_worker.errors.extend(store.recovery_errors)
    for role_id in store.pending_role_deletions():
        _channel_roles_deleting.add(role_id)
        recovered = False
        try:
            for account in multichannel_store.list_role_accounts(role_id):
                multichannel_store.set_account_state(account.account_id, enabled=False, last_error="role_deleted")
                await _stop_channel_account(account.account_id)
            await memory_worker.begin_role_deletion(role_id)
            await channel_memory_maintenance.begin_role_deletion(role_id)
            await owner_knowledge_plugin.begin_role_deletion(role_id)
            session_store.delete_role_session(role_id)
            memory_store.delete_role(role_id)
            owner_knowledge_store.delete_role(role_id)
            multichannel_store.delete_role(role_id)
            _channel_push_service().clear_role(role_id)
            todo_store.mark_source_unavailable(role_id)
            for marker in store.root.glob(f".deleting-{role_id}-*.json"):
                store.complete_role_deletion(marker)
            recovered = True
        except Exception as error:
            memory_worker.errors.append(f"{role_id}: interrupted deletion recovery failed: {type(error).__name__}")
        finally:
            memory_worker.end_role_deletion(role_id, deleted=True)
            channel_memory_maintenance.end_role_deletion(role_id, deleted=True)
            await owner_knowledge_plugin.end_role_deletion(role_id)
            if recovered:
                _channel_roles_deleting.discard(role_id)
    if memory_settings_store.get().enabled:
        await asyncio.to_thread(memory_maintenance.resume_pending)
    if memory_settings_store.get().enabled:
        memory_optimizer_loop.start()
    owner_startup_report = await owner_knowledge_plugin.startup()
    if owner_startup_report and (owner_startup_report.created or owner_startup_report.updated or owner_startup_report.deleted):
        _queue_owner_understanding_refresh(owner_startup_report)
    owner_knowledge_scheduler.start()


owner_knowledge_frontend_port = os.getenv("MEIDO_WEB_PORT", "5288").strip()
owner_knowledge_frontend_origins = [
    os.getenv("MEIDO_WEB_ORIGIN", f"http://127.0.0.1:{owner_knowledge_frontend_port}").strip().rstrip("/"),
]
if "MEIDO_WEB_ORIGIN" not in os.environ:
    owner_knowledge_frontend_origins.append(f"http://localhost:{owner_knowledge_frontend_port}")


def _owner_knowledge_return_url(origin: str = "") -> str:
    requested_origin = origin.rstrip("/")
    safe_origin = (
        requested_origin
        if requested_origin in owner_knowledge_frontend_origins
        else owner_knowledge_frontend_origins[0]
    )
    return f"{safe_origin}/app?ownerKnowledge=connected"


app = FastAPI(title="Meido API", on_startup=[_start_memory_workers], on_shutdown=[_close_memory_workers])
app.include_router(mail_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=owner_knowledge_frontend_origins,
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
        "embeddingReady": embedding_provider.ready,
        "embeddingEnabled": memory_settings_store.get().embeddingEnabled,
        "embeddingConfigured": bool(embedding_configuration_store.get().apiKey),
        "embeddingModel": embedding_provider.model_name,
        "embeddingDimensions": embedding_provider.dimensions,
        "embeddingProvider": embedding_configuration_store.get().provider,
        "embeddingLastError": owner_knowledge_plugin.embedding_last_error or embedding_provider.last_error,
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
def authorize_owner_knowledge(request: Request) -> dict[str, str]:
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
    owner_knowledge_store.set_state("oauth_return_url", _owner_knowledge_return_url(request.headers.get("origin", "")))
    try:
        return {"authorizationUrl": feishu_authorization_url(app_id, redirect_uri, state)}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/owner-knowledge/oauth/callback")
async def owner_knowledge_oauth_callback(code: str = "", state: str = "", error: str = "") -> Response:
    if error:
        raise HTTPException(status_code=400, detail="飞书授权未完成")

    try:
        expected_state = owner_knowledge_store.get_state("oauth_state")
    except Exception as callback_error:
        logger.exception("读取飞书 OAuth 状态失败：%s", type(callback_error).__name__)
        raise HTTPException(status_code=503, detail="飞书授权状态暂时不可用，请重试") from callback_error
    if not code or not state or state != expected_state:
        raise HTTPException(status_code=400, detail="飞书授权状态无效或已过期")

    try:
        # Consume the one-time state before exchanging the code. A failed
        # exchange can then be retried by starting a fresh authorization flow.
        owner_knowledge_store.set_state("oauth_state", "")
        return_url = owner_knowledge_store.get_state("oauth_return_url")
        owner_knowledge_store.set_state("oauth_return_url", "")
        app_id = owner_knowledge_store.get_state("feishu_app_id") or os.getenv("MEIDO_FEISHU_APP_ID", "").strip()
        app_secret = owner_knowledge_store.get_state("feishu_app_secret") or os.getenv("MEIDO_FEISHU_APP_SECRET", "").strip()
        redirect_uri = os.getenv(
            "MEIDO_FEISHU_REDIRECT_URI",
            "http://127.0.0.1:4288/api/owner-knowledge/oauth/callback",
        ).strip()
    except Exception as callback_error:
        logger.exception("读取或清理飞书 OAuth 配置失败：%s", type(callback_error).__name__)
        raise HTTPException(status_code=503, detail="飞书授权配置暂时不可用，请重试") from callback_error
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
    safe_return_urls = {_owner_knowledge_return_url(origin) for origin in owner_knowledge_frontend_origins}
    if return_url not in safe_return_urls:
        return_url = _owner_knowledge_return_url()
    return RedirectResponse(return_url, status_code=303)


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


@app.get("/api/todos/settings")
def get_todo_settings() -> dict[str, object]:
    return todo_store.settings()


@app.put("/api/todos/settings")
async def update_todo_settings(data: TodoSettingsInput) -> dict[str, object]:
    return await todo_plugin.configure(
        enabled=data.enabled,
        reminders_enabled=data.remindersEnabled,
        timezone_name=data.timezone,
    )


@app.get("/api/todos/reminder-roles")
def list_todo_reminder_roles() -> dict[str, object]:
    settings = todo_store.settings()
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(str(settings["timezone"]))).date()
    roles = store.list()
    configured = {str(item["roleId"]): item for item in todo_store.reminder_role_settings((role.id for role in roles), on_date=today)}
    return {"roles": [
        {"id": role.id, "name": role.name, **configured[role.id]}
        for role in roles
    ]}


@app.put("/api/todos/reminder-roles/{role_id}")
def update_todo_reminder_role(role_id: str, data: TodoReminderRoleInput) -> dict[str, object]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    settings = todo_store.settings()
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(str(settings["timezone"]))).date()
    effective_date = today if not data.enabled else today + timedelta(days=1)
    todo_store.set_reminder_role(role_id, data.enabled, effective_date=effective_date)
    return {"roleId": role_id, "enabled": data.enabled, "effectiveDate": effective_date.isoformat()}


@app.get("/api/todos/reminders/today")
def get_todo_reminders_today() -> dict[str, object]:
    settings = todo_store.settings()
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(str(settings["timezone"]))).date()
    return {"date": today.isoformat(), "paused": todo_store.reminders_paused(today)}


@app.put("/api/todos/reminders/today")
def set_todo_reminders_today(paused: bool) -> dict[str, object]:
    settings = todo_store.settings()
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(str(settings["timezone"]))).date()
    todo_store.set_reminders_paused(today if paused else None)
    return {"date": today.isoformat(), "paused": paused}


@app.get("/api/todos", response_model=TodoList)
def list_todos(status: str | None = None, query: str | None = None) -> TodoList:
    valid_statuses = {"inbox", "scheduled", "completed", "cancelled"}
    if status is not None and status not in valid_statuses:
        raise HTTPException(status_code=422, detail="不支持的待办状态")
    return TodoList(todos=todo_store.list(statuses=(status,) if status else None, query=query))


@app.post("/api/todos", response_model=dict, status_code=201)
def create_todo(data: TodoCreate) -> dict[str, object]:
    try:
        return _todo_application().create(data.model_dump(mode="json"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.put("/api/todos/{todo_id}")
def update_todo(todo_id: str, data: TodoUpdate) -> dict[str, object]:
    try:
        return _todo_application().update(todo_id, data.model_dump(mode="json", exclude_unset=True))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="待办不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.put("/api/todos/{todo_id}/status")
def set_todo_status(todo_id: str, data: TodoStatusInput) -> dict[str, object]:
    try:
        return _todo_application().set_status(todo_id, data.status)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="待办不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/todos/reminders", response_model=TodoReminderList)
def list_todo_reminders() -> TodoReminderList:
    todo_plugin.schedule_reminders_once()
    reminders = todo_store.list_reminders()
    return TodoReminderList(reminders=reminders, unreadCount=todo_store.unread_reminder_count())


@app.post("/api/todos/reminders/{reminder_id}/read", status_code=204)
def mark_todo_reminder_read(reminder_id: str) -> Response:
    todo_store.mark_reminder_read(reminder_id)
    return Response(status_code=204)


@app.post("/api/todos/reminders/{reminder_id}/retry")
def retry_todo_reminder(reminder_id: str) -> dict[str, bool]:
    if not todo_store.retry_reminder(reminder_id):
        raise HTTPException(status_code=404, detail="可重试的提醒不存在")
    todo_plugin.schedule_reminders_once()
    return {"accepted": True}


@app.get("/api/todos/diagnostics")
def todo_diagnostics() -> dict[str, object]:
    return {"items": todo_plugin.diagnostics(), "workerErrors": list(todo_plugin.errors[-20:])}


@app.get("/api/model/providers", response_model=ProviderPresetList)
def list_model_providers() -> ProviderPresetList:
    return ProviderPresetList(providers=PROVIDER_PRESETS)


def _public_memory_settings(settings: MemorySettings | None = None) -> dict[str, object]:
    current = settings or memory_settings_store.get()
    return {
        "enabled": current.enabled,
        "embeddingEnabled": current.embeddingEnabled,
        "embedding": get_embedding_configuration(),
    }


@app.get("/api/memory/settings")
def get_memory_settings() -> dict[str, object]:
    return _public_memory_settings()


@app.put("/api/memory/settings")
async def update_memory_settings(data: MemorySettings) -> dict[str, object]:
    previous = memory_settings_store.get()
    try:
        current = memory_settings_store.save(data)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="记忆设置保存失败，当前设置未改变") from error
    if current.enabled and not previous.enabled:
        memory_worker.start()
        memory_optimizer_loop.start()
        await asyncio.to_thread(memory_maintenance.resume_pending)
    return _public_memory_settings(current)


def _embedding_configuration_with_saved_key(data: EmbeddingConfigurationInput) -> EmbeddingConfigurationInput:
    if data.apiKey:
        return data
    current = embedding_configuration_store.get()
    return data.model_copy(update={"apiKey": current.apiKey})


@app.get("/api/embedding/configuration")
def get_embedding_configuration() -> dict[str, object]:
    configuration = embedding_configuration_store.get()
    return {
        "configuration": public_embedding_configuration(configuration),
        "status": {
            "configured": bool(configuration.apiKey),
            "ready": embedding_provider.ready,
            "enabled": memory_settings_store.get().embeddingEnabled,
            "model": configuration.model,
            "dimensions": configuration.dimensions,
            "lastError": embedding_provider.last_error,
        },
    }


@app.put("/api/embedding/configuration")
def save_embedding_configuration(data: EmbeddingConfigurationInput) -> dict[str, object]:
    try:
        configuration = embedding_configuration_store.save(_embedding_configuration_with_saved_key(data))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="向量模型配置保存失败，当前生效配置未改变") from error
    embedding_provider.last_error = None
    return {
        "configuration": public_embedding_configuration(configuration),
        "status": {
            "configured": bool(configuration.apiKey),
            "ready": embedding_provider.ready,
            "enabled": memory_settings_store.get().embeddingEnabled,
            "model": configuration.model,
            "dimensions": configuration.dimensions,
            "lastError": embedding_provider.last_error,
        },
    }


@app.post("/api/embedding/configuration/test")
def test_embedding_configuration(data: EmbeddingConfigurationInput) -> dict[str, object]:
    try:
        candidate = _embedding_configuration_with_saved_key(data)
        validate_embedding_configuration(candidate)
        configuration = EmbeddingConfiguration.model_validate(candidate.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    tester = SharedEmbeddingProvider(lambda: configuration, timeout=10)
    started_at = monotonic()
    try:
        tester.embed_query("ping")
    except Exception as error:
        return {"ok": False, "message": _safe_model_error(error, configuration.apiKey)}
    return {"ok": True, "message": "连接成功", "latencyMs": round((monotonic() - started_at) * 1000)}


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
    if current and (current.providerId, current.provider, current.baseUrl.rstrip("/"), current.api) == (
        data.providerId,
        data.provider,
        data.baseUrl.rstrip("/"),
        data.api,
    ):
        return data.model_copy(update={"apiKey": current.apiKey})
    return data


def _safe_model_error(error: Exception, api_key: str = "") -> str:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 400:
            detail = str(error).strip()
            message = f"模型服务拒绝了请求 (HTTP 400)：{detail}" if detail else "模型服务拒绝了请求 (HTTP 400)"
        else:
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
        async for event in connection_test_adapter.stream_messages(
            [{"role": "user", "content": "ping"}], configuration, max_tokens=8
        ):
            if getattr(event, "type", None) == "error":
                message = getattr(event, "error", "模型服务请求失败")
                return {"ok": False, "message": _safe_model_error(RuntimeError(str(message)), configuration.apiKey)}
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
async def update_role(role_id: str, data: RoleUpdateInput) -> RoleResponse:
    try:
        current = store.get(role_id)
        updated = store.update(role_id, data)
        if current is not None and current.agentConfig.browserUse.enabled != updated.agentConfig.browserUse.enabled:
            # The role policy is the plugin's explicit authorization boundary.
            # Disabling closes the current generation and blocks an old run's
            # frozen capability snapshot from creating a replacement session.
            await browser_use_sessions.set_role_enabled(role_id, updated.agentConfig.browserUse.enabled)
        return RoleResponse(role=updated)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"角色保存失败：{error}") from error


@app.post("/api/role-card-import/preview")
async def preview_role_card_import(request: Request) -> dict[str, object]:
    """Parse a role card without creating or modifying a role.

    The browser sends the file bytes directly and supplies the original name in
    ``x-role-card-filename``.  This avoids requiring a multipart parser for a
    preview-only endpoint and keeps the payload bounded before parsing.
    """
    filename = unquote(request.headers.get("x-role-card-filename", "role-card.json").strip() or "role-card.json")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_SOURCE_BYTES:
            raise HTTPException(status_code=413, detail="角色卡文件不能超过 50 MB")
        content.extend(chunk)
    try:
        return parse_role_card(filename, bytes(content))
    except RoleCardImportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/roles/{role_id}/attachments")
async def upload_chat_attachment(role_id: str, request: Request) -> dict[str, object]:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    filename = unquote(request.headers.get("x-attachment-filename", "attachment").strip() or "attachment")
    data = await read_request_bytes(request)
    media_type = request.headers.get("content-type", "application/octet-stream")
    return public_attachment(save_attachment(store.workspace_path(role_id), filename, media_type, data))


@app.get("/api/roles/{role_id}/attachments/{attachment_id}")
def get_chat_attachment(role_id: str, attachment_id: str) -> Response:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    item = resolve_attachment(store.workspace_path(role_id), attachment_id)
    if item is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    try:
        content = Path(str(item["path"])).read_bytes()
    except OSError as error:
        raise HTTPException(status_code=404, detail="附件不存在") from error
    filename = quote(str(item.get("name") or "attachment"), safe="")
    return Response(content, media_type=str(item.get("mediaType") or "application/octet-stream"), headers={"Content-Disposition": f"inline; filename*=UTF-8''{filename}"})


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
    # Close channel turn admission synchronously before any preflight await.
    # A newly accepted source event must not wait for the role lock held here
    # while deletion is trying to drain that same source event.
    _channel_roles_deleting.add(role_id)
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
    channel_accounts_snapshot: list[ChannelAccount] = []
    channel_snapshot = None
    try:
        await browser_use_sessions.set_role_enabled(role_id, False)
        # Mark the gate before the first await so cancellation cannot leave a
        # role permanently blocked from future memory work.
        deletion_started = True
        await memory_worker.begin_role_deletion(role_id)
        await channel_memory_maintenance.begin_role_deletion(role_id)
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
        # Quiesce channel workers before removing the role.  The snapshot lets
        # rollback restart the same accounts if a later cleanup step fails.
        channel_accounts_snapshot = multichannel_store.list_role_accounts(role_id)
        for account in channel_accounts_snapshot:
            await _stop_channel_account(account.account_id)
        channel_snapshot = multichannel_store.snapshot_role(role_id)
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
        deletion_phase = "channels"
        multichannel_store.delete_role(role_id)
        _channel_push_service().clear_role(role_id)
        store.purge_staged_role_files(staged_path)
        deletion_succeeded = True
        try:
            todo_store.mark_source_unavailable(role_id)
        except Exception as error:
            todo_plugin.errors.append(f"{role_id}: source cleanup deferred: {error}")
        try:
            store.complete_role_deletion(deletion_marker)
        except OSError as error:
            memory_worker.errors.append(f"{role_id}: deletion marker cleanup deferred: {error}")
    except HTTPException:
        raise
    except (Exception, asyncio.CancelledError) as error:
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
        if channel_snapshot is not None:
            try:
                multichannel_store.restore_role(role_id, channel_snapshot)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if channel_accounts_snapshot and not deletion_succeeded:
            for account in channel_accounts_snapshot:
                if not account.enabled:
                    continue
                try:
                    await _start_channel_account(account.account_id)
                except Exception as restart_error:
                    rollback_errors.append(restart_error)
        if deletion_marker is not None and not deletion_succeeded and not rollback_errors:
            try:
                store.complete_role_deletion(deletion_marker)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if isinstance(error, asyncio.CancelledError):
            if rollback_errors:
                memory_worker.errors.append(f"{role_id}: cancelled deletion rollback failed")
            raise
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
            channel_memory_maintenance.end_role_deletion(role_id, deleted=deletion_succeeded)
            store.end_role_deletion(role_id)
        if deletion_lock is not None and deletion_lock.locked():
            deletion_lock.release()
        lock.release()
        _channel_roles_deleting.discard(role_id)
    if deletion_succeeded:
        for account in channel_accounts_snapshot:
            channel_host.unregister(account.account_id)
            channel_adapters.pop(account.account_id, None)
            channel_transports.pop(account.account_id, None)
            telegram_services.pop(account.account_id, None)
        try:
            browser_use_sessions.delete_role_data(role_id)
        except (OSError, RuntimeError) as error:
            memory_worker.errors.append(f"{role_id}: browser data cleanup deferred: {error}")


@app.get("/api/roles/{role_id}/session", response_model=SessionResponse)
def get_role_session(role_id: str) -> SessionResponse:
    try:
        response = session_manager.open_role_session(role_id)
        multichannel_store.register_desktop_thread(
            role_id,
            response.session.sessionKey,
            response.session.createdAt.isoformat(),
            response.session.updatedAt.isoformat(),
        )
        return response
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error


def _channel_account_payload(account: ChannelAccount) -> dict[str, object]:
    payload = account.public_dict(token_configured=_channel_credentials_configured(account))
    payload["credentialsConfigured"] = _channel_credentials_configured(account)
    payload["avatarUrl"] = multichannel_store.avatars.url(account.account_id, "account", account.platform_id or account.account_id)
    config = multichannel_store.channel_config(account.account_id)
    if config.get("endpoint"):
        payload["endpoint"] = config["endpoint"]
    app_id = config.get("appId") or account.platform_id
    if app_id:
        payload["appId"] = app_id
    payload["connectionStatus"] = _channel_connection_status(account)
    adapter = channel_adapters.get(account.account_id)
    if adapter is not None:
        capabilities = adapter.capabilities
        payload["capabilities"] = {
            "private": capabilities.private,
            "group": capabilities.group,
            "groupListening": capabilities.group_listening,
            "media": capabilities.media,
            "replies": capabilities.replies,
            "streaming": capabilities.streaming,
            "targets": sorted(item.value for item in capabilities.targets),
        }
    return payload


@asynccontextmanager
async def _channel_lifecycle(account_id: str):
    """Serialize configuration, state, and transport changes per account."""

    active = _active_channel_lifecycles.get()
    if account_id in active:
        yield
        return
    lock = channel_lifecycle_locks.setdefault(account_id, asyncio.Lock())
    await lock.acquire()
    token = _active_channel_lifecycles.set(active | {account_id})
    try:
        yield
    finally:
        _active_channel_lifecycles.reset(token)
        lock.release()


def _channel_connection_status(account: ChannelAccount) -> str:
    if not account.enabled:
        return "disconnected"
    if account.last_error:
        return "error"
    if account.channel == "telegram":
        task = telegram_tasks.get(account.account_id)
        return "connected" if task is not None and not task.done() else "disconnected"
    transport = channel_transports.get(account.account_id)
    if transport is None:
        return "disconnected"
    if bool(getattr(transport, "connected", False)):
        return "connected"
    if account.channel == "feishu" and getattr(transport, "_handler", None) is not None:
        return "connected"
    task = getattr(transport, "_task", None)
    return "connecting" if task is not None and not task.done() else "disconnected"


def _channel_secret_key(account: ChannelAccount, name: str) -> str:
    return credential_key(account.channel, account.account_id, name)


def _channel_secret(account: ChannelAccount, name: str) -> str:
    return multichannel_store.credentials.get(_channel_secret_key(account, name))


def _channel_credentials_configured(account: ChannelAccount) -> bool:
    if account.channel == "telegram":
        return bool(multichannel_store.token(account.account_id))
    required = {"qq": (), "qqbot": ("client_secret",), "feishu": ("app_secret",)}.get(account.channel, ())
    if account.channel in {"qqbot", "feishu"} and not multichannel_store.channel_config(account.account_id).get("appId"):
        return False
    return all(bool(_channel_secret(account, key)) for key in required)


def _channel_credential_names(channel: str) -> tuple[str, ...]:
    return {
        "qq": ("access_token",),
        "qqbot": ("client_secret",),
        "feishu": ("app_secret", "verification_token", "encrypt_key"),
    }.get(channel, ())


def _build_channel_adapter(account: ChannelAccount, *, register: bool = True) -> ChannelAdapter:
    existing = channel_adapters.get(account.account_id)
    if register and existing is not None:
        return existing
    config = multichannel_store.channel_config(account.account_id)
    transport: object | None = None
    if account.channel == "telegram":
        transport = _telegram_service(account.account_id).transport
        adapter = TelegramAccountAdapter(
            account.account_id,
            lambda: multichannel_store.token(account.account_id),
            transport,
        )
    elif account.channel == "qq":
        transport = NapCatWebSocketTransport(
            str(config.get("endpoint") or "ws://127.0.0.1:6700"),
            access_token=_channel_secret(account, "access_token"),
        )
        adapter: ChannelAdapter = NapCatAdapter(NapCatConfig(
            account_id=account.account_id,
            endpoint=str(config.get("endpoint") or "ws://127.0.0.1:6700"),
            private_enabled=account.allow_private,
            group_enabled=account.allow_group,
            group_listening=True,
            blocked_sender_ids=set(account.blocked_senders),
        ), transport=transport)
    elif account.channel == "qqbot":
        transport = QQBotGatewayTransport(
            str(config.get("appId") or ""),
            _channel_secret(account, "client_secret"),
            api_endpoint=str(config.get("endpoint") or "https://api.sgroup.qq.com"),
        )
        adapter = QQBotAdapter(QQBotConfig(
            account_id=account.account_id,
            app_id=str(config.get("appId") or ""),
            endpoint=str(config.get("endpoint") or "https://api.sgroup.qq.com"),
            private_enabled=account.allow_private,
            blocked_sender_ids=set(account.blocked_senders),
        ), transport=transport)
    elif account.channel == "feishu":
        transport = FeishuWebhookTransport(
            str(config.get("appId") or ""),
            _channel_secret(account, "app_secret"),
            verification_token=_channel_secret(account, "verification_token"),
            encrypt_key=_channel_secret(account, "encrypt_key"),
            endpoint=str(config.get("endpoint") or "https://open.feishu.cn/open-apis"),
            long_connection=True,
        )
        adapter = FeishuAdapter(FeishuConfig(
            account_id=account.account_id,
            app_id=str(config.get("appId") or ""),
            endpoint=str(config.get("endpoint") or "https://open.feishu.cn/open-apis"),
            private_enabled=account.allow_private,
            blocked_sender_ids=set(account.blocked_senders),
        ), transport=transport)
    else:
        raise ValueError("不支持的渠道类型")
    if register:
        adapter.set_event_handler(lambda payload: _dispatch_channel_payload(account.account_id, payload, source_adapter=adapter))
        channel_host.register(adapter)
        channel_adapters[account.account_id] = adapter
        if transport is not None:
            channel_transports[account.account_id] = transport
    return adapter


def _channel_identity_scope(channel: str) -> str:
    return "platform" if channel in {"telegram", "qq"} else "account"


async def _fetch_channel_avatar(account: ChannelAccount, kind: str, subject_id: str) -> bytes | None:
    from .channels.avatars import download_avatar

    if account.channel == "qq":
        if not subject_id.isdigit():
            return None
        url = f"https://p.qlogo.cn/gh/{subject_id}/{subject_id}/100" if kind == "chat" else f"https://q1.qlogo.cn/g?b=qq&nk={subject_id}&s=100"
        return await download_avatar(url)
    if account.channel == "telegram":
        service = _telegram_service(account.account_id)
        api = service.transport
        if not callable(getattr(api, "get_user_profile_photos", None)) or not callable(getattr(api, "get_chat", None)):
            return None
        token = multichannel_store.token(account.account_id)
        if kind == "chat" or subject_id.startswith("chat:"):
            profile = await api.get_chat(token, subject_id.removeprefix("chat:"))
            photo = profile.get("photo") or {}
            file_id = str(photo.get("small_file_id") or "")
        else:
            profile = await api.get_user_profile_photos(token, subject_id)
            photos = profile.get("photos") or []
            file_id = str(photos[0][0].get("file_id") or "") if photos and photos[0] else ""
        if not file_id:
            return None
        file = await api.get_file(token, file_id)
        data, _mime = await api.download_file(token, str(file.get("file_path") or ""))
        return data
    transport = channel_transports.get(account.account_id)
    profile_request = getattr(transport, "profile", None)
    if not callable(profile_request):
        return None
    if account.channel == "qqbot":
        # C2C events do not expose an authorised profile endpoint for contacts.
        if kind != "account":
            return None
        profile = await profile_request()
        url = str(profile.get("avatar") or "")
    else:
        profile = await profile_request(None if kind == "account" else subject_id)
        avatar = profile.get("avatar")
        url = str(avatar.get("avatar_240") or avatar.get("avatar_72") or "") if isinstance(avatar, dict) else str(avatar or profile.get("avatar_url") or "")
    return await download_avatar(url) if url else None


def _refresh_channel_avatars(account: ChannelAccount, sender_id: str = "", chat_id: str = "", chat_type: str = "private") -> None:
    own_id = account.platform_id or account.account_id
    multichannel_store.avatars.refresh(account, "account", own_id, lambda: _fetch_channel_avatar(account, "account", own_id))
    if sender_id:
        multichannel_store.avatars.refresh(account, "sender", sender_id, lambda: _fetch_channel_avatar(account, "sender", sender_id))
    if chat_id:
        subject = chat_id if chat_type == "group" or account.channel == "telegram" else sender_id
        kind = "chat" if chat_type == "group" or account.channel == "telegram" else "sender"
        if subject:
            multichannel_store.avatars.refresh(account, "chat", chat_id, lambda: _fetch_channel_avatar(account, kind, subject))


async def _dispatch_channel_payload(account_id: str, payload: dict[str, object], *, source_adapter: ChannelAdapter | None = None) -> None:
    account = multichannel_store.get_account(account_id)
    if account is not None and account.role_id in _channel_roles_deleting:
        original = source_adapter or channel_adapters.get(account_id)
        if original is not None:
            await _reject_channel_payload(original, payload)
        return
    try:
        async with channel_host.event_scope(account_id) as admitted:
            current_adapter = channel_adapters.get(account_id)
            if not admitted or (source_adapter is not None and source_adapter is not current_adapter):
                original = source_adapter or current_adapter
                if original is not None and account is not None and account.enabled:
                    if channel_host.intake.defer(
                        account_id,
                        lambda: _dispatch_channel_payload(account_id, payload),
                        lambda: _reject_channel_payload(original, payload),
                    ):
                        return
                    await _reject_channel_payload(original, payload)
                return
            if account is not None and account.channel == "qq" and payload.get("self_id"):
                try:
                    multichannel_store.set_platform_identity(account_id, str(payload["self_id"]))
                except ValueError:
                    multichannel_store.set_account_state(account_id, enabled=False, last_error="platform_identity_conflict")
                    logger.warning("拒收渠道账号身份冲突消息 account=%s", account_id)
                    return
            event = await channel_host.accept_event(account_id, payload)
            if event is not None:
                await _handle_channel_event(event)
    except Exception as error:
        logger.warning("渠道入站处理失败 account=%s error=%s", account_id, type(error).__name__)


async def _reject_channel_payload(adapter, payload) -> None:
    try:
        event = await adapter.accept_event(payload)
        if event is not None and event.triggered:
            await channel_host.send_retry_notice(adapter, event)
    except Exception as error:
        logger.warning("渠道拒收提示失败 account=%s error=%s", adapter.account_id, type(error).__name__)


async def _persist_channel_attachments(event, role_id: str) -> list[dict[str, object]]:
    if not event.attachments or (event.chat_type is ChatType.GROUP and not event.triggered):
        return []
    adapter = channel_adapters.get(event.account_id)
    download_media = getattr(adapter, "download_media", None)
    if not callable(download_media):
        return []
    try:
        downloaded = await download_media(event)
    except Exception as error:
        logger.warning("渠道附件下载失败 account=%s error=%s", event.account_id, type(error).__name__)
        return []

    attachments: list[dict[str, object]] = []
    for item in downloaded[:4]:
        data = getattr(item, "data", None)
        if not isinstance(data, bytes) or len(data) > MAX_CHANNEL_MEDIA_BYTES:
            logger.warning("渠道附件超过大小限制 account=%s kind=%s", event.account_id, getattr(item, "kind", "unknown"))
            continue
        try:
            attachments.append(_save_channel_attachment(
                role_id,
                media_filename(getattr(item, "file_name", None), getattr(item, "mime_type", None)),
                str(getattr(item, "mime_type", None) or "application/octet-stream"),
                data,
            ))
        except Exception as error:
            logger.warning("渠道附件保存失败 account=%s kind=%s error=%s", event.account_id, getattr(item, "kind", "unknown"), type(error).__name__)
    return attachments


def _save_channel_attachment(role_id: str, name: str, mime_type: str, data: bytes) -> dict[str, object]:
    with _channel_attachment_lock:
        root = store.workspace_path(role_id)
        if role_id not in _channel_attachment_usage:
            directory = root / "attachments"
            _channel_attachment_usage[role_id] = sum(
                path.stat().st_size
                for path in directory.iterdir()
                if path.is_file() and not re.fullmatch(r"[0-9a-f]{32}\.json", path.name)
            ) if directory.exists() else 0
        used = _channel_attachment_usage[role_id]
        if len(data) > MAX_CHANNEL_MEDIA_BYTES or used + len(data) > _MAX_CHANNEL_ATTACHMENT_STORAGE_BYTES:
            raise RuntimeError("角色渠道附件存储已达到上限")
        attachment = save_attachment(root, name, mime_type, data)
        _channel_attachment_usage[role_id] = used + len(data)
        return attachment


async def _channel_command_reply(thread, command: str, sender_id: str) -> str:
    from .channels.commands import identifier_reply

    if command != "stop":
        return identifier_reply(
            command, channel=thread.channel, account_id=thread.account_id,
            chat_id=thread.chat_id, sender_id=sender_id,
        )
    active = session_store.active_run(thread.role_id)
    if active is not None and active.sessionKey == thread.session_key:
        if runtime_manager.cancel_run(active.runId):
            return "已停止当前回复。"
    return "当前会话没有正在生成的回复。"


async def _handle_channel_control_command(event) -> bool:
    from .channels.commands import control_command

    command = control_command(event.content)
    if command is None:
        return False
    account = multichannel_store.get_account(event.account_id)
    if account is None or not account.enabled or event.sender_id in account.blocked_senders:
        return True
    if (event.chat_type is ChatType.PRIVATE and not account.allow_private) or (
        event.chat_type is ChatType.GROUP and not account.allow_group
    ):
        return True
    thread = multichannel_store.ensure_channel_thread(
        account.role_id, event.channel, event.account_id, event.chat_id, event.chat_type.value,
        title=event.group_name or event.sender_name or event.chat_id,
    )
    text = await _channel_command_reply(thread, command, event.sender_id)
    target = target_from_chat(
        account_id=event.account_id, chat_id=event.chat_id,
        kind=TargetKind.GROUP if event.chat_type is ChatType.GROUP else TargetKind.PRIVATE,
        display_name=thread.title,
    )
    multichannel_store.remember_targets(account.role_id, [target])
    await channel_host.account_send(
        account.role_id, event.account_id, target.id, text, actor="reply",
        source_thread_id=thread.thread_id, reply_to_message_id=event.external_message_id,
        record_message=False,
    )
    return True


async def _handle_channel_event(event) -> None:
    """Process one account's events serially to make dedupe and attachment
    persistence atomic from the event loop's point of view."""

    if await _handle_channel_control_command(event):
        return
    lock = channel_event_locks.setdefault(event.account_id, asyncio.Lock())
    async with lock:
        await _handle_channel_event_locked(event)


async def _handle_channel_event_locked(event) -> None:
    account = multichannel_store.get_account(event.account_id)
    if account is None or not account.enabled or account.role_id not in {role.id for role in store.list()}:
        return
    if account.role_id in _channel_roles_deleting:
        adapter = channel_adapters.get(account.account_id)
        if adapter is not None and event.triggered:
            await channel_host.send_retry_notice(adapter, event)
        return
    _refresh_channel_avatars(account, event.sender_id, event.chat_id, event.chat_type.value)
    if event.chat_type is ChatType.GROUP:
        listening = multichannel_store.group_listening(account.role_id, event.account_id, event.chat_id)
        if not event.triggered and not listening:
            return
    thread = multichannel_store.ensure_channel_thread(
        account.role_id,
        event.channel,
        event.account_id,
        event.chat_id,
        event.chat_type.value,
        title=event.group_name or event.sender_name or event.chat_id,
        metadata={"senderId": event.sender_id},
    )
    if multichannel_store.has_external_event(thread.thread_id, event.external_message_id):
        return
    scope = _channel_identity_scope(event.channel)
    from .channels.commands import pairing_code
    code = pairing_code(event.content) if event.chat_type is ChatType.PRIVATE else None
    if code is not None:
        paired = multichannel_store.consume_pairing_code(
            code,
            role_id=account.role_id,
            channel=event.channel,
            scope=scope,
            platform_user_id=event.sender_id,
            account_id=event.account_id,
            chat_id=event.chat_id,
        )
        if paired:
            target_kind = TargetKind.PRIVATE
            target = target_from_chat(
                account_id=event.account_id,
                chat_id=event.chat_id,
                kind=target_kind,
                display_name=event.sender_name,
            )
            try:
                await channel_host.account_send(
                    account.role_id,
                    event.account_id,
                    target.id,
                    "配对成功。",
                    actor="owner",
                    record_message=False,
                )
            except Exception as error:
                logger.warning("渠道配对确认发送失败 account=%s error=%s", event.account_id, type(error).__name__)
        return
    owner = multichannel_store.identity_is_owner(
        role_id=account.role_id,
        channel=event.channel,
        scope=scope,
        platform_user_id=event.sender_id,
        account_id=event.account_id,
    )
    if owner and event.chat_type is ChatType.PRIVATE:
        multichannel_store.remember_owner_chat(
            role_id=account.role_id,
            channel=event.channel,
            scope=scope,
            platform_user_id=event.sender_id,
            account_id=event.account_id,
            chat_id=event.chat_id,
        )
    enrich_reply = getattr(channel_adapters.get(event.account_id), "enrich_reply", None)
    if event.triggered and callable(enrich_reply):
        event = await enrich_reply(event)
    attachments = await _persist_channel_attachments(event, account.role_id)
    quoted_attachments = event.metadata.get("quotedAttachments", ())
    saved_quote = await _persist_channel_attachments(replace(event, attachments=tuple(quoted_attachments)), account.role_id) if quoted_attachments else []
    content = event.content.strip() or ("[收到附件]" if event.attachments else "")
    metadata: dict[str, object] = {
        "platformMessageId": event.external_message_id,
        "senderId": event.sender_id,
        "senderName": event.sender_name,
        "chatType": event.chat_type.value,
        "triggered": event.triggered,
        "mentioned": event.mentioned,
        "mentionedIds": list(event.mentioned_ids),
        "replyTo": event.reply_to_message_id,
        "replyToSenderId": event.reply_to_sender_id,
        "replyToContent": str(event.metadata.get("replyToContent") or ""),
        "replyToSenderName": str(event.metadata.get("replyToSenderName") or ""),
        "quotedAttachments": saved_quote,
        "attachments": attachments,
        "listening": event.chat_type is ChatType.GROUP and not event.triggered,
        "senderIsOwner": owner,
    }
    if event.chat_type is ChatType.GROUP and not event.triggered:
        heard = multichannel_store.listening.hear(thread, content, event.external_message_id, metadata)
        if heard is not None and heard["sequence"] is not None:
            channel_memory_maintenance.heard(thread)
        return
    accepted, stored = multichannel_store.append_event(
        thread,
        direction="inbound",
        content=content,
        external_message_id=event.external_message_id,
        metadata=metadata,
    )
    if not accepted:
        return
    if event.chat_type is ChatType.GROUP and not event.triggered:
        return
    await _handle_channel_message(
        thread,
        content,
        {**metadata, "messageId": stored.id},
        owner,
    )


def _require_channel_role(role_id: str):
    if role_id in _channel_roles_deleting:
        raise HTTPException(status_code=409, detail="角色正在删除，请稍后重试")
    role = store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return role


@app.get("/api/roles/{role_id}/channels/accounts")
def list_channel_accounts(role_id: str) -> dict[str, object]:
    _require_channel_role(role_id)
    return {"accounts": [_channel_account_payload(item) for item in multichannel_store.list_role_accounts(role_id)]}


@app.put("/api/roles/{role_id}/channels/telegram")
async def save_telegram_account(role_id: str, data: dict[str, object]) -> dict[str, object]:
    existing = multichannel_store.get_role_account(role_id, "telegram")
    account_id = str(data.get("accountId") or (existing.account_id if existing else f"telegram-{uuid.uuid4().hex}"))
    payload = dict(data)
    payload["accountId"] = account_id
    async with _channel_lifecycle(account_id):
        token = _channel_handover_accounts.set(_channel_handover_accounts.get() | ({account_id} if data.get("enabled") is True else set()))
        try:
            return await _save_telegram_account_unlocked(role_id, payload)
        finally:
            _channel_handover_accounts.reset(token)


async def _save_telegram_account_unlocked(role_id: str, data: dict[str, object]) -> dict[str, object]:
    _require_channel_role(role_id)
    previous = multichannel_store.get_role_account(role_id, "telegram")
    previous_token = multichannel_store.token(previous.account_id) if previous is not None else ""
    account_id = str(data.get("accountId") or (previous.account_id if previous else ""))
    was_enabled = bool(previous and previous.enabled)
    token = data.get("token")
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise HTTPException(status_code=422, detail="Telegram Bot Token 不能为空")
    blocked = data.get("blockedSenders", [])
    if not isinstance(blocked, list) or any(not isinstance(item, str) for item in blocked):
        raise HTTPException(status_code=422, detail="屏蔽发送者格式无效")
    for key in ("enabled", "allowPrivate", "allowGroup"):
        if key in data and not isinstance(data[key], bool):
            raise HTTPException(status_code=422, detail=f"{key} 必须是布尔值")
    if previous is not None:
        try:
            await _stop_channel_account_unlocked(previous.account_id)
        except Exception as error:
            try:
                await channel_host.resume(previous.account_id)
            except Exception:
                logger.warning("Telegram 旧连接停止失败后的恢复失败 account=%s", previous.account_id, exc_info=True)
            raise HTTPException(status_code=502, detail="旧 Telegram 连接停止失败，配置未修改") from error
    try:
        account = multichannel_store.save_account(
            role_id,
            account_id=account_id or None,
            token=token,
            enabled=bool(data.get("enabled", False)),
            allow_private=bool(data.get("allowPrivate", True)),
            allow_group=bool(data.get("allowGroup", False)),
            blocked_senders=list(blocked),
            display_name=str(data.get("displayName", "")),
        )
    except (ValueError, RuntimeError, sqlite3.IntegrityError) as error:
        if previous is not None and was_enabled:
            try:
                await _start_channel_account_unlocked(previous.account_id)
            except Exception:
                logger.warning("Telegram 旧配置恢复启动失败 account=%s", previous.account_id, exc_info=True)
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        if account.enabled and multichannel_store.token(account.account_id):
            await _start_channel_account(account.account_id)
        else:
            await _stop_channel_account_unlocked(account.account_id)
    except Exception as error:
        # Restore the previous Telegram row/token when a new poller cannot
        # start.  ``save_account`` already restored the token on persistence
        # failure; this covers transport/runtime failures after persistence.
        try:
            if previous is None:
                multichannel_store.delete_account(account.account_id)
            else:
                multichannel_store.save_account(
                    previous.role_id,
                    account_id=previous.account_id,
                    token=previous_token,
                    enabled=previous.enabled,
                    allow_private=previous.allow_private,
                    allow_group=previous.allow_group,
                    blocked_senders=list(previous.blocked_senders),
                    display_name=previous.display_name,
                )
            if previous is not None and previous.enabled:
                await _start_channel_account_unlocked(previous.account_id)
        except Exception:
            logger.warning("Telegram 配置回滚失败 account=%s", account.account_id, exc_info=True)
        raise HTTPException(status_code=502, detail="Telegram 连接启动失败，已恢复旧配置") from error
    return {"account": _channel_account_payload(account)}


@app.put("/api/roles/{role_id}/channels/{channel}")
async def save_platform_channel_account(role_id: str, channel: str, data: dict[str, object]) -> dict[str, object]:
    _require_channel_role(role_id)
    if channel not in {"qq", "qqbot", "feishu"}:
        raise HTTPException(status_code=404, detail="渠道类型不存在")
    existing = multichannel_store.get_role_account(role_id, channel)
    account_id = str(data.get("accountId") or (existing.account_id if existing else f"{channel}-{uuid.uuid4().hex}"))
    payload = dict(data)
    payload["accountId"] = account_id
    async with _channel_lifecycle(account_id):
        token = _channel_handover_accounts.set(_channel_handover_accounts.get() | ({account_id} if data.get("enabled") is True else set()))
        try:
            return await _save_platform_channel_account_unlocked(role_id, channel, payload)
        finally:
            _channel_handover_accounts.reset(token)


async def _save_platform_channel_account_unlocked(role_id: str, channel: str, data: dict[str, object]) -> dict[str, object]:
    _require_channel_role(role_id)
    if channel not in {"qq", "qqbot", "feishu"}:
        raise HTTPException(status_code=404, detail="渠道类型不存在")
    existing = multichannel_store.get_role_account(role_id, channel)
    account_id = str(data.get("accountId") or (existing.account_id if existing else f"{channel}-{uuid.uuid4().hex}"))
    if existing is not None and account_id != existing.account_id:
        raise HTTPException(status_code=422, detail="已有渠道账号不能更换账号 ID")
    old_config = multichannel_store.channel_config(existing.account_id) if existing else {}
    old_credentials = {
        name: multichannel_store.credentials.get(credential_key(channel, account_id, name))
        for name in _channel_credential_names(channel)
    }
    config: dict[str, object] = {}
    default_endpoint = {
        "qq": "ws://127.0.0.1:6700",
        "qqbot": "https://api.sgroup.qq.com",
        "feishu": "https://open.feishu.cn/open-apis",
    }[channel]
    endpoint = data.get("endpoint", (multichannel_store.channel_config(account_id).get("endpoint") if existing else None) or default_endpoint)
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise HTTPException(status_code=422, detail="连接地址不能为空")
    endpoint = endpoint.strip()
    if channel == "qq" and not endpoint.startswith(("ws://", "wss://")):
        raise HTTPException(status_code=422, detail="NapCat 地址必须使用 ws:// 或 wss://")
    if channel != "qq" and not endpoint.startswith(("https://",)):
        raise HTTPException(status_code=422, detail="平台 API 地址必须使用 HTTPS")
    config["endpoint"] = endpoint
    secrets_by_name = {
        "qq": {"accessToken": "access_token"},
        "qqbot": {"clientSecret": "client_secret"},
        "feishu": {"appSecret": "app_secret", "verificationToken": "verification_token", "encryptKey": "encrypt_key"},
    }[channel]
    if channel in {"qqbot", "feishu"}:
        app_id = data.get("appId", old_config.get("appId") if existing else "")
        if not isinstance(app_id, str) or not app_id.strip():
            raise HTTPException(status_code=422, detail="App ID 不能为空")
        config["appId"] = app_id.strip()
    blocked = data.get("blockedSenders", list(existing.blocked_senders) if existing else [])
    if not isinstance(blocked, list) or any(not isinstance(item, str) for item in blocked):
        raise HTTPException(status_code=422, detail="屏蔽发送者格式无效")
    enabled = data.get("enabled", existing.enabled if existing else False)
    allow_private = data.get("allowPrivate", existing.allow_private if existing else True)
    allow_group = data.get("allowGroup", existing.allow_group if existing else channel == "qq")
    if not all(isinstance(item, bool) for item in (enabled, allow_private, allow_group)):
        raise HTTPException(status_code=422, detail="渠道开关必须是布尔值")

    # Validate all plain fields before touching the credential vault.  The
    # remaining mutation is wrapped below so partial secret writes are rolled
    # back if persistence or connection startup fails.
    for input_name, credential_name in secrets_by_name.items():
        secret = data.get(input_name)
        if secret is not None:
            if not isinstance(secret, str):
                raise HTTPException(status_code=422, detail=f"{input_name} 格式无效")
            if not secret.strip() and existing is None:
                raise HTTPException(status_code=422, detail=f"{input_name} 不能为空")

    was_enabled = bool(existing and existing.enabled)
    if existing is not None:
        try:
            await _stop_channel_account(account_id)
        except Exception as error:
            # Keep the old configuration live when the transport cannot be
            # drained.  The account remains the source of truth; resuming the
            # admission gate avoids stranding it in a permanently paused state.
            try:
                await channel_host.resume(account_id)
            except Exception:
                logger.warning("渠道账号停止失败后的恢复失败 account=%s", account_id, exc_info=True)
            raise HTTPException(status_code=502, detail="旧渠道连接停止失败，配置未修改") from error
        channel_host.unregister(account_id)
        channel_adapters.pop(account_id, None)
        channel_transports.pop(account_id, None)

    async def rollback() -> None:
        try:
            current = multichannel_store.get_account(account_id)
            if existing is None:
                if current is not None:
                    multichannel_store.delete_account(account_id)
            else:
                multichannel_store.register_channel_account(
                    role_id,
                    channel,
                    account_id=existing.account_id,
                    enabled=existing.enabled,
                    allow_private=existing.allow_private,
                    allow_group=existing.allow_group,
                    blocked_senders=list(existing.blocked_senders),
                    display_name=existing.display_name,
                    platform_id=existing.platform_id,
                )
                multichannel_store.set_channel_config(account_id, old_config)
                multichannel_store.set_account_state(account_id, enabled=existing.enabled, last_error=existing.last_error)
        except Exception:
            logger.warning("渠道账号配置回滚失败 account=%s", account_id, exc_info=True)
        for name, value in old_credentials.items():
            try:
                multichannel_store.credentials.set(credential_key(channel, account_id, name), value)
            except Exception:
                logger.warning("渠道凭据回滚失败 account=%s name=%s", account_id, name, exc_info=True)
        if existing is not None and was_enabled:
            try:
                await _start_channel_account(account_id)
            except Exception:
                logger.warning("渠道账号旧配置恢复启动失败 account=%s", account_id, exc_info=True)

    try:
        for input_name, credential_name in secrets_by_name.items():
            secret = data.get(input_name)
            if isinstance(secret, str) and secret.strip():
                multichannel_store.credentials.set(credential_key(channel, account_id, credential_name), secret.strip())
        account = multichannel_store.register_channel_account(
            role_id,
            channel,
            account_id=account_id,
            enabled=enabled,
            allow_private=allow_private,
            allow_group=allow_group if channel == "qq" else False,
            blocked_senders=blocked,
            display_name=str(data.get("displayName", existing.display_name if existing else "")),
            platform_id=str(config.get("appId") or "") or None,
        )
        multichannel_store.set_channel_config(account_id, config)
        if account.enabled and not _channel_credentials_configured(account):
            raise HTTPException(status_code=422, detail="请先配置所需的渠道凭据")
        if account.enabled:
            await _start_channel_account(account_id)
        return {"account": _channel_account_payload(multichannel_store.get_account(account_id) or account)}
    except HTTPException:
        await rollback()
        raise
    except (ValueError, sqlite3.IntegrityError) as error:
        await rollback()
        raise HTTPException(status_code=422, detail="渠道账号配置无效") from error
    except RuntimeError as error:
        await rollback()
        raise HTTPException(status_code=502, detail="渠道连接启动失败，已恢复旧配置") from error
    except Exception as error:
        await rollback()
        raise HTTPException(status_code=502, detail="渠道连接启动失败，已恢复旧配置") from error


@app.post("/api/roles/{role_id}/channels/{channel}/{account_id}/test")
async def test_platform_channel_account(role_id: str, channel: str, account_id: str) -> dict[str, object]:
    if channel == "telegram":
        return await test_telegram_account(role_id, account_id)
    _require_channel_role(role_id)
    account = multichannel_store.get_account(account_id)
    if account is None or account.role_id != role_id or account.channel != channel:
        raise HTTPException(status_code=404, detail="渠道账号不存在")
    adapter: ChannelAdapter | None = None
    try:
        # A connection test must not replace or start the account's live
        # adapter.  Build an unregistered candidate and always retire its
        # transport below so a failed test cannot leak a socket or HTTP
        # client into the process.
        adapter = _build_channel_adapter(account, register=False)
        if channel == "qq":
            await adapter.start()
            transport = getattr(adapter, "_transport", None)
            for _ in range(30):
                if bool(getattr(transport, "connected", False)):
                    break
                await asyncio.sleep(0.1)
            if not bool(getattr(transport, "connected", False)):
                raise RuntimeError("NapCat WebSocket 连接失败")
        elif channel == "qqbot":
            transport = getattr(adapter, "_transport", None)
            await transport._gateway_url()
        elif channel == "feishu":
            transport = getattr(adapter, "_transport", None)
            await transport._tenant_access_token()
        else:
            raise RuntimeError("不支持的渠道类型")
        multichannel_store.set_account_state(account_id, last_error=None)
        return {"ok": True, "account": _channel_account_payload(account)}
    except Exception as error:
        multichannel_store.set_account_state(account_id, last_error=type(error).__name__)
        raise HTTPException(status_code=502, detail=f"{channel} 连接测试失败") from error
    finally:
        if adapter is not None:
            try:
                await adapter.stop()
            except Exception:
                logger.debug("渠道连接测试清理失败 account=%s", account_id, exc_info=True)


@app.post("/api/channels/feishu/{account_id}/webhook")
async def feishu_channel_webhook(account_id: str, request: Request) -> Response:
    """Receive the raw Feishu event body for one configured bot account."""

    account = multichannel_store.get_account(account_id)
    if account is None or account.channel != "feishu":
        raise HTTPException(status_code=404, detail="飞书渠道账号不存在")
    if not account.enabled:
        raise HTTPException(status_code=409, detail="飞书渠道账号未启用")
    if not _channel_credentials_configured(account) or not any(
        _channel_secret(account, key) for key in ("verification_token", "encrypt_key")
    ):
        raise HTTPException(status_code=503, detail="飞书 webhook 校验凭据未配置")
    adapter = channel_adapters.get(account_id)
    if adapter is None:
        try:
            adapter = _build_channel_adapter(account)
            await channel_host.start(account_id)
        except Exception as error:
            raise HTTPException(status_code=503, detail="飞书渠道连接尚未就绪") from error
    transport = channel_transports.get(account_id)
    if not isinstance(transport, FeishuWebhookTransport):
        raise HTTPException(status_code=503, detail="飞书渠道传输不可用")
    try:
        result = await transport.handle_webhook(await request.body(), request.headers)
    except ChannelTransportError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="飞书 webhook 内容格式错误") from error
    return Response(content=json.dumps(result, ensure_ascii=False), media_type="application/json")


@app.post("/api/roles/{role_id}/channels/telegram/{account_id}/test")
async def test_telegram_account(role_id: str, account_id: str) -> dict[str, object]:
    _require_channel_role(role_id)
    account = multichannel_store.get_account(account_id)
    if account is None or account.role_id != role_id or account.channel != "telegram":
        raise HTTPException(status_code=404, detail="渠道账号不存在")
    try:
        result = await _telegram_service(account_id).test_account(account_id)
    except Exception as error:
        multichannel_store.set_account_state(account_id, last_error=type(error).__name__)
        raise HTTPException(status_code=502, detail="Telegram 连接测试失败") from error
    current = multichannel_store.get_account(account_id)
    assert current is not None
    return {"ok": True, "account": _channel_account_payload(current), "bot": dict(result)}


@app.post("/api/roles/{role_id}/channels/accounts/{account_id}/state")
async def set_channel_account_state(role_id: str, account_id: str, data: dict[str, object]) -> dict[str, object]:
    _require_channel_role(role_id)
    account = multichannel_store.get_account(account_id)
    if account is None or account.role_id != role_id:
        raise HTTPException(status_code=404, detail="渠道账号不存在")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="enabled 必须是布尔值")
    if enabled:
        updated = multichannel_store.set_account_state(account_id, enabled=True, last_error=None)
        if _channel_credentials_configured(updated):
            try:
                await _start_channel_account(account_id)
            except Exception as error:
                try:
                    await _stop_channel_account(account_id)
                except Exception:
                    logger.debug("启用失败后的渠道清理失败 account=%s", account_id, exc_info=True)
                channel_host.unregister(account_id)
                channel_adapters.pop(account_id, None)
                channel_transports.pop(account_id, None)
                updated = multichannel_store.set_account_state(account_id, enabled=False, last_error=type(error).__name__)
                raise HTTPException(status_code=502, detail="渠道连接启动失败，账号已停用") from error
        else:
            await _stop_channel_account(account_id)
            multichannel_store.set_account_state(account_id, enabled=False, last_error="credentials_missing")
            updated = multichannel_store.get_account(account_id) or updated
    else:
        try:
            await _stop_channel_account(account_id)
        except Exception as error:
            # Stop failures must not leave an enabled account that will be
            # resurrected on the next process start.
            updated = multichannel_store.set_account_state(
                account_id, enabled=False, last_error=type(error).__name__
            )
            raise HTTPException(status_code=503, detail="渠道连接停止失败，账号已停用，请重试") from error
        updated = multichannel_store.set_account_state(account_id, enabled=False, last_error=None)
    return {"account": _channel_account_payload(updated)}


@app.delete("/api/roles/{role_id}/channels/accounts/{account_id}", status_code=204)
async def delete_channel_account(role_id: str, account_id: str) -> None:
    _require_channel_role(role_id)
    account = multichannel_store.get_account(account_id)
    if account is None or account.role_id != role_id:
        raise HTTPException(status_code=404, detail="渠道账号不存在")
    stop_error: Exception | None = None
    try:
        await _stop_channel_account(account_id)
    except Exception as error:
        # The admission gate is paused before transport shutdown.  Persist the
        # disabled state even when a platform socket refuses to close so a
        # restart cannot resurrect an account that is being deleted.
        stop_error = error
    multichannel_store.set_account_state(account_id, enabled=False, last_error=type(stop_error).__name__ if stop_error else None)
    if stop_error is not None:
        raise HTTPException(status_code=503, detail="渠道连接停止失败，账号已停用，请重试删除") from stop_error
    try:
        multichannel_store.delete_account(account_id)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail="渠道凭据清理失败，账号已停用，请重试删除") from error
    telegram_services.pop(account_id, None)
    channel_adapters.pop(account_id, None)
    channel_transports.pop(account_id, None)
    channel_host.unregister(account_id)
    _channel_push_service().clear_account(account_id)


def _channel_push_service() -> ChannelPushService:
    global _push_service_cache
    if _push_service_cache is None or _push_service_cache.store is not multichannel_store or _push_service_cache.host is not channel_host:
        _push_service_cache = ChannelPushService(multichannel_store, channel_host)
    return _push_service_cache


def _deliver_todo_channel_reminder(role_id: str, event_id: str, content: str) -> None:
    service = _channel_push_service()
    if not any(target["enabled"] for target in service.list_targets(role_id)):
        return
    loop = _channel_runtime_loop
    if loop is None or not loop.is_running():
        raise RuntimeError("渠道运行时尚未启动，桌面提醒已保存")
    future = asyncio.run_coroutine_threadsafe(service.deliver_reminder(role_id, event_id, content), loop)
    outcomes = future.result(timeout=30)
    failures = [str(item["status"]) for item in outcomes if item["status"] != "sent"]
    if failures:
        raise RuntimeError("渠道提醒投递结果：" + "、".join(failures))


@app.get("/api/roles/{role_id}/channels/push-targets")
def list_channel_push_targets(role_id: str) -> dict[str, object]:
    _require_channel_role(role_id)
    return {"targets": _channel_push_service().list_targets(role_id)}


@app.put("/api/roles/{role_id}/channels/accounts/{account_id}/push-target")
def set_channel_push_target(role_id: str, account_id: str, data: dict[str, object]) -> dict[str, object]:
    _require_channel_role(role_id)
    target_id = data.get("targetId")
    if target_id is not None and (not isinstance(target_id, str) or not target_id):
        raise HTTPException(status_code=422, detail="targetId 必须是主人目录 ID 或 null")
    try:
        _channel_push_service().configure(role_id, account_id, target_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="渠道账号不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"targets": _channel_push_service().list_targets(role_id)}


@app.get("/api/channels/identities")
def list_channel_identities() -> dict[str, object]:
    return {"identities": multichannel_store.list_identities()}


@app.delete("/api/channels/identities/{identity_id}", status_code=204)
def unbind_channel_identity(identity_id: str) -> Response:
    try:
        multichannel_store.unbind_identity(identity_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="渠道身份绑定不存在") from error
    return Response(status_code=204)


@app.post("/api/roles/{role_id}/channels/telegram/pairing")
def create_telegram_pairing_code(role_id: str) -> dict[str, object]:
    _require_channel_role(role_id)
    account = multichannel_store.get_role_account(role_id, "telegram")
    if account is None:
        raise HTTPException(status_code=409, detail="请先配置 Telegram 账号")
    return {"code": multichannel_store.create_pairing_code(role_id, "telegram"), "expiresInSeconds": 600}


@app.post("/api/roles/{role_id}/channels/{channel}/pairing")
def create_channel_pairing_code(role_id: str, channel: str) -> dict[str, object]:
    _require_channel_role(role_id)
    if channel not in {"qq", "qqbot", "feishu"}:
        raise HTTPException(status_code=404, detail="渠道类型不存在")
    account = multichannel_store.get_role_account(role_id, channel)
    if account is None or not account.enabled:
        raise HTTPException(status_code=409, detail="请先启用渠道账号")
    return {"code": multichannel_store.create_pairing_code(role_id, channel), "expiresInSeconds": 600}


@app.get("/api/roles/{role_id}/channels/accounts/{account_id}/targets")
async def list_channel_targets(role_id: str, account_id: str) -> dict[str, object]:
    _require_channel_role(role_id)
    try:
        targets = await channel_host.account_targets(role_id, account_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="渠道账号不存在") from error
    return {"targets": targets}


@app.get("/api/roles/{role_id}/channels/accounts/{account_id}/groups")
def list_channel_groups(role_id: str, account_id: str) -> dict[str, object]:
    _require_channel_role(role_id)
    account = multichannel_store.get_account(account_id)
    if account is None or account.role_id != role_id or account.channel != "qq":
        raise HTTPException(status_code=404, detail="NapCat 账号不存在")
    targets = [item for item in multichannel_store.list_targets(role_id, account_id) if item.kind.value == "group"]
    groups = {str(item.chat_id): {"chatId": str(item.chat_id), "title": item.display_name, "listening": multichannel_store.group_listening(role_id, account_id, item.chat_id)} for item in targets}
    for item in multichannel_store.list_group_settings(role_id, account_id):
        groups.setdefault(str(item["chatId"]), {"chatId": item["chatId"], "title": str(item["chatId"]), "listening": item["listening"]})
    return {"groups": list(groups.values())}


@app.put("/api/roles/{role_id}/channels/accounts/{account_id}/groups/{chat_id}/listening")
def set_channel_group_listening(role_id: str, account_id: str, chat_id: str, data: dict[str, object]) -> dict[str, object]:
    _require_channel_role(role_id)
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="enabled 必须是布尔值")
    try:
        multichannel_store.set_group_listening(role_id, account_id, chat_id, enabled)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="NapCat 账号不存在") from error
    return {"chatId": chat_id, "listening": enabled}


@app.delete("/api/roles/{role_id}/channels/accounts/{account_id}/groups/{chat_id}/listening/messages", status_code=204)
def clear_channel_group_listening_messages(role_id: str, account_id: str, chat_id: str) -> None:
    _require_channel_role(role_id)
    account = multichannel_store.get_account(account_id)
    if account is None or account.role_id != role_id or account.channel != "qq":
        raise HTTPException(status_code=404, detail="NapCat 账号不存在")
    try:
        multichannel_store.clear_listening_messages(role_id, account_id, chat_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="NapCat 群不存在") from error


@app.get("/api/roles/{role_id}/channels/threads")
def list_channel_threads(role_id: str, limit: int = 100) -> dict[str, object]:
    _require_channel_role(role_id)
    threads = multichannel_store.list_threads(role_id, limit=limit)
    return {"threads": [{**item.public_dict(), "avatarUrl": multichannel_store.avatars.url(item.account_id, "chat", item.chat_id)} for item in threads]}


@app.get("/api/channels/avatars/{avatar_id}")
def get_channel_avatar(avatar_id: str) -> Response:
    path = multichannel_store.avatars.path(avatar_id)
    if path is None:
        raise HTTPException(status_code=404, detail="渠道头像不存在")
    return Response(path.read_bytes(), media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


def _channel_message_payload(thread, message) -> dict[str, object]:
    payload = public_message(message).model_dump(mode="json")
    metadata = dict(payload.get("metadata") or {})
    metadata["threadId"] = thread.thread_id
    sender_id = str(metadata.get("senderId") or "")
    metadata["senderAvatarUrl"] = multichannel_store.avatars.url(thread.account_id, "sender", sender_id)
    metadata["senderIsOwner"] = bool(sender_id) and multichannel_store.identity_is_owner(
        role_id=thread.role_id, channel=thread.channel, scope=_channel_identity_scope(thread.channel),
        platform_user_id=sender_id, account_id=thread.account_id,
    )
    source = metadata.get("viaAccount")
    if isinstance(source, dict):
        source = dict(source)
        own_id = str(source.get("platformAccountId") or thread.account_id)
        source["avatarUrl"] = multichannel_store.avatars.url(thread.account_id, "account", own_id)
        metadata["viaAccount"] = source
    payload["metadata"] = metadata
    return payload


@app.get("/api/channels/listening/defaults")
def channel_listening_defaults() -> dict[str, int]:
    return {"defaultDailyCap": multichannel_store.listening.default_cap()}


@app.put("/api/channels/listening/defaults")
def update_channel_listening_defaults(data: dict[str, object]) -> dict[str, int]:
    try:
        cap = multichannel_store.listening.set_default_cap(data.get("defaultDailyCap"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"defaultDailyCap": cap}


@app.get("/api/roles/{role_id}/channels/threads/{thread_id}/listening")
def channel_listening_state(role_id: str, thread_id: str) -> dict[str, object]:
    _require_channel_role(role_id)
    try:
        return multichannel_store.listening.state(role_id, thread_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="当前角色的旁听群不存在") from error


@app.put("/api/roles/{role_id}/channels/threads/{thread_id}/listening/cap")
def update_channel_listening_cap(role_id: str, thread_id: str, data: dict[str, object]) -> dict[str, object]:
    _require_channel_role(role_id)
    try:
        return multichannel_store.listening.set_cap(role_id, thread_id, data.get("dailyCap"))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="当前角色的旁听群不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/roles/{role_id}/channels/threads/{thread_id}/listening/messages")
def list_channel_listening_messages(role_id: str, thread_id: str, before: int | None = None, limit: int = 50) -> dict[str, object]:
    _require_channel_role(role_id)
    try:
        return multichannel_store.listening.page(role_id, thread_id, before=before, limit=limit)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="当前角色的旁听群不存在") from error


@app.get("/api/roles/{role_id}/channels/threads/{thread_id}/messages")
def list_channel_thread_messages(role_id: str, thread_id: str, before: int | None = None, limit: int = 50) -> dict[str, object]:
    _require_channel_role(role_id)
    try:
        thread, messages, has_more, next_before = multichannel_store.list_messages(role_id, thread_id, before=before, limit=limit)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="渠道线程不存在") from error
    return {
        "thread": thread.public_dict(),
        "messages": [_channel_message_payload(thread, item) for item in messages],
        "hasMore": has_more,
        "nextBefore": next_before,
    }


@app.get("/api/roles/{role_id}/channels/deliveries")
def list_channel_deliveries(role_id: str, limit: int = 100) -> dict[str, object]:
    _require_channel_role(role_id)
    return {"deliveries": multichannel_store.list_deliveries(role_id, limit=limit)}


@app.get("/api/roles/{role_id}/channels/events")
async def channel_events(role_id: str) -> StreamingResponse:
    _require_channel_role(role_id)

    async def stream():
        async for payload in multichannel_store.subscribe(role_id):
            yield _event("channel_update", payload)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


def _runtime_tool_candidates(
    role,
    *,
    is_channel_owner: bool = True,
    source_thread_id: str | None = None,
    source_account_id: str | None = None,
    include_channel: bool = False,
) -> tuple[object, ...]:
    """Build every tool available to the host for a role snapshot."""
    shell = role.agentConfig.shell
    tools: list[object] = [
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
    if mail_service.store.connection().auto_draft_ready:
        tools.append(mail_manage_tool)
    if include_channel:
        tools.extend((
            ChannelAccountListTool(channel_host),
            ChannelTargetsTool(channel_host),
            MessagePushTool(
                _channel_push_service(), is_owner=is_channel_owner,
                source_thread_id=source_thread_id, source_account_id=source_account_id,
            ),
            ChannelSendTool(
                channel_host,
                is_owner=is_channel_owner,
                source_thread_id=source_thread_id,
                source_account_id=source_account_id,
            ),
            LookupGroupContextTool(group_repository, is_owner=is_channel_owner),
            UpdateGroupContextTool(group_repository, is_owner=is_channel_owner),
            SetGroupListeningTool(
                group_repository,
                is_owner=is_channel_owner,
                source_thread_id=source_thread_id,
            ),
            LookupGroupMemberTool(group_repository, source_thread_id=source_thread_id),
        ))
    return tuple(tools)


def _role_has_usable_channel_account(role_id: str) -> bool:
    return any(
        account.enabled and _channel_credentials_configured(account)
        for account in multichannel_store.list_role_accounts(role_id)
    )


def _enabled_runtime_tool_names(role, *, include_channel: bool = False) -> set[str]:
    shell = role.agentConfig.shell
    enabled: set[str] = set()
    if shell.enabled and shell.allowedCommands:
        enabled.add("shell")
    if memory_settings_store.get().enabled and role.agentConfig.memoryRecall.enabled:
        enabled.add("recall_memory")
    if owner_knowledge_plugin.status().enabled:
        enabled.add("search_owner_knowledge")
    if mail_service.store.connection().auto_draft_ready:
        enabled.add("mail.manage")
    if include_channel:
        enabled.update({
            "account_list", "account_targets", "account_send", "message_push",
            "lookup_group_context", "update_group_context", "set_group_listening",
            "lookup_group_member",
            "read_attachment",
        })
    return enabled


def _runtime_tools(
    role,
    *,
    candidates: tuple[object, ...] | None = None,
) -> tuple[object, ...]:
    """Filter host tool candidates using the request-time role policy."""
    available = _runtime_tool_candidates(role) if candidates is None else candidates
    enabled = _enabled_runtime_tool_names(
        role,
        include_channel=any(getattr(tool.definition, "name", "") == "account_list" for tool in available),
    )
    return tuple(tool for tool in available if tool.definition.name in enabled)


async def _runtime_capabilities(
    role,
    *,
    session_key: str,
    run_id: str,
    available_tools: tuple[ShellTool | MemoryRecallTool | OwnerKnowledgeTool | MailManageTool, ...] | None = None,
    prompt_text: str = "",
    explicit_skill_ids: list[str] | None = None,
    external_only: bool = False,
) -> CapabilityResolution:
    agent_config = role.agentConfig
    include_channel = session_key.startswith("thread:") or (
        session_key.startswith("role:") and _role_has_usable_channel_account(role.id)
    )
    enabled_tools = _enabled_runtime_tool_names(role, include_channel=include_channel)
    enabled_tools.update(agent_config.enabledTools)
    if not memory_settings_store.get().enabled:
        enabled_tools.discard("recall_memory")
    enabled_plugins = set(agent_config.enabledPlugins)
    todo_enabled = bool(todo_store.settings()["enabled"]) and callable(
        getattr(model_adapter, "stream_structured_messages", None)
    )
    granted_capabilities = set(agent_config.grantedCapabilities)
    host_services: dict[str, object] = {}
    if todo_enabled:
        enabled_plugins.add("todo")
        enabled_tools.update({"todo.write", "todo.reminder_control"})
        granted_capabilities.add("todo.write")
        host_services = {"todo.write": _todo_application()}
    else:
        enabled_plugins.discard("todo")
    if agent_config.browserUse.enabled:
        enabled_plugins.add("browser_use")
        enabled_tools.update(BROWSER_USE_TOOL_NAMES)
        granted_capabilities.add("browser.session")
        host_services["browser.session"] = browser_use_sessions
    else:
        enabled_plugins.discard("browser_use")
    return await CapabilityRegistry(
        _runtime_tool_candidates(role, include_channel=include_channel) if available_tools is None else available_tools,
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
        external_only=external_only,
        prompt_text=prompt_text,
        explicit_skill_ids=set(explicit_skill_ids or ()),
        enabled_plugin_ids=enabled_plugins,
        granted_capabilities=granted_capabilities,
        host_services=host_services,
    )


def _runtime_message_snapshot(message: object) -> dict[str, object]:
    value = asdict(message)
    if not value.get("attachments"):
        value.pop("attachments", None)
    for key in ("timestamp", "usage"):
        if value.get(key) is None:
            value.pop(key, None)
    if not value.get("nested_calls"):
        value.pop("nested_calls", None)
    return value


def _telegram_accepts_update(account_id: str) -> bool:
    account = multichannel_store.get_account(account_id)
    return bool(account and account.role_id not in _channel_roles_deleting and channel_host.accepts_events(account_id))


def _defer_telegram_update(account_id: str, update) -> bool:
    account = multichannel_store.get_account(account_id)
    if account is None or not account.enabled or account.role_id in _channel_roles_deleting:
        return False
    service = _telegram_service(account_id)
    token = multichannel_store.token(account_id)
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str((message.get("chat") or {}).get("id") or "")
    async def reject():
        from .channels.host import CHANNEL_INTAKE_RETRY_NOTICE
        await service.transport.send_message(token, chat_id, CHANNEL_INTAKE_RETRY_NOTICE, reply_to=str(message.get("message_id") or "") or None, **({"message_thread_id": message["message_thread_id"]} if message.get("message_thread_id") else {}))
    return channel_host.intake.defer(account_id, lambda: _dispatch_telegram_update(account_id, update), reject)


async def _dispatch_telegram_update(account_id: str, update) -> None:
    try:
        async with channel_host.event_scope(account_id) as admitted:
            service = _telegram_service(account_id)
            if admitted or not _defer_telegram_update(account_id, update):
                await service.handle_update(account_id, update)
    except Exception as error:
        logger.warning("Telegram 入站处理失败 account=%s error=%s", account_id, type(error).__name__)


def _telegram_service(account_id: str) -> TelegramChannelService:
    service = telegram_services.get(account_id)
    if service is None:
        service = TelegramChannelService(
            multichannel_store,
            account_id,
            on_message=_handle_telegram_channel_message,
            persist_attachment=_save_channel_attachment,
            on_command=_channel_command_reply,
            on_admit=_telegram_accepts_update,
            on_defer=_defer_telegram_update,
            on_update=_dispatch_telegram_update,
        )
        telegram_services[account_id] = service
    return service


async def _handle_telegram_channel_message(thread, content: str, metadata: dict[str, object], owner: bool) -> None:
    if thread.role_id in _channel_roles_deleting:
        await _notify_channel_turn_not_started(thread, metadata)
        return
    async with channel_host.event_scope(thread.account_id) as admitted:
        if admitted:
            account = multichannel_store.get_account(thread.account_id)
            if account:
                _refresh_channel_avatars(account, str(metadata.get("senderId") or ""), thread.chat_id, thread.chat_type)
            await _handle_channel_message(thread, content, metadata, owner)
        else:
            from .channels.host import CHANNEL_INTAKE_RETRY_NOTICE
            service = _telegram_service(thread.account_id)
            await service.transport.send_message(
                multichannel_store.token(thread.account_id), thread.chat_id, CHANNEL_INTAKE_RETRY_NOTICE,
                reply_to=str(metadata.get("platformMessageId") or "") or None,
                **({"message_thread_id": metadata["messageThreadId"]} if metadata.get("messageThreadId") else {}),
            )


async def _notify_channel_turn_not_started(thread, metadata: dict[str, object]) -> None:
    """Tell an admitted sender their turn cannot start during role deletion."""

    from .channels.models import InboundEvent
    from .channels.host import CHANNEL_INTAKE_RETRY_NOTICE

    adapter = channel_adapters.get(thread.account_id)
    try:
        if adapter is not None:
            event = InboundEvent(
                channel=thread.channel, account_id=thread.account_id,
                external_event_id=str(metadata.get("platformMessageId") or ""),
                external_message_id=str(metadata.get("platformMessageId") or ""),
                chat_id=thread.chat_id, chat_type=ChatType.GROUP if thread.chat_type == "group" else ChatType.PRIVATE,
                sender_id=str(metadata.get("senderId") or ""),
            )
            await channel_host.send_retry_notice(adapter, event)
        elif thread.channel == "telegram" and thread.account_id in telegram_services:
            service = telegram_services[thread.account_id]
            await service.transport.send_message(multichannel_store.token(thread.account_id), thread.chat_id, CHANNEL_INTAKE_RETRY_NOTICE)
    except Exception as error:
        logger.warning("角色删除期间未启动回合提示失败 role=%s error=%s", thread.role_id, type(error).__name__)


async def _start_telegram_account(account_id: str) -> None:
    account = multichannel_store.get_account(account_id)
    if account is not None and not _channel_owner_is_available(account):
        multichannel_store.set_account_state(account_id, enabled=False, last_error="role_unavailable")
        return
    existing = telegram_tasks.get(account_id)
    if existing is not None and not existing.done():
        return
    account = multichannel_store.get_account(account_id)
    if account is None or not account.enabled or not multichannel_store.token(account_id):
        return
    await _telegram_service(account_id).test_account(account_id)
    stop = asyncio.Event()
    telegram_stops[account_id] = stop
    telegram_tasks[account_id] = asyncio.create_task(_telegram_service(account_id).run_polling(stop))


async def _stop_telegram_account(account_id: str) -> None:
    stop = telegram_stops.get(account_id)
    if stop is not None:
        stop.set()
    task = telegram_tasks.get(account_id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
        telegram_tasks.pop(account_id, None)
    telegram_stops.pop(account_id, None)


async def _start_channel_account_unlocked(account_id: str) -> None:
    account = multichannel_store.get_account(account_id)
    if account is None or not account.enabled:
        return
    if not _channel_owner_is_available(account):
        multichannel_store.set_account_state(account_id, enabled=False, last_error="role_unavailable")
        return
    if not _channel_credentials_configured(account):
        multichannel_store.set_account_state(account_id, enabled=False, last_error="credentials_missing")
        return
    _build_channel_adapter(account)
    if account.channel == "telegram":
        await _start_telegram_account(account_id)
    else:
        await channel_host.start(account_id)
        if account.channel == "qq":
            transport = channel_transports[account_id]
            await transport.wait_connected()
            identity = await transport("get_login_info", {})
            platform_id = str(identity.get("user_id") or "")
            if not platform_id:
                raise RuntimeError("NapCat 未返回 QQ 登录账号 ID")
            multichannel_store.set_platform_identity(
                account_id, platform_id, display_name=str(identity.get("nickname") or ""),
            )
    await channel_host.resume(account_id)
    multichannel_store.set_account_state(account_id, last_error=None)
    current = multichannel_store.get_account(account_id)
    if current:
        _refresh_channel_avatars(current)


async def _stop_channel_account_unlocked(account_id: str) -> None:
    await multichannel_store.avatars.stop_account(account_id)
    await channel_host.pause_and_drain(account_id)
    if account_id not in _channel_handover_accounts.get():
        await channel_host.intake.close(account_id)
    await _stop_telegram_account(account_id)
    try:
        await channel_host.stop(account_id)
    except KeyError:
        pass


async def _start_channel_account(account_id: str) -> None:
    account = multichannel_store.get_account(account_id)
    if account is not None and not _channel_owner_is_available(account):
        multichannel_store.set_account_state(account_id, enabled=False, last_error="role_unavailable")
        return
    async with _channel_lifecycle(account_id):
        await _start_channel_account_unlocked(account_id)


def _channel_owner_is_available(account: ChannelAccount) -> bool:
    return store.get(account.role_id) is not None and account.role_id not in store.pending_role_deletions()


async def _stop_channel_account(account_id: str) -> None:
    async with _channel_lifecycle(account_id):
        await _stop_channel_account_unlocked(account_id)


async def _start_channel_workers() -> None:
    await channel_memory_maintenance.start()
    interrupted = multichannel_store.mark_interrupted_deliveries_uncertain()
    if interrupted:
        logger.warning("启动时发现未确认渠道投递 count=%s，已标记为 uncertain", interrupted)
    for account in multichannel_store.list_role_accounts_for_all_roles():
        if not _channel_owner_is_available(account):
            multichannel_store.set_account_state(account.account_id, enabled=False, last_error="role_unavailable")
            continue
        if not account.enabled:
            continue
        try:
            await _start_channel_account(account.account_id)
        except Exception as error:
            try:
                await _stop_channel_account(account.account_id)
            except Exception:
                logger.debug("启动失败后的渠道清理失败 account=%s", account.account_id, exc_info=True)
            channel_host.unregister(account.account_id)
            channel_adapters.pop(account.account_id, None)
            channel_transports.pop(account.account_id, None)
            multichannel_store.set_account_state(account.account_id, enabled=False, last_error=type(error).__name__)
            logger.warning("渠道账号启动失败 account=%s error=%s", account.account_id, type(error).__name__)


async def _close_channel_workers() -> None:
    account_ids = set(channel_adapters) | set(telegram_tasks)
    for account_id in account_ids:
        try:
            await _stop_channel_account(account_id)
        except Exception as error:
            logger.warning("渠道账号关闭失败 account=%s error=%s", account_id, type(error).__name__)
    await channel_memory_maintenance.close()


# FastAPI 0.136 removed ``add_event_handler`` from the application object.
# Register alongside the existing constructor-based lifecycle callbacks.
async def _summarize_channel_memory(thread, messages, context, profiles):
    role = store.get(thread.role_id)
    if role is None:
        raise KeyError(thread.role_id)
    configuration = model_configuration_store.get(role.modelConfigurationId)
    return await asyncio.to_thread(_complete_model_json, [
        {"role": "system", "content": "只整理以下外部会话中可证实的内容，不把群友当主人。返回JSON：{groupNote:string,summary:string,members:[{senderId:string,name:string,profile:string,shorthand:string}]}。更新而非丢弃既有笔记/档案；senderId必须来自输入；不要推测身份。"},
        {"role": "user", "content": json.dumps({"channel": thread.channel, "chatType": thread.chat_type, "context": context, "profiles": profiles, "messages": messages}, ensure_ascii=False)},
    ], configuration, max_tokens=1500)


def _ingest_owner_channel_memory(thread, user_message, assistant_message):
    if store.get(thread.role_id) is None or memory_worker.role_deletion_blocked(thread.role_id):
        return
    memory_worker.service.process_turn(thread.role_id, thread.session_key, user_message, assistant_message)
    if memory_worker.maintenance is not None:
        memory_worker.maintenance.maintain(thread.role_id, f"role:{thread.role_id}")
        memory_worker.maintenance.sync_structured_memory(thread.role_id, memory_worker.service.store.list_all(thread.role_id))


async def _ingest_owner_listening_memory(thread, messages):
    if memory_worker.role_deletion_blocked(thread.role_id):
        return
    role = store.get(thread.role_id)
    if role is None:
        return
    source_key = f"listening:{thread.thread_id}:{messages[0]['sequence']}-{messages[-1]['sequence']}"
    configuration = model_configuration_store.get(role.modelConfigurationId)
    candidates = await asyncio.to_thread(_complete_model_json, [
        {"role": "system", "content": "以下都是已经识别的主人在群中的发言。只提取主人明确表达的稳定事实、偏好或共同经历，不推测群友信息。返回JSON {memories:[{memoryType:fact|preference|profile|procedure|event,summary:string,messageIds:[string]}]}，messageIds必须来自输入。"},
        {"role": "user", "content": json.dumps({"group": thread.title, "messages": messages}, ensure_ascii=False)},
    ], configuration, max_tokens=900)
    if not isinstance(candidates, dict) or not isinstance(candidates.get("memories"), list):
        raise ValueError("主人旁听记忆提取结果无效")
    allowed_ids = {str(message["id"]) for message in messages}
    async with memory_worker.role_lock_for(thread.role_id):
        if memory_worker.role_deletion_blocked(thread.role_id):
            return
        def commit():
            for index, candidate in enumerate(candidates["memories"][:8]):
                if not isinstance(candidate, dict) or candidate.get("memoryType") not in {"fact", "preference", "profile", "procedure", "event"} or not isinstance(candidate.get("summary"), str) or not candidate["summary"].strip():
                    continue
                ids = candidate.get("messageIds")
                if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or item not in allowed_ids for item in ids):
                    continue
                item = memory_worker.service.store.add_or_reinforce(
                    thread.role_id, candidate["memoryType"], candidate["summary"].strip(),
                    MemorySourceRef(kind="listening", sessionKey=thread.session_key, messageIds=ids, stableSourceKey=f"{source_key}:{index}"),
                    extra={"memory_visibility": "owner_private"},
                )
                memory_worker.service.index_embedding(thread.role_id, item)
            if memory_worker.maintenance is not None:
                memory_worker.maintenance.sync_structured_memory(thread.role_id, memory_worker.service.store.list_all(thread.role_id))
        await finish_io(asyncio.to_thread(commit))


channel_memory_maintenance = ChannelMemoryMaintenance(
    lambda: multichannel_store, lambda: group_repository,
    _summarize_channel_memory, _ingest_owner_channel_memory,
    owner_statements=_ingest_owner_listening_memory,
    enabled=lambda: bool(memory_settings_store.get().enabled),
)


app.router.on_startup.append(_start_channel_workers)
app.router.on_shutdown.append(_close_channel_workers)


async def _handle_channel_message(thread, content: str, metadata: dict[str, object], owner: bool) -> None:
    """Run one isolated network thread through the shared role runtime."""

    if thread.role_id in _channel_roles_deleting:
        await _notify_channel_turn_not_started(thread, metadata)
        return

    role = store.get(thread.role_id)
    message_id = metadata.get("messageId")
    if role is None or not isinstance(message_id, str):
        return
    configuration = model_configuration_store.get(role.modelConfigurationId)
    if role.modelConfigurationId and configuration is None:
        return
    run_id = f"run-{uuid.uuid4().hex}"
    model_snapshot = {
        "channel": thread.channel,
        "accountId": thread.account_id,
        "threadId": thread.thread_id,
        "ownerContext": owner,
        "role": role.model_dump(mode="json", exclude={"modelConfig"}),
    }
    run = AgentRun(
        runId=run_id,
        roleId=thread.role_id,
        sessionKey=thread.session_key,
        status="created",
        modelConfigurationId=configuration.id if configuration else None,
        modelSnapshot=model_snapshot,
        startedAt=datetime.now(timezone.utc),
    )
    try:
        runtime_manager.sessions = session_store
        runtime_manager.role_locks = role_locks
        _, user_message, assistant_message = await runtime_manager.reserve_existing_user_message(run, message_id)
    except (ActiveRunError, KeyError, sqlite3.IntegrityError):
        return
    if not runtime_manager.attach_current_task(run_id):
        runtime_manager.cancel_unstarted_run(run_id)
        return
    owner = bool(owner and multichannel_store.identity_is_owner(
        role_id=thread.role_id,
        channel=thread.channel,
        scope=_channel_identity_scope(thread.channel),
        platform_user_id=str(metadata.get("senderId") or ""),
        account_id=thread.account_id,
    ))
    model_snapshot["ownerContext"] = owner
    placeholder_id: str | None = assistant_message.id
    final_message_id: str | None = None
    capabilities: CapabilityResolution | None = None
    final_content = ""
    streamed_content = ""
    stream_queue: asyncio.Queue[str | None] | None = None
    stream_task: asyncio.Task[object] | None = None

    async def persist_runtime_message(event: MessageEndEvent) -> Message | None:
        nonlocal placeholder_id, final_message_id
        if isinstance(event.message, AssistantMessage):
            sourced_message = replace(event.message, metadata={
                **event.message.metadata,
                "channel": thread.channel, "accountId": thread.account_id,
                "threadId": thread.thread_id, "direction": "outbound",
                "viaAccount": thread.metadata.get("viaAccount"),
            })
            if event.message.tool_calls:
                if placeholder_id is not None:
                    session_store.delete_message(placeholder_id)
                    placeholder_id = None
                final_message_id = None
                return session_store.append_agent_message(
                    thread.session_key,
                    sourced_message,
                    run_id=run_id,
                    status="completed" if event.message.stop_reason == "tool_use" else "failed",
                )[0]
            if placeholder_id is not None:
                message = session_store.update_message(
                    placeholder_id,
                    event.message.content,
                    "streaming",
                    metadata={
                        **event.message.metadata,
                        "channel": thread.channel,
                        "accountId": thread.account_id,
                        "threadId": thread.thread_id,
                        "direction": "outbound",
                        "viaAccount": thread.metadata.get("viaAccount"),
                    },
                )
            else:
                message = session_store.append_agent_message(
                    thread.session_key,
                    sourced_message,
                    run_id=run_id,
                    status="streaming",
                )[0]
            final_message_id = message.id
            return message
        if isinstance(event.message, ToolResultMessage):
            return session_store.append_agent_message(thread.session_key, event.message, run_id=run_id, status="completed")[0]
        return None

    try:
        owner_context_since = (
            multichannel_store.owner_chat_context_since(
                role_id=thread.role_id,
                channel=thread.channel,
                account_id=thread.account_id,
                chat_id=thread.chat_id,
            )
            if owner and thread.chat_type == "private"
            else None
        )
        runtime_messages = session_store.runtime_messages(
            thread.session_key,
            max_messages=MemoryMaintenance.RECENT_MESSAGE_LIMIT * 2,
            since=owner_context_since,
            through_message_id=message_id,
        )
        if runtime_messages and isinstance(runtime_messages[-1], UserMessage) and metadata.get("replyTo"):
            quoted_text = str(metadata.get("replyToContent") or "")
            speaker = str(metadata.get("replyToSenderName") or metadata.get("replyToSenderId") or "未知发送者")
            prefix = f"引用 {speaker}：\n{quoted_text}\n\n" if quoted_text else ""
            quoted_media = load_runtime_attachments({"attachments": metadata.get("quotedAttachments", [])})
            runtime_messages[-1] = replace(runtime_messages[-1], content=prefix + runtime_messages[-1].content, attachments=(*runtime_messages[-1].attachments, *quoted_media))
        if thread.chat_type == "group":
            heard = multichannel_store.listening.context(thread.role_id, thread.thread_id) if thread.channel == "qq" else ""
            frame = heard
            if frame and runtime_messages and isinstance(runtime_messages[-1], UserMessage):
                runtime_messages[-1] = replace(runtime_messages[-1], content=frame + "\n\n当前消息：\n" + runtime_messages[-1].content)
        available = _runtime_tool_candidates(
            role,
            is_channel_owner=owner,
            source_thread_id=thread.thread_id,
            source_account_id=thread.account_id,
            include_channel=True,
        )
        if thread.channel == "qq":
            available = (*available, ReadChannelAttachmentTool(
                thread.role_id, thread.session_key,
                [item for key in ("attachments", "quotedAttachments") for item in metadata.get(key, []) if isinstance(item, dict)],
            ))
        runtime_tools = _runtime_tools(role, candidates=available)
        capabilities = await _runtime_capabilities(
            role,
            session_key=thread.session_key,
            run_id=run_id,
            available_tools=available,
            prompt_text=content,
            external_only=not owner,
        )
        model_snapshot["capabilities"] = capabilities.snapshot.to_dict(include_prompt_sections=False)
        session_store.update_run(run_id, status="created", model_snapshot=model_snapshot)
        memory_context = _role_memory_context(thread.role_id) if owner and thread.chat_type == "private" else ""
        if owner and thread.chat_type == "private":
            authorized_history = multichannel_store.owner_context(thread.role_id, exclude_thread_id=thread.thread_id)
            if authorized_history:
                lines = [
                    f"[{title or '桌面会话'} / {'主人' if message_role == 'user' else role.name}] {text}"
                    for title, message_role, text, _created_at in authorized_history
                ]
                memory_context = "\n\n".join(part for part in (memory_context, "[配对主人授权的近期对话]\n" + "\n".join(lines)) if part)
        if thread.chat_type == "group" or not owner:
            external_context = group_repository.prompt_context(thread.role_id, thread.thread_id, str(metadata.get("senderId") or ""))
            memory_context = "\n\n".join(part for part in (memory_context, external_context) if part)
        provider = MeidoProvider(model_adapter, role, configuration, memory_context, capabilities.prompt_context())
        target = target_from_chat(
            account_id=thread.account_id,
            chat_id=thread.chat_id,
            kind=TargetKind.GROUP if thread.chat_type == "group" else TargetKind.PRIVATE,
            display_name=thread.title,
        )
        target_id = target.id
        async def stream_updates():
            assert stream_queue is not None
            while True:
                update = await stream_queue.get()
                if update is None:
                    return
                yield update

        async for event in runtime_manager.run(
            AgentRun(
                runId=run_id,
                roleId=thread.role_id,
                sessionKey=thread.session_key,
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
            tool_timeout=30.0,
            on_message_end=persist_runtime_message,
        ):
            if isinstance(event, MessageUpdateEvent) and isinstance(event.message, AssistantMessage) and event.message.content.strip():
                streamed_content = event.message.content
                adapter = channel_adapters.get(thread.account_id)
                if adapter is not None and adapter.capabilities.streaming and (
                    thread.channel != "telegram" or thread.chat_type == "private"
                ):
                    if stream_queue is None:
                        stream_queue = asyncio.Queue()
                        stream_task = asyncio.create_task(channel_host.account_send(
                            thread.role_id,
                            thread.account_id,
                            target_id,
                            "",
                            reply_to_message_id=str(metadata.get("platformMessageId")) if metadata.get("platformMessageId") else None,
                            stream_updates=stream_updates(),
                            actor="reply",
                            source_thread_id=thread.thread_id,
                            record_message=False,
                            source_session_key=thread.session_key,
                            source_run_id=run_id,
                            source_kind="reply",
                            mention_ids=(str(metadata["senderId"]),) if thread.chat_type == "group" and thread.channel == "qq" and metadata.get("senderId") else (),
                            message_thread_id=metadata.get("messageThreadId"),
                        ))
                    stream_queue.put_nowait(event.message.content)
            if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage) and not event.message.tool_calls:
                final_content = event.message.content
        delivery_metadata: dict[str, object] | None = None
        if stream_queue is not None and stream_task is not None:
            terminal_content = final_content if final_content.strip() else streamed_content
            if terminal_content.strip():
                stream_queue.put_nowait(terminal_content)
            stream_queue.put_nowait(None)
            try:
                result = await stream_task
                delivery_metadata = {
                    "deliveryStatus": result.status.value,
                    "deliveryAttemptId": result.attempt_id,
                    "platformMessageId": result.platform_message_id,
                    "deliveryError": result.error,
                }
            except Exception as error:
                logger.warning("渠道回复投递失败 role=%s thread=%s error=%s", thread.role_id, thread.thread_id, type(error).__name__)
                delivery_metadata = {"deliveryStatus": "failed", "deliveryError": type(error).__name__}
        elif final_content.strip():
            try:
                result = await channel_host.account_send(
                    thread.role_id,
                    thread.account_id,
                    target.id,
                    final_content,
                    reply_to_message_id=str(metadata.get("platformMessageId")) if metadata.get("platformMessageId") else None,
                    actor="reply",
                    source_thread_id=thread.thread_id,
                    record_message=False,
                    source_session_key=thread.session_key,
                    source_run_id=run_id,
                    source_kind="reply",
                    mention_ids=(str(metadata["senderId"]),) if thread.chat_type == "group" and thread.channel == "qq" and metadata.get("senderId") else (),
                    message_thread_id=metadata.get("messageThreadId"),
                )
                delivery_metadata = {
                    "deliveryStatus": result.status.value,
                    "deliveryAttemptId": result.attempt_id,
                    "platformMessageId": result.platform_message_id,
                    "deliveryError": result.error,
                }
            except Exception as error:
                logger.warning("渠道回复投递失败 role=%s thread=%s error=%s", thread.role_id, thread.thread_id, type(error).__name__)
                delivery_metadata = {"deliveryStatus": "failed", "deliveryError": type(error).__name__}
        if final_message_id is not None and delivery_metadata is not None:
            message = next((item for item in session_store.list_messages(thread.session_key) if item.id == final_message_id), None)
            if message is not None:
                updated = session_store.update_message(
                    final_message_id,
                    message.content,
                    message.status if delivery_metadata.get("deliveryStatus") == "sent" else "failed",
                    metadata={**message.metadata, **delivery_metadata},
                )
                multichannel_store.publish(thread.role_id, {
                    "type": "message",
                    "roleId": thread.role_id,
                    "thread": multichannel_store.get_thread(thread.role_id, thread.thread_id).public_dict(),
                    "message": public_message(updated).model_dump(mode="json"),
                })
                try:
                    if not memory_worker.role_deletion_blocked(thread.role_id):
                        async with memory_worker.role_lock_for(thread.role_id):
                            await channel_memory_maintenance.commit_turn(thread, owner, user_message, updated)
                except Exception as error:
                    logger.warning("渠道记忆维护失败 role=%s error=%s", thread.role_id, type(error).__name__)
    except Exception as error:
        logger.warning("渠道回合失败 role=%s thread=%s error=%s", thread.role_id, thread.thread_id, type(error).__name__)
    finally:
        try:
            if stream_queue is not None and stream_task is not None:
                if not stream_task.done():
                    terminal_content = final_content if final_content.strip() else streamed_content
                    if terminal_content.strip():
                        stream_queue.put_nowait(terminal_content)
                    stream_queue.put_nowait(None)
                cleanup = asyncio.gather(stream_task, return_exceptions=True)
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
        finally:
            try:
                if capabilities is not None:
                    await finish_io(capabilities.close())
            finally:
                pending = session_store.get_run(run_id)
                if pending is not None and pending.status in {"created", "running"}:
                    runtime_manager.abandon_unstarted_run(run_id, "渠道回合在 Runtime 启动前中断")
                terminal = next((item for item in reversed(session_store.list_messages(thread.session_key)) if item.runId == run_id and item.role == "assistant"), None)
                if terminal is not None:
                    multichannel_store.publish(thread.role_id, {
                        "type": "message", "roleId": thread.role_id,
                        "threadId": thread.thread_id,
                        "message": public_message(terminal).model_dump(mode="json"),
                    })


@app.post("/api/roles/{role_id}/messages")
async def send_role_message(role_id: str, data: SendMessageInput, request: Request) -> StreamingResponse:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    attachments: list[dict[str, object]] = []
    for attachment_id in data.attachments:
        item = resolve_attachment(store.workspace_path(role_id), attachment_id)
        if item is None:
            raise HTTPException(status_code=404, detail="附件不存在或已失效")
        attachments.append(item)
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
        available_runtime_tools = _runtime_tool_candidates(
            role,
            include_channel=_role_has_usable_channel_account(role_id),
            source_thread_id=f"desktop:{role_id}",
        )
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
        _, user_message, assistant_message = await runtime_manager.reserve_run(run, data.content, attachments=attachments)
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
                        metadata={
                            **event.message.metadata,
                            "runtimeStopReason": event.message.stop_reason,
                        },
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
            yield _event("user_message_accepted", runtime_event_payload({"message": public_message(user_message).model_dump(mode="json")}))
            yield _event("assistant_generation_started", runtime_event_payload({"messageId": assistant_message.id}))
            try:
                # Run memory preparation after the response stream starts so
                # the disconnect watcher can cancel even a slow recall query.
                context_messages = session_store.context_messages(session.session.sessionKey)
                try:
                    if memory_settings_store.get().enabled and len(context_messages) > context_limit:
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
                    )) if memory_settings_store.get().enabled else None
                    memory_context = memory_service.context_block([], _role_memory_context(role_id))
                    if query_result is not None and query_result.text_block:
                        memory_context = "\n".join(part for part in (memory_context, query_result.text_block) if part)
                    owner_history = multichannel_store.owner_context(
                        role_id,
                        exclude_thread_id=f"desktop:{role_id}",
                    )
                    if owner_history:
                        owner_lines = [
                            f"[{title or '主人授权会话'}] {text}"
                            for title, _message_role, text, _created_at in owner_history
                        ]
                        memory_context = "\n\n".join(
                            part for part in (memory_context, "[配对主人授权的近期对话]\n" + "\n".join(owner_lines)) if part
                        )
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
                try:
                    todo_context = todo_plugin.context_block(data.content)
                    if todo_context:
                        memory_context = "\n\n".join(part for part in (memory_context, todo_context) if part)
                except Exception as error:
                    todo_plugin.errors.append(f"{role_id}: todo context failed: {error}")
                runtime_messages = session_store.runtime_messages(
                    session.session.sessionKey,
                    max_messages=context_limit,
                )
                model_snapshot["context"] = {
                    "messages": [_runtime_message_snapshot(message) for message in runtime_messages],
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
                    # The outer timeout must not shorten Browser Use's own
                    # per-action policy when a role also enables a shorter
                    # shell timeout; each ToolDefinition still applies its
                    # own limit inside the runtime.
                    tool_timeout=max(
                        shell_config.timeoutSeconds if runtime_tools else 30.0,
                        browser_use_sessions.timeout_seconds if role.agentConfig.browserUse.enabled else 0.0,
                    ),
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
                        safe_arguments = sanitize_browser_arguments(
                            runtime_event.tool_name,
                            runtime_event.arguments,
                        )
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
                            "arguments": safe_arguments,
                        }, runtime_event.sequence))
                    elif isinstance(runtime_event, ToolExecutionUpdateEvent):
                        safe_result, _ = sanitize_browser_text(runtime_event.result.content)
                        yield _event("tool_execution_update", runtime_event_payload({
                            "toolCallId": runtime_event.tool_call_id,
                            "toolName": runtime_event.tool_name,
                            "result": safe_result,
                            "isError": runtime_event.result.is_error,
                        }, runtime_event.sequence))
                    elif isinstance(runtime_event, ToolExecutionEndEvent):
                        safe_result, _ = sanitize_browser_text(runtime_event.result.content)
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
                            "result": safe_result,
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
                        "message": public_message(message).model_dump(mode="json"),
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
                yield _event("assistant_completed", runtime_event_payload({"message": public_message(message).model_dump(mode="json")}))
            except Exception as error:
                runtime_manager.abandon_unstarted_run(run_id, str(error))
                message = next(
                    item
                    for item in reversed(session_store.list_messages(session.session.sessionKey))
                    if item.runId == run_id and item.role == "assistant" and item.messageType == "text"
                )
                yield _event("assistant_failed", runtime_event_payload({
                    "message": public_message(message).model_dump(mode="json"),
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
            try:
                # Browser sessions are scoped to this run; the profile remains
                # on disk while the owned MCP/Chrome generation is reaped.
                await browser_use_sessions.close_run(role_id, run_id)
            except Exception as error:
                memory_worker.errors.append(f"{role_id}: browser run cleanup failed: {type(error).__name__}")
            if disconnect_watcher is not None:
                disconnect_watcher.cancel()
                await asyncio.gather(disconnect_watcher, return_exceptions=True)
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(runtime_manager.abandon_unstarted_run, run_id),
    )
