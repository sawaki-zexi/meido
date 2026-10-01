import os
import asyncio
import json
import inspect
from time import monotonic
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx

from .model_adapter import ModelAdapter, OpenAICompatibleAdapter
from .model_config import ModelConfigurationStore, PROVIDER_PRESETS, public_configuration, validate_model_configuration
from .memory_service import MemoryService, MemoryWorker
from .embeddings import OpenAICompatibleEmbeddingAdapter
from .memory_maintenance import MemoryMaintenance
from .memory_optimizer import MemoryOptimizer, MemoryOptimizerWorker, MemoryOptimizationResult, MemoryRecord
from .memory_store import MemoryStore
from .models import MemoryList, MemoryItem, MemorySourceRef, RememberMemoryInput, UpdateMemoryInput, ModelConfiguration, ModelConfigurationInput, ProviderPresetList, RoleInput, RoleList, RoleResponse, RoleUpdateInput, SendMessageInput, SessionResponse
from .models import RoleModelConfigurationInput
from .role_store import RoleStore
from .storage import initialize_databases
from .session_manager import SessionManager
from .session_store import SessionStore

roles_root = Path(os.getenv("MEIDO_ROLES_DIR", "roles"))
data_root = Path(os.getenv("MEIDO_DATA_DIR", ".data"))
initialize_databases(data_root)
store = RoleStore(roles_root)
session_store = SessionStore(data_root / "sessions.db")
session_manager = SessionManager(store, session_store)
memory_store = MemoryStore(data_root / "memory2.db")
model_adapter: ModelAdapter = OpenAICompatibleAdapter()
model_configuration_store = ModelConfigurationStore(data_root / "model-config.json")


def _embedding_configuration(role_id: str):
    role = store.get(role_id)
    return model_configuration_store.get(role.modelConfigurationId) if role and role.modelConfigurationId else model_configuration_store.get()


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
    return MemoryOptimizationResult(MemoryOptimizer._merge_records(existing, records), self_understanding[:2000])


def _persist_consolidated_memories(role_id: str, records: list[MemoryRecord]) -> None:
    """Mirror optimizer output into the role-scoped structured memory store."""
    for record in records:
        for source_key in dict.fromkeys(record.sources):
            memory_store.add_or_reinforce(
                role_id,
                record.memory_type,
                record.summary,
                MemorySourceRef(
                    kind="consolidation",
                    sessionKey=f"role:{role_id}",
                    stableSourceKey=source_key,
                ),
            )


embedding_provider = OpenAICompatibleEmbeddingAdapter(_embedding_configuration)
memory_service = MemoryService(memory_store, embedding_provider)
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
        _persist_consolidated_memories,
    )
)
memory_worker = MemoryWorker(
    memory_service,
    MemoryMaintenance(roles_root, session_store),
    memory_optimizer_worker,
)
connection_test_adapter = OpenAICompatibleAdapter(timeout=20)
role_locks: dict[str, asyncio.Lock] = {}


def _role_memory_context(role_id: str) -> str:
    roles_root_path = roles_root.resolve()
    memory_dir = (roles_root_path / role_id / "memory").resolve()
    if memory_dir.parent != roles_root_path / role_id:
        return ""
    sections = []
    for filename in ("SELF.md", "MEMORY.md", "RECENT_CONTEXT.md"):
        path = memory_dir / filename
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content:
                sections.append(f"[{filename}]\n{content}")
    return "\n\n".join(sections)

