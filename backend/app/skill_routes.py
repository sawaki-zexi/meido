"""Owner UI endpoints. Model tools intentionally cannot publish shared skills."""
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field


class SkillResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=360000)
    encoding: str


class SkillSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=256 * 1024)
    ref: str | None = None
    expectedHash: str | None = None
    resources: dict[str, str | SkillResourceInput] = Field(default_factory=dict, max_length=50)


class SkillSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = Field(default=None, strict=True)
    ref: str | None = None
    active: bool | None = Field(default=None, strict=True)
    policy: str | None = None
    expectedRevision: int = Field(ge=0)


class SkillSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str
    expectedHash: str


def skill_router(library_for_role):
    router = APIRouter(prefix="/api/roles/{role_id}/skills")

    def run(operation):
        try:
            return operation()
        except KeyError:
            raise HTTPException(404, "角色不存在") from None
        except ValueError as error:
            raise HTTPException(409 if "冲突" in str(error) else 422, str(error)) from None
        except OSError:
            raise HTTPException(500, "技能保存或读取失败，请重试") from None

    @router.get("")
    def listing(role_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return run(lambda: library_for_role(role_id).describe())

    @router.get("/document")
    def document(role_id: str, ref: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return run(lambda: library_for_role(role_id).document(ref))

    @router.post("")
    def save(role_id: str, data: SkillSave):
        return run(lambda: library_for_role(role_id).save(data.content, ref=data.ref,
            expected_hash=data.expectedHash, resources={name: value.model_dump() if isinstance(value, BaseModel) else value
                for name, value in data.resources.items()}))

    @router.put("/settings")
    def settings(role_id: str, data: SkillSettings):
        return run(lambda: library_for_role(role_id).update_settings(enabled=data.enabled, ref=data.ref,
            active=data.active, policy=data.policy, expected_revision=data.expectedRevision))

    @router.post("/remove")
    def remove(role_id: str, data: SkillSelection):
        def operation():
            library_for_role(role_id).remove(data.ref, data.expectedHash)
            return {"removed": True}
        return run(operation)

    @router.post("/publish")
    def publish(role_id: str, data: SkillSelection):
        return run(lambda: library_for_role(role_id).publish(data.ref, data.expectedHash))

    return router
