import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def resolve_owner_knowledge_database(project_root: str | Path, data_root: str | Path) -> Path:
    """Use a dedicated owner-knowledge directory, copying the legacy DB once."""
    project = Path(project_root).resolve()
    legacy_database = Path(data_root) / "owner-knowledge.db"
    configured_directory = os.getenv("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", "").strip()
    if configured_directory:
        directory = Path(configured_directory)
        if not directory.is_absolute():
            directory = project / directory
    else:
        directory = project / "owner-knowledge-data"
    database = directory / "owner-knowledge.db"

    if not database.exists() and legacy_database.exists():
        directory.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(legacy_database) as source, sqlite3.connect(database) as target:
                source.backup(target)
        except Exception:
            database.unlink(missing_ok=True)
            raise
    return database


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
                message_type TEXT NOT NULL DEFAULT 'text',
                tool_call_id TEXT,
                tool_name TEXT,
                tool_arguments_json TEXT,
                tool_result_json TEXT,
                is_error INTEGER NOT NULL DEFAULT 0,
                run_id TEXT,
                metadata_json TEXT,
                UNIQUE(session_key, sequence)
            )
        """)
        message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        message_additions = {
            "message_type": "TEXT NOT NULL DEFAULT 'text'",
            "tool_call_id": "TEXT",
            "tool_name": "TEXT",
            "tool_arguments_json": "TEXT",
            "tool_result_json": "TEXT",
            "is_error": "INTEGER NOT NULL DEFAULT 0",
            "run_id": "TEXT",
            "metadata_json": "TEXT",
        }
        for column, definition in message_additions.items():
            if column not in message_columns:
                connection.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS messages_run_id ON messages(run_id)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                status TEXT NOT NULL,
                model_configuration_id TEXT,
                model_snapshot_json TEXT NOT NULL,
                turn_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                cancel_reason TEXT,
                error TEXT
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS agent_runs_role_status ON agent_runs(role_id, status)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS tool_audits (
                audit_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                call_id TEXT NOT NULL,
                argument_summary_json TEXT NOT NULL,
                snapshot_id TEXT,
                tool_source TEXT,
                tool_version TEXT,
                policy_decision TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                result_category TEXT,
                error_type TEXT,
                error_message TEXT
            )
        """)
        audit_columns = {row[1] for row in connection.execute("PRAGMA table_info(tool_audits)")}
        audit_additions = {
            "snapshot_id": "TEXT",
            "tool_source": "TEXT",
            "tool_version": "TEXT",
            "policy_decision": "TEXT",
        }
        for column, definition in audit_additions.items():
            if column not in audit_columns:
                connection.execute(f"ALTER TABLE tool_audits ADD COLUMN {column} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS tool_audits_run_id ON tool_audits(run_id)")
        index_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='agent_runs_one_active_per_role'"
        ).fetchone()
        index_sql = str(index_row[0]).lower() if index_row and index_row[0] else ""
        if index_sql and not all(value in index_sql for value in ("created", "running")):
            # IF NOT EXISTS would retain the previous running-only predicate.
            connection.execute("DROP INDEX agent_runs_one_active_per_role")
            index_sql = ""
        if not index_sql:
            duplicate_roles = connection.execute(
                "SELECT role_id FROM agent_runs WHERE status IN ('created', 'running') GROUP BY role_id HAVING COUNT(*) > 1"
            ).fetchall()
            migration_now = datetime.now(timezone.utc).isoformat()
            for (role_id,) in duplicate_roles:
                active_rows = connection.execute(
                    "SELECT run_id FROM agent_runs WHERE role_id = ? AND status IN ('created', 'running') ORDER BY started_at DESC, run_id DESC",
                    (role_id,),
                ).fetchall()
                for (run_id,) in active_rows[1:]:
                    connection.execute(
                        "UPDATE agent_runs SET status='failed', ended_at=?, error=COALESCE(error, '数据库迁移时发现重复活动运行') WHERE run_id=?",
                        (migration_now, run_id),
                    )
                    connection.execute(
                        "UPDATE messages SET status='failed' WHERE run_id=? AND status='streaming'",
                        (run_id,),
                    )
            connection.execute(
                "CREATE UNIQUE INDEX agent_runs_one_active_per_role ON agent_runs(role_id) WHERE status IN ('created', 'running')"
            )
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
