import sqlite3
from pathlib import Path


def initialize_databases(data_dir: str | Path) -> None:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / "sessions.db") as connection:
        existing = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        if existing and "session_key" not in existing:
            connection.execute("ALTER TABLE messages RENAME TO messages_legacy_v1")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY,
                role_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS sessions_role_id_unique ON sessions(role_id)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TEXT NOT NULL,
                UNIQUE(session_key, sequence)
            )
        """)
        if "id" in existing:
            legacy_rows = connection.execute(
                "SELECT id, role_id, author, content, status, created_at FROM messages_legacy_v1 ORDER BY created_at, id"
            ).fetchall()
            sequences: dict[str, int] = {}
            for message_id, role_id, author, content, status, created_at in legacy_rows:
                session_key = f"role:{role_id}"
                connection.execute(
                    "INSERT OR IGNORE INTO sessions (session_key, role_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (session_key, role_id, created_at, created_at),
                )
                sequence = sequences.get(session_key, 0) + 1
                sequences[session_key] = sequence
                connection.execute(
                    "INSERT OR IGNORE INTO messages (message_id, session_key, sequence, role, content, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (message_id, session_key, sequence, "assistant" if author in ("assistant", "meido") else "user", content, status, created_at),
                )
            connection.execute("DROP TABLE messages_legacy_v1")
    with sqlite3.connect(root / "memory2.db") as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS semantic_memory (
                id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
