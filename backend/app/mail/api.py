from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .service import MailService

router = APIRouter(prefix="/api/mail", tags=["mail"])
mail_service: MailService | None = None


def set_mail_service(service: MailService) -> None:
    global mail_service
    mail_service = service


def _service() -> MailService:
    if mail_service is None:
        raise HTTPException(status_code=503, detail="邮件服务未初始化")
    return mail_service


class MailConnectionInput(BaseModel):
    host: str = "imap.qq.com"
    port: int = Field(default=993, ge=1, le=65535)
    username: str
    password: str = Field(min_length=1, max_length=512)
    useSsl: bool = True

    @field_validator("host", "username", "password")
    @classmethod
    def normalize(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("邮箱连接信息不能为空")
        return value


class MailTaskInput(BaseModel):
    requestId: str = Field(min_length=1, max_length=100)


class MailSyncInput(BaseModel):
    requestId: str = Field(min_length=1, max_length=100)
    process: bool = True


class ProcessPendingInput(BaseModel):
    includeUncertain: bool = True
    requestId: str = Field(min_length=1, max_length=100)


class ReminderRoleInput(BaseModel):
    roleId: str | None = None


@router.get("/status")
def get_status() -> dict[str, object]:
    return _service().public_status()


@router.put("/connection")
def save_candidate(data: MailConnectionInput) -> dict[str, object]:
    try:
        return _service().configure(host=data.host, port=data.port, username=data.username, password=data.password, use_ssl=data.useSsl)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.post("/connection/test")
def test_connection(data: dict[str, object] | None = None) -> dict[str, object]:
    try:
        return _service().test_connection(write_test=bool((data or {}).get("writeTest", False)))
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/connection/confirm-test-send")
def confirm_test_send() -> dict[str, object]:
    try:
        return _service().confirm_test_draft_sent()
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/enable")
def enable() -> dict[str, object]:
    try:
        return _service().enable()
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/pause")
def pause() -> dict[str, object]:
    return _service().pause()


@router.post("/sync")
def sync(data: MailSyncInput) -> dict[str, object]:
    try:
        return {"job": _service().enqueue_job("sync", request_id=data.requestId, payload={"process": data.process})}
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/process-pending")
def process_pending(data: ProcessPendingInput) -> dict[str, object]:
    try:
        return {"job": _service().enqueue_job("process_pending", request_id=data.requestId, payload={"includeUncertain": data.includeUncertain})}
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/threads/{thread_id}/process")
def process_thread(thread_id: str, data: MailTaskInput) -> dict[str, object]:
    try:
        service = _service()
        if not service.store.thread_items(thread_id, account=service.store.connection().username):
            raise HTTPException(status_code=404, detail="邮件线程不存在")
        return {"job": service.enqueue_job("process_thread", target_id=thread_id, request_id=data.requestId)}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, object]:
    job = _service().job_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="邮件任务不存在")
    return {"job": job}


@router.get("/items")
def list_items(status: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, object]:
    service = _service()
    return {"items": service.list_items_public(status=status, limit=limit, offset=offset)}


@router.get("/items/{mail_id}")
def get_item(mail_id: str) -> dict[str, object]:
    try:
        return _service().details_public(mail_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="邮件不存在") from error


async def _run_task(mail_id: str, request_id: str, *, kind: str) -> dict[str, object]:
    service = _service()
    try:
        if service.store.get_item(mail_id, account=service.store.connection().username) is None:
            raise HTTPException(status_code=404, detail="邮件不存在")
        return {"job": service.enqueue_job(kind, target_id=mail_id, request_id=request_id, payload={"requestId": request_id})}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/items/{mail_id}/process")
async def process_item(mail_id: str, data: MailTaskInput) -> dict[str, object]:
    return await _run_task(mail_id, data.requestId, kind="process")


@router.post("/items/{mail_id}/retry")
async def retry_item(mail_id: str, data: MailTaskInput) -> dict[str, object]:
    return await _run_task(mail_id, data.requestId, kind="retry")


@router.post("/items/{mail_id}/regenerate")
async def regenerate_item(mail_id: str, data: MailTaskInput) -> dict[str, object]:
    return await _run_task(mail_id, data.requestId, kind="regenerate")


@router.get("/reminder-role")
def get_reminder_role() -> dict[str, object]:
    return {"roleId": _service().store.setting("reminder_role_id") or None}


@router.put("/reminder-role")
async def set_reminder_role(data: ReminderRoleInput) -> dict[str, object]:
    try:
        result = _service().configure_reminder_role(data.roleId)
        await _service().deliver_reminder_batches()
        return result
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/reminder-batches")
def list_reminder_batches(limit: int = 20) -> dict[str, object]:
    service = _service()
    account = service.store.connection().username
    return {"batches": service.store.reminder_batches(limit, account=account)}


@router.get("/audit")
def list_audit(limit: int = 100) -> dict[str, object]:
    return {"events": _service().store.list_audits(limit)}
