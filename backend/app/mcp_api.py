"""Local owner configuration UI API; model tools never accept launch parameters."""
from __future__ import annotations

from collections.abc import Callable
import json

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

from .mcp_config import McpCredentialCleanupError, McpServerInput, MCP_MANAGEMENT_NAMES
from .mcp_runtime import McpHost
from .models import RoleUpdateInput
from .role_store import RoleStore


def mcp_router(host: Callable[[], McpHost], roles: Callable[[], RoleStore]) -> APIRouter:
    router = APIRouter(prefix="/api/mcp")

    @router.get("/servers")
    async def list_servers() -> dict[str, object]:
        return {"servers": host().public()}

    async def save(request: Request, server_id: str | None) -> dict[str, object]:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 128 * 1024:
                raise HTTPException(413, "MCP configuration exceeds budget")
        try:
            data = McpServerInput.model_validate(json.loads(body))
        except (ValueError, ValidationError):
            # Pydantic's default input echo could contain environment secrets.
            raise HTTPException(422, "MCP 配置无效；检查固定命令、绝对路径、环境变量和预算") from None
        current = host()
        old = current.store.get(data.serverId)
        if server_id is None and old is not None:
            raise HTTPException(409, "MCP 服务 ID 已存在")
        if server_id is not None and (server_id != data.serverId or old is None):
            raise HTTPException(404, "MCP 服务不存在或 ID 不匹配")
        grants = []
        for role_id in data.roleIds:
            role = roles().get(role_id)
            if role is None:
                raise HTTPException(422, "授权角色不存在")
            grants.append(role)
        try:
            config = current.store.save(data)
        except ValueError:
            raise HTTPException(422, "新环境变量必须提供值；留空只保留已有值") from None
        except McpCredentialCleanupError:
            raise HTTPException(503, "MCP 凭据清理未完成，请重试保存或删除；当前服务状态以目录为准") from None
        except Exception:
            raise HTTPException(503, "MCP 配置或系统凭据库暂不可写") from None
        finally:
            await current.revoke(data.serverId)
        # This checkbox is an explicit owner action. Preserve unrelated role
        # policy and give the owner a usable discovery path after granting.
        if data.enableDiscovery:
            for role in grants:
                role = roles().get(role.id)
                if role is None:
                    continue
                agent = role.agentConfig.model_copy(update={"enabledTools": sorted(set(role.agentConfig.enabledTools) | MCP_MANAGEMENT_NAMES | {"tool_search"})})
                roles().update(role.id, RoleUpdateInput(name=role.name, profile=role.profile, agentConfig=agent))
        return {"server": next(item for item in current.public() if item["serverId"] == config.server_id)}

    @router.post("/servers", status_code=201)
    async def create(request: Request) -> dict[str, object]:
        return await save(request, None)

    @router.put("/servers/{server_id}")
    async def update(server_id: str, request: Request) -> dict[str, object]:
        return await save(request, server_id)

    @router.delete("/servers/{server_id}", status_code=204)
    async def delete(server_id: str) -> Response:
        current = host()
        try:
            current.store.prepare_delete(server_id)
            await current.revoke(server_id)
            current.store.delete(server_id)
        except McpCredentialCleanupError:
            raise HTTPException(503, "MCP 服务已停用并断连，凭据清理未完成；请重试删除") from None
        except Exception:
            raise HTTPException(503, "MCP 配置或系统凭据库暂不可写，请重试删除") from None
        return Response(status_code=204)

    return router
