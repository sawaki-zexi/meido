from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from typing import Literal

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
    confirmUncertainNewVersion: bool = False


class MailAnswerInput(MailTaskInput):
    questionId: str = Field(min_length=1, max_length=100)
    answer: str = Field(min_length=1, max_length=4000)


@router.post("/items/{mail_id}/answer")
def answer_question(mail_id: str, data: MailAnswerInput) -> dict[str, object]:
    try:
        return {"job": _service().enqueue_answer(mail_id, question_id=data.questionId, answer=data.answer, request_id=data.requestId)}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


class MailSyncInput(BaseModel):
    requestId: str = Field(min_length=1, max_length=100)
    process: bool = True


class ProcessPendingInput(BaseModel):
    includeUncertain: bool = True
    requestId: str = Field(min_length=1, max_length=100)


class ReminderRoleInput(BaseModel):
    roleId: str | None = None


class MailPreferencesInput(BaseModel):
    writingStyle: str = Field(max_length=2000)
    roleAccess: bool


class MailFollowupInput(BaseModel):
    enabled: bool
    delayHours: int = Field(default=24, ge=1, le=720)
    autoDraft: bool | None = None


class MailThreadFollowupInput(BaseModel):
    state: Literal["closed", "to_reply", "awaiting_reply", "clear"]


class MailFollowupDraftInput(BaseModel):
    requestId: str = Field(min_length=1, max_length=100)


@router.get("/preferences")
def get_preferences() -> dict[str, object]:
    return _service().preferences()


@router.put("/preferences")
def save_preferences(data: MailPreferencesInput) -> dict[str, object]:
    return _service().update_preferences(writing_style=data.writingStyle, role_access=data.roleAccess)


@router.get("/followup")
def get_followup_preferences() -> dict[str, object]:
    return _service().followup_preferences()


@router.put("/followup")
async def save_followup_preferences(data: MailFollowupInput) -> dict[str, object]:
    service = _service()
    result = service.update_followup_preferences(enabled=data.enabled, delay_hours=data.delayHours, auto_draft=data.autoDraft)
    await service.deliver_followup_reminders()
    return result


@router.get("/threads/{thread_id}/followup")
def get_thread_followup(thread_id: str) -> dict[str, object]:
    service = _service()
    if not service.store.thread_items(thread_id, account=service.store.connection().username):
        raise HTTPException(status_code=404, detail="邮件线程不存在")
    return {"followup": service._effective_thread_state_public(thread_id, account=service.store.connection().username)}


@router.put("/threads/{thread_id}/followup")
def set_thread_followup(thread_id: str, data: MailThreadFollowupInput) -> dict[str, object]:
    try:
        return {"followup": _service().set_followup_state(thread_id, data.state)}
    except KeyError as error:
        raise HTTPException(status_code=404, detail="邮件线程不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/threads/{thread_id}/followup-draft")
def create_followup_draft(thread_id: str, data: MailFollowupDraftInput) -> dict[str, object]:
    try:
        service = _service()
        service._require_draft_ready()
        if not service.store.thread_items(thread_id, account=service.store.connection().username):
            raise KeyError(thread_id)
        return {"job": service.enqueue_job(
            "followup_draft", target_id=thread_id, request_id=data.requestId,
            payload={"requestId": data.requestId},
        )}
    except KeyError as error:
        raise HTTPException(status_code=404, detail="邮件线程不存在") from error
    except (ValueError, PermissionError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/followup-reminders")
def list_followup_reminders(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, object]:
    service = _service()
    account = service.store.connection().username
    return {"reminders": [
        {"id": reminder.reminder_id, "threadId": reminder.thread_id, "dueKey": reminder.due_key,
         "dueAt": reminder.due_at, "state": reminder.state, "attempts": reminder.attempts,
         "lastError": reminder.last_error, "roleId": reminder.role_id, "createdAt": reminder.created_at,
         "updatedAt": reminder.updated_at, "deliveredAt": reminder.delivered_at}
        for reminder in service.store.followup_reminders(account=account, account_epoch=service.store.connection().account_epoch, limit=limit)
    ]}


@router.get("/status")
def get_status() -> dict[str, object]:
    return _service().public_status()


@router.get("/stats")
def get_stats(query: str | None = Query(default=None, max_length=200)) -> dict[str, object]:
    return _service().mail_stats(query=query.strip() if query else None)


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
def list_items(
    status: str | None = None,
    query: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    service = _service()
    account = service.store.connection().username
    normalized_query = query.strip() if query else None
    return {"items": service.list_items_public(status=status, query=normalized_query, limit=limit, offset=offset),
            "count": service.store.count_items(account=account, status=status, query=normalized_query)}


@router.get("/items/{mail_id}")
def get_item(mail_id: str) -> dict[str, object]:
    try:
        return _service().details_public(mail_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="邮件不存在") from error


@router.get("/threads")
def list_thread_actions(
    state: str | None = Query(default=None, max_length=40),
    action: str | None = Query(default=None, max_length=60),
    query: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    try:
        return _service().thread_actions_public(
            state_filter=state.strip() if state else None,
            action_filter=action.strip() if action else None,
            query=query.strip() if query else None,
            limit=limit,
            offset=offset,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _run_task(mail_id: str, request_id: str, *, kind: str, confirm_uncertain_new_version: bool = False) -> dict[str, object]:
    service = _service()
    try:
        if service.store.get_item(mail_id, account=service.store.connection().username) is None:
            raise HTTPException(status_code=404, detail="邮件不存在")
        payload: dict[str, object] = {"requestId": request_id}
        if confirm_uncertain_new_version:
            payload["confirmUncertainNewVersion"] = True
        return {"job": service.enqueue_job(kind, target_id=mail_id, request_id=request_id, payload=payload)}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/items/{mail_id}/process")
async def process_item(mail_id: str, data: MailTaskInput) -> dict[str, object]:
    return await _run_task(mail_id, data.requestId, kind="process", confirm_uncertain_new_version=data.confirmUncertainNewVersion)


@router.post("/items/{mail_id}/retry")
async def retry_item(mail_id: str, data: MailTaskInput) -> dict[str, object]:
    return await _run_task(mail_id, data.requestId, kind="retry", confirm_uncertain_new_version=data.confirmUncertainNewVersion)


@router.post("/items/{mail_id}/regenerate")
async def regenerate_item(mail_id: str, data: MailTaskInput) -> dict[str, object]:
    return await _run_task(mail_id, data.requestId, kind="regenerate", confirm_uncertain_new_version=data.confirmUncertainNewVersion)


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
def list_reminder_batches(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, object]:
    service = _service()
    account = service.store.connection().username
    return {"batches": service.store.reminder_batches(limit, account=account)}


@router.get("/audit")
def list_audit(limit: int = Query(default=100, ge=1, le=200)) -> dict[str, object]:
    service = _service()
    return {"events": service.store.list_audits(limit, account=service.store.connection().username)}
