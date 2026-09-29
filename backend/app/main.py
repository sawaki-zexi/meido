import os
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .model_adapter import ModelAdapter, OpenAICompatibleAdapter
from .model_config import ModelConfigurationStore, PROVIDER_PRESETS, public_configuration, validate_model_configuration
from .models import ModelConfiguration, ModelConfigurationInput, ProviderPresetList, RoleInput, RoleList, RoleResponse, RoleUpdateInput, SendMessageInput, SessionResponse
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
model_adapter: ModelAdapter = OpenAICompatibleAdapter()
model_configuration_store = ModelConfigurationStore(data_root / "model-config.json")
connection_test_adapter = OpenAICompatibleAdapter(timeout=20)
role_locks: dict[str, asyncio.Lock] = {}

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


@app.post("/api/model/configuration/test")
async def test_model_configuration(data: ModelConfigurationInput) -> dict[str, object]:
    try:
        candidate = _configuration_with_saved_key(data)
        validate_model_configuration(candidate)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    configuration = ModelConfiguration.model_validate(candidate.model_dump())
    try:
        async for _ in connection_test_adapter.stream_messages(
            [{"role": "user", "content": "ping"}], configuration, max_tokens=8
        ):
            pass
    except Exception as error:
        return {"ok": False, "message": _safe_model_error(error, configuration.apiKey)}
    return {"ok": True, "message": "连接成功"}


@app.get("/api/roles", response_model=RoleList)
def list_roles() -> RoleList:
    return RoleList(roles=store.list())


@app.post("/api/roles", response_model=RoleResponse, status_code=201)
def create_role(data: RoleInput) -> RoleResponse:
    return RoleResponse(role=store.create(data))


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
        raise HTTPException(status_code=500, detail="角色保存失败，请稍后重试") from error


@app.delete("/api/roles/{role_id}", status_code=204)
async def delete_role(role_id: str) -> None:
    role = store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")

    lock = role_locks.setdefault(role_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(status_code=409, detail="该角色正在生成回复，暂时无法删除")

    await lock.acquire()
    try:
        deleted = store.delete(role_id)
        try:
            session_store.delete_role_session(role_id)
        except Exception as error:
            try:
                store.restore_deleted(deleted)
            except OSError as restore_error:
                raise HTTPException(status_code=500, detail="删除失败，角色恢复也未能完成") from restore_error
            raise HTTPException(status_code=500, detail="删除失败，角色和聊天记录已保留") from error
        store.remove_role_files(role_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
    except OSError as error:
        raise HTTPException(status_code=500, detail="删除失败，角色和聊天记录已保留") from error
    finally:
        lock.release()


@app.get("/api/roles/{role_id}/session", response_model=SessionResponse)
def get_role_session(role_id: str) -> SessionResponse:
    try:
        return session_manager.open_role_session(role_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error


def _event(event_type: str, payload: dict[str, object]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/roles/{role_id}/messages")
async def send_role_message(role_id: str, data: SendMessageInput) -> StreamingResponse:
    try:
        role, session = session_manager.role_and_history(role_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
    lock = role_locks.setdefault(role_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(status_code=409, detail="该角色正在生成回复，请稍后再试")
    await lock.acquire()

    try:
        user_message = session_store.append_message(session.session.sessionKey, "user", data.content)
        assistant_message = session_store.append_message(session.session.sessionKey, "assistant", "", "streaming")
    except Exception:
        lock.release()
        raise
    history = [*session_store.context_messages(session.session.sessionKey)[:-1], user_message]
    configuration = model_configuration_store.get()

    async def stream():
        content = ""
        try:
            yield _event("user_message_accepted", {"message": user_message.model_dump(mode="json")})
            yield _event("assistant_generation_started", {"messageId": assistant_message.id})
            try:
                async for delta in model_adapter.stream_reply(role, history, configuration):
                    content += delta
                    session_store.update_message(assistant_message.id, content, "streaming")
                    yield _event("assistant_delta", {"messageId": assistant_message.id, "delta": delta})
                message = session_store.update_message(assistant_message.id, content, "completed")
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
