import sqlite3
from pathlib import Path


def initialize_databases(data_dir: str | Path) -> None:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / "sessions.db") as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL,
                author TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TEXT NOT NULL
            )
        """)
    with sqlite3.connect(root / "memory2.db") as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS semantic_memory (
                id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
