import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Message, SessionSummary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        with sqlite3.connect(self.database_path) as connection:
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
                "SELECT message_id, session_key, sequence, role, content, status, created_at FROM messages WHERE session_key = ? ORDER BY sequence ASC",
                (session_key,),
            ).fetchall()
        return [self._message(row) for row in rows]

    def context_messages(self, session_key: str) -> list[Message]:
        return [message for message in self.list_messages(session_key) if message.role == "user" or message.status == "completed"]

    def append_message(self, session_key: str, role: str, content: str, status: str = "completed") -> Message:
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            sequence = int(row[0])
            message_id = f"{session_key}:{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO messages (message_id, session_key, sequence, role, content, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, session_key, sequence, role, content, status, now),
            )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE session_key = ?", (now, session_key))
        return Message(id=message_id, sessionKey=session_key, sequence=sequence, role=role, content=content, status=status, createdAt=datetime.fromisoformat(now))

    def update_message(self, message_id: str, content: str, status: str) -> Message:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("UPDATE messages SET content = ?, status = ? WHERE message_id = ?", (content, status, message_id))
            row = connection.execute(
                "SELECT message_id, session_key, sequence, role, content, status, created_at FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return self._message(row)

    def delete_role_session(self, role_id: str) -> None:
        session_key = f"role:{role_id}"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
            connection.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))

    def _session(self, row: tuple[object, ...]) -> SessionSummary:
        return SessionSummary(sessionKey=str(row[0]), roleId=str(row[1]), createdAt=datetime.fromisoformat(str(row[2])), updatedAt=datetime.fromisoformat(str(row[3])))

    def _message(self, row: tuple[object, ...]) -> Message:
        return Message(id=str(row[0]), sessionKey=str(row[1]), sequence=int(row[2]), role=str(row[3]), content=str(row[4]), status=str(row[5]), createdAt=datetime.fromisoformat(str(row[6])))
