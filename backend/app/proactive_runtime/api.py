"""Owner-only local configuration; preview never connects to a source."""
import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import AutonomyConfig


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


def autonomy_router(application):
    router = APIRouter(prefix="/api/roles/{role_id}/autonomy")

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
