"""Owner-only local configuration; preview never connects to a source."""
import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import AutonomyConfig
from .source_authoring import preview_artifact


class ConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expectedRevision: int = Field(ge=0)
    config: AutonomyConfig


class SourceVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requestNonce: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    sourceId: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    expectedRevision: int = Field(ge=1)
    manifestPath: str = Field(min_length=3, max_length=2048)
    manifestSha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    trustLocalCode: bool

    @field_validator("trustLocalCode")
    @classmethod
    def require_trust(cls, value):
        if value is not True:
            raise ValueError("explicit local code trust is required")
        return value


class ArtifactPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    manifestPath: str = Field(min_length=3, max_length=240)
    serverId: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,23}$")


class DriftPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    skillId: str = Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,63}$')


class DriftAdoptionRequest(DriftPreviewRequest):
    previewId: str = Field(pattern=r'^[a-f0-9]{32}$')
    bundleSha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    authoringScopeId: str = Field(min_length=1, max_length=64)
    expectedScopeRevision: int = Field(ge=1)
    expectedRevision: int = Field(ge=1)


def autonomy_router(application, *, workspace_getter=None):
    router = APIRouter(prefix="/api/roles/{role_id}/autonomy")

    async def drift_request(request, model):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, '长期技能请求超出大小限制')
        try:
            return model.model_validate(json.loads(body))
        except (ValueError, TypeError):
            raise HTTPException(422, '长期技能标识、精确产物或预览版本无效') from None

    @router.post('/drift-artifact-preview')
    async def drift_preview(role_id: str, request: Request):
        data = await drift_request(request, DriftPreviewRequest)
        try:
            owner = application()
            return await owner._owned(owner.drift_skills.preview, role_id, data.skillId)
        except KeyError:
            raise HTTPException(404, '角色不存在') from None
        except (ValueError, OSError):
            raise HTTPException(422, '长期技能文件、链接或声明无效') from None
        except sqlite3.Error:
            raise HTTPException(503, '长期技能预览暂不可保存') from None

    @router.post('/drift-skill-adoptions')
    async def drift_adopt(role_id: str, request: Request):
        data = await drift_request(request, DriftAdoptionRequest)
        try:
            owner = application()
            return await owner._owned(owner.drift_skills.adopt, role_id, data)
        except KeyError:
            raise HTTPException(404, '角色不存在') from None
        except (ValueError, OSError):
            raise HTTPException(409, '产物、目录身份或作者范围已变化，请重新预览并明确采用') from None
        except sqlite3.Error:
            raise HTTPException(503, '长期技能采用暂不可保存') from None

    @router.get('/drift-skills')
    async def drift_skills(role_id: str):
        try:
            owner = application()
            return {'skills': await owner._owned(owner.drift_skills.list, role_id)}
        except KeyError:
            raise HTTPException(404, '角色不存在') from None
        except sqlite3.Error:
            raise HTTPException(503, '长期技能状态暂不可读，请重试') from None

    @router.get('/drift-grant-options')
    async def drift_grant_options(role_id: str):
        from ..mcp_autonomy import declaration_hash
        from .bindings import shape, local_tool_names
        try:
            owner = application()
            owner._require_role(role_id)
            role = shape(owner.role_getter(role_id), role=True)
            enabled = local_tool_names(role)
            servers = []
            for server in owner.mcp_host.store.all() if owner.mcp_host is not None else ():
                if role_id not in server.role_ids:
                    continue
                status, catalog = owner.mcp_host.observed_catalog(role_id, server.server_id)
                servers.append({'serverId': server.server_id, 'name': server.name, 'configRevision': server.revision,
                    'grantRevision': server.grant_id, 'risk': server.risk, 'status': status,
                    'operations': [{'name': item['name'], 'definitionHash': declaration_hash(item),
                        'description': item.get('description', item['name'])} for item in catalog]})
            names = ('read_file', 'write_file', 'edit_file', 'list_dir', 'recall_memory', 'web_fetch', 'web_search',
                'search_messages', 'fetch_messages', 'shell', 'message_push')
            from .drift_shell import presets, available
            from .drift_resources import RESOURCES
            return {'tools': [{'name': name, 'available': name in enabled} for name in names],
                'resources': [{'id': identifier, 'name': name, 'available': True} for identifier, name in RESOURCES.items()],
                'shellRules': [{'id': preset.identifier, 'description': preset.description, 'available': available(preset, role),
                    **({} if available(preset, role) else {'reason': '需要当前角色Shell开关及精确可执行程序许可'})}
                    for preset in presets().values()], 'mcpServers': servers}
        except KeyError:
            raise HTTPException(404, '角色不存在') from None
        except sqlite3.Error:
            raise HTTPException(503, 'Drift许可目录暂不可读，请重试') from None

    @router.post('/source-artifact-preview')
    async def preview(role_id: str, request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, '来源预览请求超出大小限制')
        try:
            data = ArtifactPreviewRequest.model_validate(json.loads(body))
            app = application()
            app._require_role(role_id)
            if workspace_getter is None:
                raise ValueError('host workspace is unavailable')
            return preview_artifact(workspace_getter(role_id), data.manifestPath, role_id=role_id,
                mcp_store=app.mcp_host.store if app.mcp_host is not None else None, server_id=data.serverId)
        except KeyError:
            raise HTTPException(404, '角色或产物声明不存在') from None
        except PermissionError:
            raise HTTPException(403, '产物不在当前角色目录或所选服务许可内') from None
        except (ValueError, OSError, TypeError, RecursionError):
            raise HTTPException(422, '产物草稿无效；检查相对清单路径、声明文件、链接、大小与已保存服务') from None

    @router.get("")
    async def query(role_id: str):
        try:
            await application().source_verifications.recover()
            return application().query(role_id)
        except KeyError:
            raise HTTPException(404, "角色不存在") from None
        except sqlite3.Error:
            raise HTTPException(503, "自主状态暂不可读，请重试") from None

    @router.get("/source-verifications")
    async def list_verifications(role_id: str):
        try:
            owner = application().source_verifications
            await owner.recover()
            return {"attempts": owner.list(role_id)}
        except KeyError:
            raise HTTPException(404, "角色不存在") from None
        except (sqlite3.Error, RuntimeError):
            raise HTTPException(503, "来源验证状态暂不可读，请重试") from None

    @router.post("/source-verifications", status_code=202)
    async def verify_source(role_id: str, request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16 * 1024:
                raise HTTPException(413, "来源验证请求超出大小限制")
        try:
            data = SourceVerificationRequest.model_validate(json.loads(body))
        except ValueError:
            raise HTTPException(422, "须明确信任本地产物，并填写已保存来源、验证清单及精确 SHA256") from None
        try:
            return await application().source_verifications.submit_owner(role_id,
                request_nonce=data.requestNonce, source_id=data.sourceId, expected_revision=data.expectedRevision,
                manifest_path=Path(data.manifestPath), expected_manifest_sha256=data.manifestSha256)
        except KeyError:
            raise HTTPException(404, "角色或已保存来源不存在") from None
        except PermissionError:
            raise HTTPException(403, "来源许可已撤销或产物不符合明确信任范围") from None
        except ValueError:
            raise HTTPException(409, "配置或验证请求已变化，请刷新状态后核对") from None
        except (sqlite3.Error, RuntimeError):
            raise HTTPException(503, "验证准入或资源收尾暂不可确认，请用原请求重试") from None

    @router.post("/source-verifications/{attempt_id}/cancel", status_code=202)
    async def cancel_verification(role_id: str, attempt_id: str):
        try:
            return await application().source_verifications.cancel(role_id, attempt_id)
        except KeyError:
            raise HTTPException(404, "角色或来源验证不存在") from None
        except (sqlite3.Error, RuntimeError, PermissionError):
            raise HTTPException(503, "取消或资源收尾暂不可确认，请刷新状态") from None

    @router.put("")
    async def configure(role_id: str, request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 128 * 1024:
                raise HTTPException(413, "自主配置超出大小限制")
        try:
            data = ConfigurationUpdate.model_validate(json.loads(body))
        except ValueError:
            raise HTTPException(422, "自主配置无效，请检查模型、来源、目标和有限操作许可") from None
        try:
            return application().configure(role_id, data.config, expected_revision=data.expectedRevision)
        except KeyError:
            raise HTTPException(404, "角色不存在") from None
        except ValueError as error:
            if str(error) == "autonomy configuration revision changed":
                raise HTTPException(409, "配置已变化，请刷新后重试") from None
            raise HTTPException(422, "模型或来源许可已失效，请重新选择") from None
        except (sqlite3.Error, RuntimeError):
            raise HTTPException(503, "自主配置暂不可写，请重试") from None

    @router.post("/stop", status_code=202)
    async def stop(role_id: str):
        try:
            return application().stop(role_id)
        except KeyError:
            raise HTTPException(404, "角色不存在") from None
        except sqlite3.Error:
            raise HTTPException(503, "停止请求暂不可记录，请重试") from None

    return router