app = FastAPI(title="Meido API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


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

    lock = role_locks.setdefault(role_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(status_code=409, detail="该角色正在生成回复，暂时无法删除")

    await lock.acquire()
    deletion_started = False
    session_snapshot = None
    memory_snapshot = None
    session_snapshot_taken = False
    memory_snapshot_taken = False
    role_delete_attempted = False
    deleted = None
    staged_path = None
    deletion_phase = "preflight"
    try:
        await memory_worker.begin_role_deletion(role_id)
        deletion_started = True
        session_snapshot = session_store.snapshot_role_session(role_id)
        session_snapshot_taken = True
        memory_snapshot = memory_store.snapshot_role(role_id)
        memory_snapshot_taken = True
        deletion_phase = "role"
        role_delete_attempted = True
        deleted = store.delete(role_id)
        staged_path = store.stage_role_files_for_deletion(role_id)
        deletion_phase = "session"
        session_store.delete_role_session(role_id)
        deletion_phase = "memory"
        memory_store.delete_role(role_id)
        store.purge_staged_role_files(staged_path)
    except Exception as error:
        rollback_errors: list[Exception] = []
        if session_snapshot_taken:
            try:
                session_store.restore_role_session(role_id, session_snapshot)
            except Exception as restore_error:
                rollback_errors.append(restore_error)
        if memory_snapshot_taken:
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
                    store.remove_role_files(role_id)
                except Exception as restore_error:
                    rollback_errors.append(restore_error)
                try:
                    store.restore_staged_role_files(role_id, staged_path)
                except Exception as restore_error:
                    rollback_errors.append(restore_error)
        if isinstance(error, KeyError) and role_delete_attempted and deleted is None and not rollback_errors:
            raise HTTPException(status_code=404, detail="角色不存在") from error
        if rollback_errors:
            raise HTTPException(status_code=500, detail="删除失败，角色恢复也未能完成") from error
        detail = "删除失败，角色和聊天记录已保留" if deletion_phase == "session" else "删除失败，角色和记忆已保留"
        raise HTTPException(status_code=500, detail=detail) from error
    finally:
        if deletion_started:
            memory_worker.end_role_deletion(role_id)
        lock.release()


@app.get("/api/roles/{role_id}/session", response_model=SessionResponse)
def get_role_session(role_id: str) -> SessionResponse:
    try:
        return session_manager.open_role_session(role_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error


@app.get("/api/roles/{role_id}/memories", response_model=MemoryList)
def list_role_memories(role_id: str, q: str = "") -> MemoryList:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    memories = memory_service.recall(role_id, q) if q.strip() else memory_store.list_active(role_id)
    return MemoryList(memories=memories)


@app.post("/api/roles/{role_id}/memories", response_model=MemoryItem, status_code=201)
def remember_role_memory(role_id: str, data: RememberMemoryInput) -> MemoryItem:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return memory_service.remember(
            role_id,
            data.summary,
            data.memoryType,
            happened_at=data.happenedAt,
            stable_source_key=f"manual:{role_id}:{data.memoryType}:{data.summary.casefold()}",
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.put("/api/roles/{role_id}/memories/{memory_id}", response_model=MemoryItem)
def update_role_memory(role_id: str, memory_id: str, data: UpdateMemoryInput) -> MemoryItem:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return memory_store.update(role_id, memory_id, data.summary, data.memoryType, data.happenedAt)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="记忆不存在") from error


@app.post("/api/roles/{role_id}/memories/{memory_id}/forget", response_model=MemoryItem)
def forget_role_memory(role_id: str, memory_id: str) -> MemoryItem:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        return memory_store.set_status(role_id, memory_id, "forgotten")
    except KeyError as error:
        raise HTTPException(status_code=404, detail="记忆不存在") from error


@app.delete("/api/roles/{role_id}/memories/{memory_id}", status_code=204)
def delete_role_memory(role_id: str, memory_id: str) -> None:
    if store.get(role_id) is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        memory_store.remove(role_id, memory_id)
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


@app.post("/api/roles/{role_id}/messages")
async def send_role_message(role_id: str, data: SendMessageInput) -> StreamingResponse:
    try:
        role, session = session_manager.role_and_history(role_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
    configuration = model_configuration_store.get(role.modelConfigurationId)
    if role.modelConfigurationId and configuration is None:
        raise HTTPException(status_code=409, detail="该角色绑定的模型连接已不存在，请重新选择模型")
    lock = role_locks.setdefault(role_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(status_code=409, detail="该角色正在生成回复，请稍后再试")
    await lock.acquire()

    if store.get(role_id) is None:
        lock.release()
        raise HTTPException(status_code=404, detail="角色不存在")

    try:
        user_message = session_store.append_message(session.session.sessionKey, "user", data.content)
        assistant_message = session_store.append_message(session.session.sessionKey, "assistant", "", "streaming")
    except Exception:
        lock.release()
        raise
    history = [*session_store.context_messages(session.session.sessionKey)[:-1], user_message]
    try:
        memories = await memory_service.recall_async(role_id, data.content)
        memory_context = memory_service.context_block(memories, _role_memory_context(role_id))
    except Exception as error:
        memory_worker.errors.append(f"{role_id}: recall failed: {error}")
        memory_context = ""
    async def stream():
        content = ""
        try:
            yield _event("user_message_accepted", {"message": user_message.model_dump(mode="json")})
            yield _event("assistant_generation_started", {"messageId": assistant_message.id})
            try:
                deltas = _stream_model_reply(role, history, configuration, memory_context)
                async for delta in deltas:
                    content += delta
                    session_store.update_message(assistant_message.id, content, "streaming")
                    yield _event("assistant_delta", {"messageId": assistant_message.id, "delta": delta})
                message = session_store.update_message(assistant_message.id, content, "completed")
                memory_worker.submit(role_id, session.session.sessionKey, user_message, message)
                yield _event("assistant_completed", {"message": message.model_dump(mode="json")})
            except Exception as error:
                message = session_store.update_message(assistant_message.id, content, "failed")
                yield _event("assistant_failed", {
                    "message": message.model_dump(mode="json"),
                    "error": _safe_model_error(error, configuration.apiKey if configuration else ""),
                })
        except asyncio.CancelledError:
            session_store.update_message(assistant_message.id, content, "failed")
            raise
        finally:
            lock.release()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
