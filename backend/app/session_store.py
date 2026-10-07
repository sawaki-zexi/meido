import sqlite3
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from .agent_runtime.types import AgentMessage, AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from .models import AgentRun, Message, SessionSummary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            return [self.append_message(session_key, "user", message.content, status, run_id=run_id)]
        if isinstance(message, AssistantMessage):
            if not message.tool_calls:
                return [self.append_message(session_key, "assistant", message.content, status, run_id=run_id)]
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
                    message_id = f"{session_key}:{uuid.uuid4().hex}"
                    metadata = {
                        "stopReason": message.stop_reason,
                        "toolCallGroupId": group_id,
                        "toolCallIndex": index,
                        "toolCallCount": len(message.tool_calls),
                    }
                    connection.execute(
                        "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at, message_type, tool_call_id, tool_name, tool_arguments_json, run_id, metadata_json) VALUES (?, ?, ?, 'assistant', ?, ?, ?, 'tool_call', ?, ?, ?, ?, ?)",
                        (message_id, session_key, sequence, message.content, status, now, call.id, call.name, _dump(call.arguments), run_id, _dump(metadata)),
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
                        toolArguments=call.arguments,
                        runId=run_id,
                        metadata=metadata,
                    ))
                    sequence += 1
                connection.execute("UPDATE sessions SET updated_at = ? WHERE session_key = ?", (now, session_key))
            return persisted
        if isinstance(message, ToolResultMessage):
            return [
                self.append_message(
                    session_key,
                    "tool",
                    message.content,
                    status,
                    message_type="tool_result",
                    tool_call_id=message.tool_call_id,
                    tool_name=message.tool_name,
                    tool_result=message.details,
                    is_error=message.is_error,
                    run_id=run_id,
                )
            ]
        raise TypeError(f"unsupported runtime message: {type(message)!r}")

    def runtime_messages(self, session_key: str, *, max_messages: int | None = None) -> list[AgentMessage]:
        """Rebuild a provider transcript from the structured SQLite rows."""

        stored_messages = self.list_messages(session_key)
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
                    messages.append(AssistantMessage(content=group[0].content, tool_calls=calls, stop_reason=stop_reason))

                    # Results are expected immediately after the assistant call group.
                    # Only matching IDs are replayable; duplicates and orphan results
                    # stay in SQLite for diagnostics but never poison provider context.
                    result_rows: dict[str, Message] = {}
                    result_cursor = cursor
                    while result_cursor < len(stored_messages) and stored_messages[result_cursor].messageType == "tool_result":
                        result = stored_messages[result_cursor]
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
                            ))
                        else:
                            messages.append(ToolResultMessage(
                                tool_call_id=call.id,
                                tool_name=call.name,
                                content=replay_result.content,
                                is_error=replay_result.isError,
                                details=replay_result.toolResult,
                            ))
                    index = result_cursor
                    continue
            elif stored.messageType == "tool_result":
                # A result without a replayable assistant call is an orphan.
                index += 1
                continue
            elif stored.messageType == "text" and stored.status == "completed":
                if stored.role == "user":
                    messages.append(UserMessage(stored.content))
                elif stored.role == "assistant":
                    messages.append(AssistantMessage(stored.content))
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
                (user_id, run.sessionKey, user_sequence, user_content, now, run.runId, _dump({})),
            )
            connection.execute(
                "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at, message_type, run_id, metadata_json) VALUES (?, ?, ?, 'assistant', '', ?, ?, 'text', ?, ?)",
                (assistant_id, run.sessionKey, assistant_sequence, assistant_status, now, run.runId, _dump({"runtimePlaceholder": True})),
            )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE session_key = ?", (now, run.sessionKey))
        user_message = Message(id=user_id, sessionKey=run.sessionKey, sequence=user_sequence, role="user", content=user_content, status="completed", createdAt=datetime.fromisoformat(now), runId=run.runId)
        assistant_message = Message(id=assistant_id, sessionKey=run.sessionKey, sequence=assistant_sequence, role="assistant", content="", status=assistant_status, createdAt=datetime.fromisoformat(now), runId=run.runId, metadata={"runtimePlaceholder": True})
        return run, user_message, assistant_message

    def delete_role_session(self, role_id: str) -> None:
        session_key = f"role:{role_id}"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
            connection.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
            connection.execute("DELETE FROM agent_runs WHERE role_id = ?", (role_id,))

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
