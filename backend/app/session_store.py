import sqlite3
import uuid
import json
import hashlib
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from .agent_runtime.types import AgentMessage, AssistantMessage, AttachmentContent, ImageContent, ToolCall, ToolResultMessage, UserMessage
from .chat_attachments import load_image
from .models import AgentRun, Message, SessionSummary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_runtime_attachments(metadata: dict[str, Any]) -> tuple[AttachmentContent, ...]:
    raw = metadata.get("attachments", []) if isinstance(metadata, dict) else []
    if not isinstance(raw, list):
        return ()
    loaded: list[AttachmentContent] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        name = str(item.get("name") or "附件")
        media_type = str(item.get("mediaType") or "application/octet-stream")
        if not isinstance(path, str):
            continue
        text = str(item.get("text") or "")
        image = None
        if media_type.startswith("image/"):
            try:
                actual_type, encoded = load_image(path, media_type)
                image = ImageContent(actual_type, encoded)
            except (OSError, ValueError):
                continue
        loaded.append(AttachmentContent(name, media_type, text=text, image=image))
    return tuple(loaded)


def _channel_user_content(stored: Message) -> str:
    metadata = stored.metadata
    if not metadata.get("channel") or metadata.get("channel") == "desktop":
        return stored.content
    kind = "群友" if metadata.get("chatType") == "group" else "访客"
    sender = "主人" if metadata.get("senderIsOwner") is True else kind
    name = str(metadata.get("senderName") or metadata.get("senderId") or "")
    lines = [f"[渠道 {metadata['channel']}；chat_id={metadata.get('chatId', '')}；{sender} {name}（ID {metadata.get('senderId', '')}）]"]
    if metadata.get("groupName"):
        lines.append(f"群名：{metadata['groupName']}")
    if metadata.get("mentionedIds"):
        lines.append("提及 ID：" + "、".join(str(value) for value in metadata["mentionedIds"]))
    attachments = [item for key in ("attachments", "quotedAttachments") for item in metadata.get(key, []) if isinstance(item, dict) and item.get("id")]
    if attachments:
        lines.append("本消息附件：" + "；".join(f"{item.get('name', '附件')}（attachment_id={item['id']}）" for item in attachments))
    return "\n".join([*lines, stored.content])


def _runtime_assistant_metadata(metadata: dict[str, Any]) -> dict[str, object]:
    internal_keys = {
        "stopReason",
        "toolCallGroupId",
        "toolCallIndex",
        "toolCallCount",
        "runtimeStopReason",
        "runtimePlaceholder",
    }
    return {key: value for key, value in metadata.items() if key not in internal_keys}


def _runtime_assistant_message(stored: Message) -> AssistantMessage:
    content = stored.content
    if stored.messageType == "desktop_push":
        raw_attachments = stored.metadata.get("attachments", [])
        attachments = raw_attachments if isinstance(raw_attachments, list) else []
        labels = [f"{item.get('name', '附件')}（attachment_id={item['id']}）"
                  for item in attachments if isinstance(item, dict) and item.get('id')]
        if labels:
            content = "\n".join(filter(None, (content, "已推送附件：" + "；".join(labels))))
    return AssistantMessage(content, metadata=_runtime_assistant_metadata(stored.metadata),
                            timestamp=_timestamp(stored.metadata), source_message_ids=(stored.id,))


class AttachmentConflict(ValueError):
    """The explicitly selected message/image no longer has its original slot."""


class SessionStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        with sqlite3.connect(self.database_path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
            if "agent_runs" in tables and "run_id" in message_columns:
                connection.execute(
                    "UPDATE messages SET status = 'failed' WHERE status = 'streaming' AND (run_id IS NULL OR run_id IN (SELECT run_id FROM agent_runs WHERE status IN ('created', 'running')) )"
                )
                connection.execute(
                    "UPDATE agent_runs SET status='failed', ended_at=?, error=COALESCE(error, '服务重启时运行未完成') WHERE status IN ('created', 'running')",
                    (_now(),),
                )
                if "tool_audits" in tables:
                    connection.execute(
                        "UPDATE tool_audits SET ended_at=COALESCE(ended_at, ?), result_category=COALESCE(result_category, 'failed'), error_type=COALESCE(error_type, 'RuntimeError'), error_message=COALESCE(error_message, '服务重启时工具运行未完成') WHERE ended_at IS NULL",
                        (_now(),),
                    )
            elif "messages" in tables and "status" in message_columns:
                # Legacy databases may be opened before initialize_databases.
                connection.execute("UPDATE messages SET status = 'failed' WHERE status = 'streaming'")

    def open_role_session(self, role_id: str) -> SessionSummary:
        key = f"role:{role_id}"
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sessions (session_key, role_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (key, role_id, now, now),
            )
            row = connection.execute(
                "SELECT session_key, role_id, created_at, updated_at FROM sessions WHERE session_key = ?",
                (key,),
            ).fetchone()
        assert row is not None
        return self._session(row)

    def list_messages(self, session_key: str) -> list[Message]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, is_error, run_id, metadata_json FROM messages WHERE session_key = ? ORDER BY sequence ASC",
                (session_key,),
            ).fetchall()
        return [self._message(row) for row in rows]

    def budget_diagnostics(self):
        allowed = {'requests', 'compactions', 'inputEstimate', 'maxTokens', 'maxIterations', 'toolCount', 'elapsedMs', 'modelId'}
        with sqlite3.connect(self.database_path) as db:
            rows = db.execute('SELECT run_id, role_id, model_snapshot_json FROM agent_runs ORDER BY started_at DESC LIMIT 20').fetchall()
        result = []
        for run_id, role_id, payload in rows:
            data = json.loads(payload or '{}').get('budgetDiagnostic')
            if isinstance(data, dict):
                result.append({'runId': run_id, 'roleId': role_id, **{key: data[key] for key in allowed if key in data}})
        return result

    def user_message_for_run(self, run_id: str) -> Message | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, is_error, run_id, metadata_json "
                "FROM messages WHERE run_id=? AND role='user' AND status='completed' ORDER BY sequence LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._message(row) if row else None

    def context_messages(self, session_key: str) -> list[Message]:
        return [
            message
            for message in self.list_messages(session_key)
            if message.messageType == "text"
            and (message.role == "user" or message.status == "completed")
        ]

    def append_message(
        self,
        session_key: str,
        role: str,
        content: str,
        status: str = "completed",
        *,
        message_type: str = "text",
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        tool_arguments: dict[str, object] | None = None,
        tool_result: object | None = None,
        is_error: bool = False,
        run_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Message:
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            sequence = int(row[0])
            message_id = f"{session_key}:{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, is_error, run_id, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, session_key, sequence, role, content, status, now, message_type, tool_call_id, tool_name, _dump(tool_arguments), _dump(tool_result), int(is_error), run_id, _dump(metadata or {})),
            )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE session_key = ?", (now, session_key))
        return Message(id=message_id, sessionKey=session_key, sequence=sequence, role=role, content=content, status=status, createdAt=datetime.fromisoformat(now), messageType=message_type, toolCallId=tool_call_id, toolName=tool_name, toolArguments=tool_arguments, toolResult=tool_result, isError=is_error, runId=run_id, metadata=metadata or {})

    def append_task_completion(self, role_id: str, session_key: str, task_id: str, content: str, *, task_status: str) -> Message:
        """Append one clearly internal completion to an existing original session.

        This application boundary never creates a session or a user turn and
        cannot drive owner memory maintenance. The deterministic ID closes the
        crash window between this database and the task outbox acknowledgement.
        """
        message_id = f"{session_key}:task-completion:{task_id}"
        with sqlite3.connect(self.database_path, timeout=.05) as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute("SELECT role_id FROM sessions WHERE session_key=?", (session_key,)).fetchone()
            if session is None and session_key.startswith("thread:"):
                session = connection.execute("SELECT role_id FROM channel_threads WHERE session_key=?", (session_key,)).fetchone()
            if session is None or session[0] != role_id:
                raise PermissionError("original task session no longer exists")
            existing = connection.execute("SELECT message_id,session_key,sequence,role,content,status,created_at,message_type,tool_call_id,tool_name,tool_arguments_json,tool_result_json,is_error,run_id,metadata_json FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if existing is not None:
                return self._message(existing)
            sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE session_key=?", (session_key,)).fetchone()[0]
            now = _now()
            metadata = {"internalEvent": "spawn_completion", "taskId": task_id, "taskStatus": task_status}
            channel = connection.execute("SELECT channel FROM channel_threads WHERE session_key=? AND role_id=?", (session_key, role_id)).fetchone() if session_key.startswith("thread:") else None
            if session_key.startswith("thread:") and channel is None:
                raise PermissionError("original task channel no longer exists")
            if channel is not None:
                # The very first durable row must already be excluded from
                # delivered history, including a crash before the outbox ack.
                metadata.update({"channel": channel[0], "deliveryStatus": "pending"})
            connection.execute("INSERT INTO messages(message_id,session_key,sequence,role,content,status,created_at,message_type,metadata_json) VALUES(?,?,?,'assistant',?,'completed',?,'internal_completion',?)",
                               (message_id, session_key, sequence, content, now, _dump(metadata)))
            connection.execute("UPDATE sessions SET updated_at=? WHERE session_key=?", (now, session_key))
            if session_key.startswith("thread:"):
                connection.execute("UPDATE channel_threads SET last_activity=? WHERE session_key=?", (now, session_key))
        return Message(id=message_id, sessionKey=session_key, sequence=sequence, role="assistant", content=content,
                       status="completed", createdAt=datetime.fromisoformat(now), messageType="internal_completion", metadata=metadata)

    def reconcile_task_completion(self, role_id: str, session_key: str, task_id: str, *,
                                  delivery_status: str, delivery_attempt_id: object) -> None:
        """Repair an existing channel completion without a long event-loop lock wait."""
        if delivery_status not in {"pending", "sent", "failed", "uncertain"}:
            raise ValueError("invalid task delivery status")
        message_id = f"{session_key}:task-completion:{task_id}"
        with sqlite3.connect(self.database_path, timeout=.05) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT m.metadata_json,t.channel FROM messages m JOIN channel_threads t ON t.session_key=m.session_key "
                "WHERE m.message_id=? AND m.session_key=? AND t.role_id=? AND m.role='assistant' AND m.message_type='internal_completion'",
                (message_id, session_key, role_id),
            ).fetchone()
            if row is None:
                return
            metadata = {**(_load_dict(row[0]) or {}), "channel": row[1], "deliveryStatus": delivery_status,
                        "deliveryAttemptId": delivery_attempt_id}
            connection.execute("UPDATE messages SET metadata_json=? WHERE message_id=?", (_dump(metadata), message_id))

    def append_generation_attachments_once(self, role_id: str, session_key: str, run_id: str,
                                          message_id: str, attempt_id: str, attachments: list[dict[str, object]], *,
                                          connection: sqlite3.Connection, authorized: Callable[[], bool]) -> Message:
        """Consume generated media with its business outbox's existing transaction."""
        if not connection.in_transaction:
            raise ValueError("generation consumption requires an owned transaction")
        database = connection.execute("PRAGMA database_list").fetchone()
        if database is None or Path(database[2]).resolve() != Path(self.database_path).resolve():
            raise ValueError("generation transaction uses a different database")
        if not attempt_id or len(attempt_id) > 128 or not 1 <= len(attachments) <= 4:
            raise ValueError("invalid generation attachment claim")
        ids = []
        for item in attachments:
            identifier = item.get("id")
            if (not isinstance(identifier, str) or not identifier or identifier in ids
                    or not str(item.get("mediaType", "")).startswith("image/")
                    or any(key in item for key in ("path", "data", "base64", "data_base64"))):
                raise ValueError("generation requires public image attachment references")
            ids.append(identifier)
        owner = connection.execute("SELECT role_id FROM sessions WHERE session_key=?", (session_key,)).fetchone()
        if owner is None and session_key.startswith("thread:"):
            owner = connection.execute("SELECT role_id FROM channel_threads WHERE session_key=?", (session_key,)).fetchone()
        run = connection.execute("SELECT role_id,session_key,status FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if (owner is None or owner[0] != role_id or run is None
                or (run[0], run[1], run[2]) != (role_id, session_key, "completed")):
            raise PermissionError("generation reply does not belong to its completed source run")
        columns = 'message_id,session_key,sequence,role,content,status,created_at,message_type,tool_call_id,tool_name,tool_arguments_json,tool_result_json,is_error,run_id,metadata_json'
        row = connection.execute(f"SELECT {columns} FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            raise PermissionError("generation final reply is unavailable")
        message = self._message(row)
        if ((message.sessionKey, message.runId, message.role, message.status, message.messageType)
                != (session_key, run_id, "assistant", "completed", "text")
                or not connection.execute("SELECT 1 FROM messages WHERE run_id=? AND role='user' AND message_type='text'",
                                          (run_id,)).fetchone()):
            raise PermissionError("generation requires the original final assistant reply")
        metadata = dict(message.metadata)
        consumed = metadata.get("generationAttemptIds", [])
        prior = metadata.get("attachments", [])
        if (not isinstance(consumed, list) or any(not isinstance(value, str) for value in consumed)
                or not isinstance(prior, list) or any(not isinstance(item, dict) for item in prior)):
            raise ValueError("generation reply has invalid attachment metadata")
        if not authorized():
            raise PermissionError("generation delivery permission was revoked")
        if attempt_id in consumed:
            return message
        prior_ids = {item.get("id") for item in prior if isinstance(item.get("id"), str)}
        metadata["attachments"] = [*prior, *(item for item in attachments if item["id"] not in prior_ids)]
        metadata["generationAttemptIds"] = [*consumed, attempt_id]
        if not authorized():
            raise PermissionError("generation delivery permission was revoked")
        connection.execute("UPDATE messages SET metadata_json=? WHERE message_id=?", (_dump(metadata), message_id))
        return message.model_copy(update={"metadata": metadata})

    def append_autonomy_message_once(self, role_id: str, tick_id: str, intent_id: str, content: str, *,
                                     connection: sqlite3.Connection, authorized: Callable[[], bool],
                                     mood: str, thought: str, mode: str = 'active_decision',
                                     attachments: tuple[dict[str, object], ...] = ()) -> Message:
        """The autonomy outbox owns this transaction and canonical receipt."""
        if not connection.in_transaction:
            raise ValueError("autonomous delivery requires an owned transaction")
        database = connection.execute("PRAGMA database_list").fetchone()
        if database is None or Path(database[2]).resolve() != Path(self.database_path).resolve():
            raise ValueError("autonomous delivery uses a different database")
        session_key = f"role:{role_id}"
        message_id = f"{session_key}:autonomy:{intent_id}"
        if mode not in {'active_decision', 'drift'}:
            raise ValueError('invalid autonomous delivery mode')
        metadata: dict[str, Any] = {"internalEvent": mode, "autonomyTickId": tick_id,
                    "autonomyIntentId": intent_id, "channel": "desktop", "deliveryStatus": "sent",
                    "mood": mood, "thought": thought}
        if attachments:
            metadata['attachments'] = list(attachments)
        result = self._append_owned_message_once(connection, role_id, session_key, message_id, content,
            message_type="internal_completion", metadata=metadata, authorized=authorized, create_session=True)
        if result.content != content or result.metadata != metadata or result.messageType != "internal_completion":
            raise ValueError("autonomous desktop receipt conflicts with its original intent")
        return result

    def replace_attachment_once(self, role_id: str, session_key: str, message_id: str,
                                expected_attachment_id: str, new_attachment: dict[str, object], attempt_id: str, *,
                                connection: sqlite3.Connection, authorized: Callable[[], bool]) -> Message:
        """Replace one image while its canonical generation outbox owns the transaction."""
        if not connection.in_transaction:
            raise ValueError('generation replacement requires an owned transaction')
        database = connection.execute('PRAGMA database_list').fetchone()
        if database is None or Path(database[2]).resolve() != Path(self.database_path).resolve():
            raise ValueError('generation replacement uses a different database')
        size = new_attachment.get('size')
        if (not attempt_id or len(attempt_id) > 128 or not expected_attachment_id
                or not isinstance(new_attachment.get('id'), str) or not new_attachment['id']
                or set(new_attachment) - {'id', 'name', 'mediaType', 'size', 'kind'}
                or new_attachment.get('mediaType') not in {'image/png', 'image/jpeg', 'image/webp'}
                or not isinstance(new_attachment.get('name'), str)
                or new_attachment.get('kind', 'image') != 'image'
                or not isinstance(size, int) or isinstance(size, bool) or size < 1):
            raise ValueError('generation replacement requires public image references')
        if not authorized():
            raise PermissionError('generation replacement permission was revoked')
        owner = connection.execute('SELECT role_id FROM sessions WHERE session_key=?', (session_key,)).fetchone()
        if owner is None and session_key.startswith('thread:'):
            owner = connection.execute('SELECT role_id FROM channel_threads WHERE session_key=?', (session_key,)).fetchone()
        columns = 'message_id,session_key,sequence,role,content,status,created_at,message_type,tool_call_id,tool_name,tool_arguments_json,tool_result_json,is_error,run_id,metadata_json'
        row = connection.execute(f'SELECT {columns} FROM messages WHERE message_id=?', (message_id,)).fetchone()
        if owner is None or owner[0] != role_id:
            raise PermissionError('original generation message is unavailable')
        if row is None:
            raise AttachmentConflict('original generation message was removed')
        message = self._message(row)
        if ((message.sessionKey, message.role, message.status) != (session_key, 'assistant', 'completed')
                or message.messageType not in {'text', 'internal_completion'}):
            raise PermissionError('generation replacement requires a final assistant message')
        if message.messageType == 'text':
            run = connection.execute('SELECT role_id,session_key,status FROM agent_runs WHERE run_id=?', (message.runId,)).fetchone()
            if (run is None or tuple(run) != (role_id, session_key, 'completed')
                    or not connection.execute("SELECT 1 FROM messages WHERE run_id=? AND role='user' AND message_type='text'", (message.runId,)).fetchone()):
                raise PermissionError('generation replacement requires the completed source run')
        elif message.metadata.get('internalEvent') != 'novelai_generation':
            raise PermissionError('generation replacement requires a generated completion')
        metadata = dict(message.metadata)
        prior, consumed = metadata.get('attachments', []), metadata.get('generationAttemptIds', [])
        receipts = metadata.get('generationReplacementReceipts', {})
        if (not isinstance(prior, list) or any(not isinstance(item, dict) for item in prior)
                or not isinstance(consumed, list) or any(not isinstance(value, str) for value in consumed)
                or not isinstance(receipts, dict)):
            raise ValueError('generation message has invalid attachment metadata')
        fingerprint = hashlib.sha256(json.dumps([expected_attachment_id, new_attachment],
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if attempt_id in receipts:
            if receipts[attempt_id] != fingerprint:
                raise ValueError('generation replacement conflicts with its original receipt')
            if not authorized():
                raise PermissionError('generation replacement permission was revoked')
            return message
        slots = [index for index, item in enumerate(prior) if item.get('id') == expected_attachment_id]
        if (len(slots) != 1 or not str(prior[slots[0]].get('mediaType', '')).startswith('image/')
                or any(item.get('id') == new_attachment['id'] for item in prior)):
            raise AttachmentConflict('selected generation attachment has changed')
        metadata['attachments'] = [new_attachment if index == slots[0] else item for index, item in enumerate(prior)]
        metadata['generationAttemptIds'] = [*consumed, *([] if attempt_id in consumed else [attempt_id])]
        metadata['generationReplacementReceipts'] = {**receipts, attempt_id: fingerprint}
        if not authorized():
            raise PermissionError('generation replacement permission was revoked')
        connection.execute('UPDATE messages SET metadata_json=? WHERE message_id=?', (_dump(metadata), message_id))
        return message.model_copy(update={'metadata': metadata})

    def append_desktop_push_once(self, role_id: str, source_run_id: str, call_id: str, content: str, *,
                                 attachments: list[dict[str, object]], authorized: Callable[[], bool]) -> Message:
        """Persist a role's own message independently of the parent run outcome."""
        session_key = f"role:{role_id}"
        event = hashlib.sha256(json.dumps([role_id, source_run_id, call_id]).encode()).hexdigest()
        message_id = f"{session_key}:desktop-push:{event}"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            metadata = {"internalEvent": "desktop_push", "channel": "desktop", "deliveryStatus": "sent",
                        "sourceRunId": source_run_id, "sourceToolCallId": call_id, "attachments": attachments}
            return self._append_owned_message_once(connection, role_id, session_key, message_id, content,
                message_type='desktop_push', metadata=metadata, authorized=authorized, create_session=True)

    def append_generation_completion_once(self, role_id: str, session_key: str, attempt_id: str,
                                          attachments: list[dict[str, object]], *,
                                          connection: sqlite3.Connection, authorized: Callable[[], bool],
                                          status: str = 'succeeded', channel: str = 'desktop',
                                          delivery_status: str = 'sent') -> Message:
        """Publish owned media in its existing original session without a human run."""
        if not connection.in_transaction:
            raise ValueError('generation completion requires an owned transaction')
        database = connection.execute('PRAGMA database_list').fetchone()
        if database is None or Path(database[2]).resolve() != Path(self.database_path).resolve():
            raise ValueError('generation transaction uses a different database')
        if (not attempt_id or len(attempt_id) > 128 or status != 'succeeded'
                or delivery_status not in {'pending', 'sent', 'failed', 'uncertain'}
                or not 1 <= len(attachments) <= 16):
            raise ValueError('invalid generation completion source or media')
        if session_key == f'role:{role_id}':
            if channel != 'desktop' or delivery_status != 'sent':
                raise ValueError('invalid desktop generation completion')
        elif session_key.startswith('thread:'):
            thread = connection.execute('SELECT role_id,channel FROM channel_threads WHERE session_key=?', (session_key,)).fetchone()
            if thread is None or (thread[0], thread[1]) != (role_id, channel):
                raise PermissionError('original generation channel is unavailable')
        else:
            raise ValueError('invalid original generation session')
        ids = set()
        for item in attachments:
            identifier = item.get('id')
            size = item.get('size')
            if (not isinstance(identifier, str) or not identifier or identifier in ids
                    or set(item) - {'id', 'name', 'mediaType', 'size', 'kind'}
                    or item.get('mediaType') not in {'image/png', 'image/jpeg', 'image/webp'}
                    or not isinstance(size, int) or isinstance(size, bool) or size < 1):
                raise ValueError('generation completion requires public image references')
            ids.add(identifier)
        # The owner of this exact business transaction also holds its admission
        # lock; the callback rechecks immediately before the shared insert seam.
        if not authorized():
            raise PermissionError('generation completion permission was revoked')
        metadata = {'internalEvent': 'novelai_generation', 'channel': channel,
                    'deliveryStatus': delivery_status, 'generationAttemptIds': [attempt_id], 'attachments': attachments}
        return self._append_owned_message_once(connection, role_id, session_key,
            f'{session_key}:generation:{attempt_id}', '图片已生成', message_type='internal_completion',
            metadata=metadata, authorized=authorized, create_session=False)

    def _append_owned_message_once(self, connection, role_id, session_key, message_id, content, *,
                                   message_type, metadata, authorized, create_session=False):
        """Called inside the owning application's write transaction."""
        if not authorized():
            raise PermissionError('internal delivery permission was revoked')
        columns = 'message_id,session_key,sequence,role,content,status,created_at,message_type,tool_call_id,tool_name,tool_arguments_json,tool_result_json,is_error,run_id,metadata_json'
        now = _now()
        if create_session:
            connection.execute('INSERT OR IGNORE INTO sessions(session_key,role_id,created_at,updated_at) VALUES(?,?,?,?)',
                               (session_key, role_id, now, now))
        session = connection.execute('SELECT role_id FROM sessions WHERE session_key=?', (session_key,)).fetchone()
        if session is None and session_key.startswith('thread:'):
            session = connection.execute('SELECT role_id FROM channel_threads WHERE session_key=?', (session_key,)).fetchone()
        if session is None or session[0] != role_id:
            raise PermissionError('original delivery session is unavailable')
        existing = connection.execute(f'SELECT {columns} FROM messages WHERE message_id=?', (message_id,)).fetchone()
        if existing is not None:
            return self._message(existing)
        sequence = connection.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE session_key=?', (session_key,)).fetchone()[0]
        if not authorized():
            raise PermissionError('internal delivery permission was revoked')
        connection.execute("INSERT INTO messages(message_id,session_key,sequence,role,content,status,created_at,message_type,metadata_json) VALUES(?,?,?,'assistant',?,'completed',?,?,?)",
                           (message_id, session_key, sequence, content, now, message_type, _dump(metadata)))
        connection.execute('UPDATE sessions SET updated_at=? WHERE session_key=?', (now, session_key))
        if session_key.startswith('thread:'):
            connection.execute('UPDATE channel_threads SET last_activity=? WHERE session_key=?', (now, session_key))
        stored = connection.execute(f'SELECT {columns} FROM messages WHERE message_id=?', (message_id,)).fetchone()
        return self._message(stored)

    def reconcile_generation_receipt(self, role_id, session_key, message_id, attempt_id, receipt, *,
                                     connection, authorized):
        """Project a durable media receipt in its outbox owner's transaction."""
        if not connection.in_transaction:
            raise ValueError('generation receipt requires an owned transaction')
        database = connection.execute('PRAGMA database_list').fetchone()
        if database is None or Path(database[2]).resolve() != Path(self.database_path).resolve():
            raise ValueError('generation receipt uses a different database')
        if set(receipt) != {'status', 'deliveryAttemptId', 'platformMessageId'} or receipt['status'] not in {'pending', 'sent', 'failed', 'uncertain'}:
            raise ValueError('invalid generation receipt')
        columns = 'message_id,session_key,sequence,role,content,status,created_at,message_type,tool_call_id,tool_name,tool_arguments_json,tool_result_json,is_error,run_id,metadata_json'
        row = connection.execute(f'SELECT {columns} FROM messages WHERE message_id=? AND session_key=?', (message_id, session_key)).fetchone()
        if row is None:
            return None
        message = self._message(row)
        owner = connection.execute('SELECT role_id FROM channel_threads WHERE session_key=?', (session_key,)).fetchone()
        if (owner is None or owner[0] != role_id or message.role != 'assistant'
                or message.messageType not in {'text', 'internal_completion'}
                or attempt_id not in message.metadata.get('generationAttemptIds', [])):
            raise PermissionError('generation receipt does not belong to its original media message')
        metadata = {**message.metadata, 'deliveryStatus': receipt['status'],
            'deliveryAttemptId': receipt['deliveryAttemptId'], 'platformMessageId': receipt['platformMessageId']}
        if receipt['status'] == 'sent':
            metadata.pop('deliveryError', None)
        if not authorized():
            raise PermissionError('generation receipt projection was revoked')
        connection.execute('UPDATE messages SET metadata_json=?,status=? WHERE message_id=?',
            (_dump(metadata), 'completed' if receipt['status'] == 'sent' else 'failed', message_id))
        return self._message(connection.execute(f'SELECT {columns} FROM messages WHERE message_id=?', (message_id,)).fetchone())

    def append_schedule_message_once(self, role_id, session_key, schedule_id, occurrence_id, content, *,
                                      authorized, connection, channel='desktop', delivery_status='sent', attempt_id=None):
        """Schedule's own transaction includes both its cancel gate and receipt."""
        message_id = f'{session_key}:schedule:{occurrence_id}'
        metadata = {'internalEvent': 'schedule', 'scheduleId': schedule_id, 'occurrenceId': occurrence_id,
                    'channel': channel, 'deliveryStatus': delivery_status, 'deliveryAttemptId': attempt_id}
        message = self._append_owned_message_once(connection, role_id, session_key, message_id, content,
            message_type='internal_completion', metadata=metadata, authorized=authorized)
        if message.metadata != metadata:
            connection.execute('UPDATE messages SET metadata_json=? WHERE message_id=?', (_dump(metadata), message_id))
        return message

    def append_coding_completion_once(self, role_id, session_key, task_id, attempt_id, content, *,
                                      authorized, connection, delivery_status='pending', delivery_attempt_id=None,
                                      task_status='unknown'):
        """Coding outbox and its existing-session message share one transaction."""
        if delivery_status not in {'pending', 'sent', 'failed', 'uncertain'}:
            raise ValueError('invalid coding delivery status')
        if not task_id or not attempt_id or len(task_id) > 100 or len(attempt_id) > 100:
            raise ValueError('invalid coding completion identity')
        database = connection.execute('PRAGMA database_list').fetchone()
        if database is None or Path(database[2]).resolve() != Path(self.database_path).resolve():
            raise ValueError('coding completion requires its canonical session database')
        channel = 'desktop'
        if session_key.startswith('thread:'):
            row = connection.execute('SELECT channel FROM channel_threads WHERE session_key=? AND role_id=?',
                                     (session_key, role_id)).fetchone()
            if row is None:
                raise PermissionError('original coding thread no longer exists')
            channel = row[0]
        elif session_key != f'role:{role_id}':
            raise PermissionError('original coding desktop session does not match role')
        message_id = f'{session_key}:coding-completion:{attempt_id}'
        metadata = {'internalEvent': 'coding_completion', 'codingTaskId': task_id, 'attemptId': attempt_id,
                    'taskStatus': task_status, 'channel': channel, 'deliveryStatus': delivery_status,
                    'deliveryAttemptId': delivery_attempt_id}
        message = self._append_owned_message_once(connection, role_id, session_key, message_id, content,
            message_type='internal_completion', metadata=metadata, authorized=authorized)
        if message.content != content:
            raise ValueError('coding completion content conflicts with its durable identity')
        # A later failed local projection must never downgrade a real receipt.
        if message.metadata.get('deliveryStatus') == 'sent':
            metadata['deliveryStatus'] = 'sent'
            metadata['deliveryAttemptId'] = message.metadata.get('deliveryAttemptId')
        if message.metadata != metadata:
            connection.execute('UPDATE messages SET metadata_json=? WHERE message_id=?', (_dump(metadata), message_id))
            message.metadata.update(metadata)
        return message

    def append_coding_operation_once(self, role_id, session_key, task_id, attempt_id, operation_id, kind, content, *,
                                     connection, authorized, operation_status, delivery_status, delivery_attempt_id=None):
        """A reviewed host operation has its own original-source completion."""
        if kind not in {'coding_commit', 'coding_publication'} or delivery_status not in {'pending', 'sent', 'failed', 'uncertain'}:
            raise ValueError('invalid Coding operation completion')
        if not all(isinstance(value, str) and 1 <= len(value) <= 100 for value in (task_id, attempt_id, operation_id)):
            raise ValueError('invalid Coding operation identity')
        database = connection.execute('PRAGMA database_list').fetchone()
        if not connection.in_transaction or database is None or Path(database[2]).resolve() != Path(self.database_path).resolve():
            raise ValueError('Coding operation requires its canonical transaction')
        channel = 'desktop'
        if session_key.startswith('thread:'):
            row = connection.execute('SELECT channel FROM channel_threads WHERE session_key=? AND role_id=?',
                (session_key, role_id)).fetchone()
            if row is None:
                raise PermissionError('original Coding operation thread no longer exists')
            channel = row[0]
        elif session_key != f'role:{role_id}':
            raise PermissionError('original Coding operation session does not match role')
        metadata = {'internalEvent': kind, 'codingTaskId': task_id, 'attemptId': attempt_id,
            'operationId': operation_id, 'operationStatus': operation_status, 'channel': channel,
            'deliveryStatus': delivery_status, 'deliveryAttemptId': delivery_attempt_id}
        message_id = f'{session_key}:coding-operation:{operation_id}'
        message = self._append_owned_message_once(connection, role_id, session_key, message_id, content,
            message_type='internal_completion', metadata=metadata, authorized=authorized)
        if message.content != content:
            raise ValueError('Coding operation completion content conflicts with durable identity')
        if message.metadata.get('deliveryStatus') == 'sent':
            metadata['deliveryStatus'] = 'sent'
            metadata['deliveryAttemptId'] = message.metadata.get('deliveryAttemptId')
        if message.metadata != metadata:
            if not authorized():
                raise PermissionError('Coding operation projection was revoked')
            connection.execute('UPDATE messages SET metadata_json=? WHERE message_id=?', (_dump(metadata), message_id))
            message.metadata.update(metadata)
        return message

    def append_proactive_message_once(
        self,
        session_key: str,
        content: str,
        *,
        event_id: str,
        metadata: dict[str, object] | None = None,
    ) -> Message:
        """Persist one proactive role message per durable event ID."""

        if not event_id.strip():
            raise ValueError("主动消息缺少事件标识")
        message_metadata = {**(metadata or {}), "proactiveEventId": event_id}
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT m.message_id,m.session_key,m.sequence,m.role,m.content,m.status,m.created_at,m.message_type,m.tool_call_id,m.tool_name,m.tool_arguments_json,m.tool_result_json,m.is_error,m.run_id,m.metadata_json "
                "FROM proactive_messages p JOIN messages m ON m.message_id=p.message_id WHERE p.event_id=?",
                (event_id,),
            ).fetchall()
            for row in rows:
                return self._message(row)
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE session_key=?",
                (session_key,),
            ).fetchone()
            sequence = int(row[0])
            now = _now()
            message_id = f"{session_key}:{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO messages(message_id,session_key,sequence,role,content,status,created_at,message_type,run_id,metadata_json) "
                "VALUES(?,?,?,'assistant',?,'completed',?,'proactive_reminder',NULL,?)",
                (message_id, session_key, sequence, content, now, _dump(message_metadata)),
            )
            connection.execute(
                "INSERT INTO proactive_messages(event_id,message_id,created_at) VALUES(?,?,?)",
                (event_id, message_id, now),
            )
            connection.execute("UPDATE sessions SET updated_at=? WHERE session_key=?", (now, session_key))
            stored = connection.execute(
                "SELECT message_id,session_key,sequence,role,content,status,created_at,message_type,tool_call_id,tool_name,tool_arguments_json,tool_result_json,is_error,run_id,metadata_json FROM messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        assert stored is not None
        return self._message(stored)

    def update_message(
        self,
        message_id: str,
        content: str,
        status: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> Message:
        with sqlite3.connect(self.database_path) as connection:
            if metadata is None:
                connection.execute("UPDATE messages SET content = ?, status = ? WHERE message_id = ?", (content, status, message_id))
            else:
                connection.execute("UPDATE messages SET content = ?, status = ?, metadata_json = ? WHERE message_id = ?", (content, status, _dump(metadata), message_id))
            row = connection.execute(
                "SELECT message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, is_error, run_id, metadata_json FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return self._message(row)

    def delete_message(self, message_id: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))

    def append_agent_message(
        self,
        session_key: str,
        message: AgentMessage,
        *,
        run_id: str | None = None,
        status: str = "completed",
    ) -> list[Message]:
        """Persist an internal runtime message at a message-end boundary.

        A provider assistant may contain several tool calls, so each call gets
        its own ordered row. This keeps the SQLite transcript unambiguous when
        a run is resumed or inspected after a restart.
        """

        if isinstance(message, UserMessage):
            metadata: dict[str, object] = {}
            if message.timestamp is not None:
                metadata["timestamp"] = message.timestamp
            return [self.append_message(session_key, "user", message.content, status, run_id=run_id, metadata=metadata)]
        if isinstance(message, AssistantMessage):
            assistant_metadata = dict(message.metadata)
            if message.timestamp is not None:
                assistant_metadata["timestamp"] = message.timestamp
            if not message.tool_calls:
                return [self.append_message(
                    session_key,
                    "assistant",
                    message.content,
                    status,
                    run_id=run_id,
                    metadata=assistant_metadata,
                )]
            group_id = uuid.uuid4().hex
            now = _now()
            with sqlite3.connect(self.database_path) as connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                sequence = int(row[0])
                persisted: list[Message] = []
                for index, call in enumerate(message.tool_calls):
                    # Browser inputs can contain passwords, tokens, or page
                    # scripts. Persist the safe transcript form only; the
                    # actual arguments were already used by the current run.
                    if call.name.startswith("agent_browser_"):
                        from .browser_use.safety import sanitize_browser_arguments

                        safe_arguments = sanitize_browser_arguments(call.name, call.arguments)
                    else:
                        safe_arguments = call.arguments
                    message_id = f"{session_key}:{uuid.uuid4().hex}"
                    metadata = {
                        **assistant_metadata,
                        "stopReason": message.stop_reason,
                        "toolCallGroupId": group_id,
                        "toolCallIndex": index,
                        "toolCallCount": len(message.tool_calls),
                    }
                    connection.execute(
                        "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, run_id, metadata_json) VALUES (?, ?, ?, 'assistant', ?, ?, ?, 'tool_call', ?, ?, ?, ?, ?)",
                        (message_id, session_key, sequence, message.content, status, now, call.id, call.name, _dump(safe_arguments), run_id, _dump(metadata)),
                    )
                    persisted.append(Message(
                        id=message_id,
                        sessionKey=session_key,
                        sequence=sequence,
                        role="assistant",
                        content=message.content,
                        status=status,
                        createdAt=datetime.fromisoformat(now),
                        messageType="tool_call",
                        toolCallId=call.id,
                        toolName=call.name,
                        toolArguments=safe_arguments,
                        runId=run_id,
                        metadata=metadata,
                    ))
                    sequence += 1
                connection.execute("UPDATE sessions SET updated_at = ? WHERE session_key = ?", (now, session_key))
            return persisted
        if isinstance(message, ToolResultMessage):
            safe_content = message.content
            if message.tool_name.startswith("agent_browser_"):
                from .browser_use.safety import sanitize_browser_text

                safe_content, _ = sanitize_browser_text(safe_content)
            metadata: dict[str, object] = {}
            if message.usage is not None:
                metadata["usage"] = asdict(message.usage) if is_dataclass(message.usage) else message.usage
            if message.nested_calls:
                metadata["nestedCalls"] = [asdict(call) for call in message.nested_calls]
            if message.timestamp is not None:
                metadata["timestamp"] = message.timestamp
            return [
                self.append_message(
                    session_key,
                    "tool",
                    safe_content,
                    status,
                    message_type="tool_result",
                    tool_call_id=message.tool_call_id,
                    tool_name=message.tool_name,
                    tool_result=message.details,
                    is_error=message.is_error,
                    run_id=run_id,
                    metadata=metadata,
                )
            ]
        raise TypeError(f"unsupported runtime message: {type(message)!r}")

    def runtime_messages(self, session_key: str, *, max_messages: int | None = None, since: str | None = None, through_message_id: str | None = None) -> list[AgentMessage]:
        """Rebuild a provider transcript from the structured SQLite rows."""

        stored_messages = self.list_messages(session_key)
        if through_message_id is not None:
            source = next((item for item in stored_messages if item.id == through_message_id and item.role == "user"), None)
            if source is None:
                raise KeyError(through_message_id)
            # Polling may persist another sender while this turn waits for the
            # role lock. Never let a later request inherit this source's tools.
            run_sources = {
                item.runId: item.sequence for item in stored_messages
                if item.role == "user" and item.runId and item.sequence < source.sequence
            }
            stored_messages = [
                item for item in stored_messages
                if item.sequence <= source.sequence or item.runId in run_sources
            ]
            # An earlier turn can finish after the next user's intake was saved.
            # Replay that completed turn before this request, preserving its
            # call/result sequence without admitting any later user's turn.
            stored_messages.sort(key=lambda item: (run_sources.get(item.runId, item.sequence), item.sequence))
        if since is not None:
            source_dates = {item.runId: item.createdAt.isoformat() for item in stored_messages if item.role == "user" and item.runId}
            stored_messages = [
                item for item in stored_messages
                if item.createdAt.isoformat() >= since and source_dates.get(item.runId, since) >= since
            ]
        messages: list[AgentMessage] = []
        index = 0
        while index < len(stored_messages):
            stored = stored_messages[index]
            if stored.messageType == "tool_call" and stored.status == "completed" and stored.toolCallId and stored.toolName:
                group: list[Message] = []
                group_run_id = stored.runId
                group_id = stored.metadata.get("toolCallGroupId")
                cursor = index
                seen_call_ids: set[str] = set()
                while cursor < len(stored_messages):
                    candidate = stored_messages[cursor]
                    if (
                        candidate.messageType != "tool_call"
                        or candidate.status != "completed"
                        or not candidate.toolCallId
                        or not candidate.toolName
                        or candidate.runId != group_run_id
                        or (group_id is not None and candidate.metadata.get("toolCallGroupId") != group_id)
                    ):
                        break
                    if candidate.toolCallId not in seen_call_ids:
                        group.append(candidate)
                        seen_call_ids.add(candidate.toolCallId)
                    cursor += 1
                calls = tuple(
                    ToolCall(item.toolCallId or "", item.toolName or "", item.toolArguments or {})
                    for item in group
                )
                if calls:
                    raw_stop_reason = str(group[0].metadata.get("stopReason") or "tool_use")
                    if raw_stop_reason not in {"stop", "tool_use", "error", "aborted"}:
                        raw_stop_reason = "tool_use"
                    stop_reason = cast(Literal["stop", "tool_use", "error", "aborted"], raw_stop_reason)
                    messages.append(AssistantMessage(
                        content=group[0].content,
                        tool_calls=calls,
                        stop_reason=stop_reason,
                        metadata=_runtime_assistant_metadata(group[0].metadata),
                        timestamp=_timestamp(group[0].metadata),
                        source_message_ids=tuple(item.id for item in group),
                    ))

                    # A push commits while its tool is executing, before the
                    # result row. Keep its SQLite sequence but replay it only
                    # after every result in this provider call group.
                    # Only matching IDs are replayable; duplicates and orphan results
                    # stay in SQLite for diagnostics but never poison provider context.
                    result_rows: dict[str, Message] = {}
                    independent_messages: list[Message] = []
                    result_cursor = cursor
                    while result_cursor < len(stored_messages):
                        result = stored_messages[result_cursor]
                        if result.messageType == "desktop_push" and result.role == "assistant" and result.status == "completed":
                            independent_messages.append(result)
                            result_cursor += 1
                            continue
                        if result.messageType != "tool_result":
                            break
                        if (
                            result.status == "completed"
                            and result.runId == group_run_id
                            and result.toolCallId in seen_call_ids
                            and result.toolCallId not in result_rows
                        ):
                            result_rows[result.toolCallId or ""] = result
                        result_cursor += 1
                    for call in calls:
                        replay_result = result_rows.get(call.id)
                        if replay_result is None:
                            messages.append(ToolResultMessage(
                                tool_call_id=call.id,
                                tool_name=call.name,
                                content="Tool call interrupted before a result was recorded",
                                is_error=True,
                                details={"repaired": True},
                                source_message_ids=tuple(item.id for item in group if item.toolCallId == call.id),
                            ))
                        else:
                            nested_calls = _nested_calls(replay_result.metadata)
                            messages.append(ToolResultMessage(
                                tool_call_id=call.id,
                                tool_name=call.name,
                                content=replay_result.content,
                                is_error=replay_result.isError,
                                details=replay_result.toolResult,
                                usage=replay_result.metadata.get("usage"),
                                nested_calls=nested_calls,
                                timestamp=_timestamp(replay_result.metadata),
                                # Images are intentionally ephemeral and are not
                                # reconstructed from SQLite on later runs.
                                images=(),
                                source_message_ids=(replay_result.id,),
                            ))
                    messages.extend(_runtime_assistant_message(item) for item in independent_messages)
                    index = result_cursor
                    continue
            elif stored.messageType == "tool_result":
                # A result without a replayable assistant call is an orphan.
                index += 1
                continue
            elif stored.messageType in {"text", "internal_completion", "desktop_push"} and stored.status == "completed":
                if stored.role == "user":
                    messages.append(UserMessage(
                        _channel_user_content(stored),
                        attachments=load_runtime_attachments(stored.metadata),
                        timestamp=_timestamp(stored.metadata),
                        source_message_ids=(stored.id,),
                    ))
                elif stored.role == "assistant":
                    if stored.metadata.get("channel") not in {None, "", "desktop"} and stored.metadata.get("deliveryStatus") != "sent":
                        index += 1
                        continue
                    messages.append(_runtime_assistant_message(stored))
            index += 1
        if max_messages is None or max_messages < 1 or len(messages) <= max_messages:
            return messages

        # Keep complete user turns, including each assistant tool-call group
        # and its results, when limiting provider context.
        turns: list[list[AgentMessage]] = []
        current: list[AgentMessage] = []
        for message in messages:
            if isinstance(message, UserMessage) and current:
                turns.append(current)
                current = []
            current.append(message)
        if current:
            turns.append(current)
        selected: list[list[AgentMessage]] = []
        selected_count = 0
        for turn in reversed(turns):
            if selected and selected_count + len(turn) > max_messages:
                break
            selected.append(turn)
            selected_count += len(turn)
        return [message for turn in reversed(selected) for message in turn]

    def create_run_with_messages(
        self,
        run: AgentRun,
        user_content: str,
        *,
        assistant_status: str = "streaming",
        attachments: list[dict[str, object]] | None = None,
        received_at: float | None = None,
    ) -> tuple[AgentRun, Message, Message]:
        """Atomically create a run, its user message, and stream placeholder."""

        now = _now()
        user_id = f"{run.sessionKey}:{uuid.uuid4().hex}"
        assistant_id = f"{run.sessionKey}:{uuid.uuid4().hex}"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO agent_runs (run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run.runId, run.roleId, run.sessionKey, run.status, run.modelConfigurationId, json.dumps(run.modelSnapshot, ensure_ascii=False), run.turnCount, run.startedAt.isoformat(), run.endedAt.isoformat() if run.endedAt else None, run.cancelReason, run.error),
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_key = ?",
                (run.sessionKey,),
            ).fetchone()
            user_sequence = int(row[0])
            assistant_sequence = user_sequence + 1
            connection.execute(
                "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at, message_type, run_id, metadata_json) VALUES (?, ?, ?, 'user', ?, 'completed', ?, 'text', ?, ?)",
                (user_id, run.sessionKey, user_sequence, user_content, now, run.runId, _dump({"attachments": attachments or [], **({'hostReceivedAt': received_at} if received_at is not None else {})})),
            )
            connection.execute(
                "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at, message_type, run_id, metadata_json) VALUES (?, ?, ?, 'assistant', '', ?, ?, 'text', ?, ?)",
                (assistant_id, run.sessionKey, assistant_sequence, assistant_status, now, run.runId, _dump({"runtimePlaceholder": True})),
            )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE session_key = ?", (now, run.sessionKey))
        user_message = Message(id=user_id, sessionKey=run.sessionKey, sequence=user_sequence, role="user", content=user_content, status="completed", createdAt=datetime.fromisoformat(now), runId=run.runId, metadata={"attachments": attachments or [], **({'hostReceivedAt': received_at} if received_at is not None else {})})
        assistant_message = Message(id=assistant_id, sessionKey=run.sessionKey, sequence=assistant_sequence, role="assistant", content="", status=assistant_status, createdAt=datetime.fromisoformat(now), runId=run.runId, metadata={"runtimePlaceholder": True})
        return run, user_message, assistant_message

    def create_run_with_existing_user_message(
        self,
        run: AgentRun,
        message_id: str,
        *,
        assistant_status: str = "streaming",
    ) -> tuple[AgentRun, Message, Message]:
        """Reserve a run for an already persisted external inbound message.

        Channel intake commits its durable event before dispatching a model
        turn. Reusing that row avoids duplicating the user's message while
        still giving RuntimeManager the same atomic run/placeholder boundary
        as the desktop path.
        """

        now = _now()
        assistant_id = f"{run.sessionKey}:{uuid.uuid4().hex}"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO agent_runs (run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run.runId, run.roleId, run.sessionKey, run.status, run.modelConfigurationId, json.dumps(run.modelSnapshot, ensure_ascii=False), run.turnCount, run.startedAt.isoformat(), run.endedAt.isoformat() if run.endedAt else None, run.cancelReason, run.error),
            )
            source = connection.execute(
                "SELECT message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, is_error, run_id, metadata_json FROM messages WHERE message_id=? AND session_key=?",
                (message_id, run.sessionKey),
            ).fetchone()
            if source is None or source[3] != "user" or source[5] != "completed":
                raise KeyError(message_id)
            connection.execute("UPDATE messages SET run_id=? WHERE message_id=?", (run.runId, message_id))
            assistant_sequence = int(source[2]) + 1
            next_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_key=?",
                (run.sessionKey,),
            ).fetchone()[0]
            assistant_sequence = max(assistant_sequence, int(next_sequence))
            connection.execute(
                "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at, message_type, run_id, metadata_json) VALUES (?, ?, ?, 'assistant', '', ?, ?, 'text', ?, ?)",
                (assistant_id, run.sessionKey, assistant_sequence, assistant_status, now, run.runId, _dump({"runtimePlaceholder": True})),
            )
            connection.execute("UPDATE sessions SET updated_at=? WHERE session_key=?", (now, run.sessionKey))
        user_message = self._message(source)
        user_message = user_message.model_copy(update={"runId": run.runId})
        assistant_message = Message(
            id=assistant_id,
            sessionKey=run.sessionKey,
            sequence=assistant_sequence,
            role="assistant",
            content="",
            status=assistant_status,
            createdAt=datetime.fromisoformat(now),
            runId=run.runId,
            metadata={"runtimePlaceholder": True},
        )
        return run, user_message, assistant_message

    def delete_role_session(self, role_id: str) -> None:
        session_key = f"role:{role_id}"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
            connection.execute("DELETE FROM proactive_messages WHERE message_id NOT IN (SELECT message_id FROM messages)")
            connection.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
            connection.execute("DELETE FROM agent_runs WHERE role_id = ?", (role_id,))
            connection.execute("DELETE FROM tool_audits WHERE role_id = ?", (role_id,))

    def start_tool_audit(
        self,
        *,
        run_id: str,
        role_id: str,
        tool_name: str,
        call_id: str,
        argument_summary: dict[str, object],
        snapshot_id: str | None = None,
        tool_source: str | None = None,
        tool_version: str | None = None,
        policy_decision: str | None = None,
    ) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO tool_audits (audit_id, run_id, role_id, tool_name, call_id, argument_summary_json, snapshot_id, tool_source, tool_version, policy_decision, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{run_id}:{call_id}",
                    run_id,
                    role_id,
                    tool_name,
                    call_id,
                    _dump(argument_summary),
                    snapshot_id,
                    tool_source,
                    tool_version,
                    policy_decision,
                    _now(),
                ),
            )

    def finish_tool_audit(
        self,
        *,
        run_id: str,
        call_id: str,
        result_category: str,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE tool_audits SET ended_at=?, result_category=?, error_type=?, error_message=? WHERE audit_id=?",
                (_now(), result_category, error_type, error_message, f"{run_id}:{call_id}"),
            )

    def finish_open_tool_audits(
        self,
        run_id: str,
        *,
        result_category: str,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Close audits that did not emit a tool-end event before interruption."""

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE tool_audits SET ended_at=?, result_category=?, error_type=?, error_message=? WHERE run_id=? AND ended_at IS NULL",
                (_now(), result_category, error_type, error_message, run_id),
            )

    def list_tool_audits(self, run_id: str) -> list[dict[str, object]]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT audit_id, run_id, role_id, tool_name, call_id, argument_summary_json, snapshot_id, tool_source, tool_version, policy_decision, started_at, ended_at, result_category, error_type, error_message FROM tool_audits WHERE run_id=? ORDER BY started_at, audit_id",
                (run_id,),
            ).fetchall()
        return [
            {
                "auditId": str(row[0]),
                "runId": str(row[1]),
                "roleId": str(row[2]),
                "toolName": str(row[3]),
                "callId": str(row[4]),
                "argumentSummary": _load_dict(row[5]) or {},
                "snapshotId": str(row[6]) if row[6] is not None else None,
                "toolSource": str(row[7]) if row[7] is not None else None,
                "toolVersion": str(row[8]) if row[8] is not None else None,
                "policyDecision": str(row[9]) if row[9] is not None else None,
                "startedAt": str(row[10]),
                "endedAt": str(row[11]) if row[11] is not None else None,
                "resultCategory": str(row[12]) if row[12] is not None else None,
                "errorType": str(row[13]) if row[13] is not None else None,
                "errorMessage": str(row[14]) if row[14] is not None else None,
            }
            for row in rows
        ]

    def snapshot_tool_audits(self, role_id: str) -> list[tuple[object, ...]]:
        with sqlite3.connect(self.database_path) as connection:
            return connection.execute(
                "SELECT audit_id, run_id, role_id, tool_name, call_id, argument_summary_json, snapshot_id, tool_source, tool_version, policy_decision, started_at, ended_at, result_category, error_type, error_message FROM tool_audits WHERE role_id = ?",
                (role_id,),
            ).fetchall()

    def restore_tool_audits(self, role_id: str, audits: list[tuple[object, ...]]) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM tool_audits WHERE role_id = ?", (role_id,))
            connection.executemany(
                "INSERT INTO tool_audits (audit_id, run_id, role_id, tool_name, call_id, argument_summary_json, snapshot_id, tool_source, tool_version, policy_decision, started_at, ended_at, result_category, error_type, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                audits,
            )

    def snapshot_role_session(self, role_id: str) -> tuple[tuple[object, ...] | None, list[tuple[object, ...]], list[tuple[object, ...]]]:
        session_key = f"role:{role_id}"
        with sqlite3.connect(self.database_path) as connection:
            session = connection.execute(
                "SELECT session_key, role_id, created_at, updated_at FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            messages = connection.execute(
                "SELECT message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, is_error, run_id, metadata_json FROM messages WHERE session_key = ?",
                (session_key,),
            ).fetchall()
            runs = connection.execute(
                "SELECT run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error FROM agent_runs WHERE role_id = ?",
                (role_id,),
            ).fetchall()
        return session, messages, runs

    def restore_role_session(
        self,
        role_id: str,
        snapshot: tuple[tuple[object, ...] | None, list[tuple[object, ...]], list[tuple[object, ...]]],
    ) -> None:
        session, messages, runs = snapshot
        session_key = f"role:{role_id}"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
            connection.execute("DELETE FROM proactive_messages WHERE message_id NOT IN (SELECT message_id FROM messages)")
            connection.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
            if session is not None:
                connection.execute(
                    "INSERT INTO sessions (session_key, role_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    session,
                )
            connection.executemany(
                "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, is_error, run_id, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                messages,
            )
            connection.execute("DELETE FROM agent_runs WHERE role_id = ?", (role_id,))
            connection.executemany(
                "INSERT INTO agent_runs (run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                runs,
            )

    def _session(self, row: tuple[object, ...]) -> SessionSummary:
        return SessionSummary(sessionKey=str(row[0]), roleId=str(row[1]), createdAt=datetime.fromisoformat(str(row[2])), updatedAt=datetime.fromisoformat(str(row[3])))

    def _message(self, row: tuple[object, ...]) -> Message:
        return Message(
            id=str(row[0]), sessionKey=str(row[1]), sequence=int(str(row[2])), role=str(row[3]), content=str(row[4]),
            status=str(row[5]), createdAt=datetime.fromisoformat(str(row[6])), messageType=str(row[7] or "text"),
            toolCallId=str(row[8]) if row[8] is not None else None, toolName=str(row[9]) if row[9] is not None else None,
            toolArguments=_load_dict(row[10]), toolResult=_load(row[11]), isError=bool(row[12]),
            runId=str(row[13]) if row[13] is not None else None, metadata=_load_dict(row[14]) or {},
        )

    def create_run(self, run: AgentRun) -> AgentRun:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO agent_runs (run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run.runId, run.roleId, run.sessionKey, run.status, run.modelConfigurationId, json.dumps(run.modelSnapshot, ensure_ascii=False), run.turnCount, run.startedAt.isoformat(), run.endedAt.isoformat() if run.endedAt else None, run.cancelReason, run.error),
            )
        return run

    def latest_capability_revision(self, role_id: str, session_key: str) -> dict[str, Any] | None:
        """Read activation evidence only from a completed run of this origin."""
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("""SELECT model_snapshot_json FROM agent_runs WHERE role_id=? AND session_key=?
                AND status='completed' AND json_array_length(json_extract(model_snapshot_json,'$.capabilityRevisions'))>0
                ORDER BY started_at DESC LIMIT 1""", (role_id, session_key)).fetchone()
        return json.loads(row[0])["capabilityRevisions"][-1] if row is not None else None

    def capability_revision_for_call(self, run_id: str, call_id: str) -> dict[str, Any] | None:
        """Return the persisted provider version bound to a tool call."""
        run = self.get_run(run_id)
        if run is None:
            return None
        snapshot_id = run.modelSnapshot.get("capabilityCalls", {}).get(call_id)
        return next((item for item in run.modelSnapshot.get("capabilityRevisions", []) if item["snapshotId"] == snapshot_id), None)

    def record_capability_call(self, run_id: str, call_id: str, snapshot_id: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT model_snapshot_json FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            snapshot = json.loads(row[0])
            if not any(item["snapshotId"] == snapshot_id for item in snapshot.get("capabilityRevisions", [])):
                raise ValueError("tool call references an unknown capability revision")
            calls = snapshot.setdefault("capabilityCalls", {})
            if call_id in calls and calls[call_id] != snapshot_id:
                raise ValueError("tool call cannot change capability revision")
            calls[call_id] = snapshot_id
            connection.execute("UPDATE agent_runs SET model_snapshot_json=? WHERE run_id=?", (json.dumps(snapshot, ensure_ascii=False), run_id))

    def append_capability_revision(self, run_id: str, revision: dict[str, Any]) -> None:
        """Durably append one immutable visibility version before it is used."""
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT model_snapshot_json,role_id,session_key FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if (revision.get("runId"), revision.get("roleId"), revision.get("sessionKey")) != (run_id, row[1], row[2]):
                raise PermissionError("capability revision scope does not match its run")
            snapshot = json.loads(row[0])
            versions = snapshot.setdefault("capabilityRevisions", [])
            if any(item["snapshotId"] == revision["snapshotId"] for item in versions):
                if next(item for item in versions if item["snapshotId"] == revision["snapshotId"]) != revision:
                    raise ValueError("cannot overwrite an immutable capability revision")
                return
            expected_parent = versions[-1]["snapshotId"] if versions else None
            if revision.get("parentSnapshotId") != expected_parent or revision.get("visibilityVersion") != len(versions):
                raise ValueError("capability revision is out of sequence")
            versions.append(revision)
            connection.execute("UPDATE agent_runs SET model_snapshot_json=? WHERE run_id=?", (json.dumps(snapshot, ensure_ascii=False), run_id))

    def update_run(self, run_id: str, *, status: str, turn_count: int | None = None, ended_at: datetime | None = None, cancel_reason: str | None = None, error: str | None = None, model_snapshot: dict[str, Any] | None = None) -> AgentRun:
        with sqlite3.connect(self.database_path) as connection:
            current = connection.execute("SELECT role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if current is None:
                raise KeyError(run_id)
            current_status = str(current[2])
            terminal = {"completed", "failed", "cancelled", "max_turns"}
            if current_status in terminal:
                row = connection.execute("SELECT run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
                return self._run(row)
            if current_status == "created" and status not in {"created", "running", "failed", "cancelled"}:
                raise ValueError(f"invalid run transition: {current_status} -> {status}")
            if current_status == "running" and status not in {"running", *terminal}:
                raise ValueError(f"invalid run transition: {current_status} -> {status}")
            connection.execute("UPDATE agent_runs SET status=?, turn_count=COALESCE(?, turn_count), ended_at=COALESCE(?, ended_at), cancel_reason=COALESCE(?, cancel_reason), error=COALESCE(?, error), model_snapshot_json=COALESCE(?, model_snapshot_json) WHERE run_id=?", (status, turn_count, ended_at.isoformat() if ended_at else None, cancel_reason, error, json.dumps(model_snapshot, ensure_ascii=False) if model_snapshot is not None else None, run_id))
            row = connection.execute("SELECT run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run(row)

    def finish_run(
        self,
        run_id: str,
        *,
        status: Literal["completed", "failed", "cancelled", "max_turns"],
        assistant_message_id: str | None,
        assistant_content: str,
        turn_count: int,
        cancel_reason: str | None = None,
        error: str | None = None,
    ) -> tuple[AgentRun, Message]:
        """Commit the run and its user-visible assistant terminal state together."""

        now = _now()
        assistant_status = "completed" if status == "completed" else "failed"
        with sqlite3.connect(self.database_path) as connection:
            run_row = connection.execute(
                "SELECT run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            current = self._run(run_row)
            terminal = {"completed", "failed", "cancelled", "max_turns"}
            if current.status in terminal:
                existing = self._find_terminal_assistant(connection, run_id, assistant_message_id)
                if existing is None:
                    raise RuntimeError(f"terminal run {run_id} has no assistant message")
                return current, self._message(existing)

            if current.status not in {"created", "running"}:
                raise ValueError(f"invalid run transition: {current.status} -> {status}")
            connection.execute(
                "UPDATE agent_runs SET status = ?, turn_count = ?, ended_at = ?, cancel_reason = ?, error = ? WHERE run_id = ?",
                (status, turn_count, now, cancel_reason, error, run_id),
            )
            target = None
            if assistant_message_id is not None:
                target = connection.execute(
                    "SELECT message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, is_error, run_id, metadata_json FROM messages WHERE message_id = ? AND run_id = ?",
                    (assistant_message_id, run_id),
                ).fetchone()
            metadata: dict[str, object] = {"runtimeReason": status}
            if error:
                metadata["error"] = error
            if target is not None:
                previous_metadata = _load_dict(target[14]) or {}
                previous_metadata.update(metadata)
                connection.execute(
                    "UPDATE messages SET content = ?, status = ?, metadata_json = ? WHERE message_id = ?",
                    (assistant_content, assistant_status, _dump(previous_metadata), assistant_message_id),
                )
            else:
                sequence_row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_key = ?",
                    (current.sessionKey,),
                ).fetchone()
                message_id = f"{current.sessionKey}:{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at, message_type, run_id, metadata_json) VALUES (?, ?, ?, 'assistant', ?, ?, ?, 'text', ?, ?)",
                    (message_id, current.sessionKey, int(sequence_row[0]), assistant_content, assistant_status, now, run_id, _dump(metadata)),
                )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE session_key = ?", (now, current.sessionKey))
            run_row = connection.execute(
                "SELECT run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            message_row = self._find_terminal_assistant(connection, run_id, assistant_message_id)
        assert run_row is not None and message_row is not None
        return self._run(run_row), self._message(message_row)

    def _find_terminal_assistant(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        assistant_message_id: str | None,
    ) -> tuple[object, ...] | None:
        columns = "message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, tool_result_json, is_error, run_id, metadata_json"
        if assistant_message_id is not None:
            row = connection.execute(
                f"SELECT {columns} FROM messages WHERE message_id = ? AND run_id = ?",
                (assistant_message_id, run_id),
            ).fetchone()
            if row is not None:
                return row
        return connection.execute(
            f"SELECT {columns} FROM messages WHERE run_id = ? AND role = 'assistant' AND message_type = 'text' ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()

    def get_run(self, run_id: str) -> AgentRun | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run(row) if row else None

    def active_run(self, role_id: str) -> AgentRun | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT run_id, role_id, session_key, status, model_configuration_id, model_snapshot_json, turn_count, started_at, ended_at, cancel_reason, error FROM agent_runs WHERE role_id = ? AND status IN ('created', 'running') ORDER BY started_at DESC LIMIT 1", (role_id,)).fetchone()
        return self._run(row) if row else None

    def mark_running_runs_failed(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            now = _now()
            connection.execute("UPDATE messages SET status='failed' WHERE status='streaming' AND (run_id IS NULL OR run_id IN (SELECT run_id FROM agent_runs WHERE status IN ('created', 'running')))" )
            connection.execute("UPDATE agent_runs SET status='failed', ended_at=?, error=COALESCE(error, '服务重启时运行未完成') WHERE status IN ('created', 'running')", (now,))

    def _run(self, row: tuple[object, ...]) -> AgentRun:
        status = cast(Literal["created", "running", "completed", "failed", "cancelled", "max_turns"], str(row[3]))
        return AgentRun(runId=str(row[0]), roleId=str(row[1]), sessionKey=str(row[2]), status=status, modelConfigurationId=str(row[4]) if row[4] is not None else None, modelSnapshot=_load_dict(row[5]) or {}, turnCount=int(str(row[6])), startedAt=datetime.fromisoformat(str(row[7])), endedAt=datetime.fromisoformat(str(row[8])) if row[8] else None, cancelReason=str(row[9]) if row[9] else None, error=str(row[10]) if row[10] else None)


def _dump(value: object | None) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _timestamp(metadata: dict[str, Any]) -> int | None:
    value = metadata.get("timestamp")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nested_calls(metadata: dict[str, Any]) -> tuple[ToolCall, ...]:
    value = metadata.get("nestedCalls")
    if not isinstance(value, list):
        return ()
    calls: list[ToolCall] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        call_id = item.get("id")
        name = item.get("name")
        arguments = item.get("arguments", {})
        if (
            isinstance(call_id, str)
            and isinstance(name, str)
            and isinstance(arguments, dict)
        ):
            calls.append(ToolCall(call_id, name, dict(arguments)))
    return tuple(calls)


def _load(value: object | None) -> object | None:
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return None


def _load_dict(value: object | None) -> dict[str, Any] | None:
    loaded = _load(value)
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else None
