from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol


TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out", "interrupted"})


@dataclass(frozen=True)
class TaskOrigin:
    role_id: str
    session_key: str
    parent_run_id: str = ""
    tool_call_id: str | None = None
    capability_version: str = ""
    source_thread_id: str = ""
    source_account_id: str = ""
    source_scope_id: str = ""
    source_authority_json: str = ""

    def scope(self) -> tuple[str, ...]:
        return self.role_id, self.session_key, self.source_thread_id, self.source_account_id, self.source_scope_id

    def to_dict(self) -> dict[str, object]:
        return {
            "roleId": self.role_id, "sessionKey": self.session_key,
            "parentRunId": self.parent_run_id, "toolCallId": self.tool_call_id,
            "capabilityVersion": self.capability_version,
            "sourceThreadId": self.source_thread_id, "sourceAccountId": self.source_account_id,
            "sourceScopeId": self.source_scope_id,
            "sourceAuthority": json.loads(self.source_authority_json) if self.source_authority_json else None,
        }


@dataclass(frozen=True)
class TaskBudget:
    timeout_seconds: float = 30
    output_chars: int = 20_000
    log_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class TaskOutcome:
    status: str
    exit_code: int | None = None
    output_tail: str | None = None


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    origin: TaskOrigin
    kind: str
    description: str
    status: str
    budget: TaskBudget
    started_at: float
    ended_at: float | None
    exit_code: int | None
    output: str
    output_chars: int
    log_path: str
    log_bytes: int
    log_truncated: bool
    progress: str
    log_error: str = ""

    def details(self) -> dict[str, object]:
        return {
            "taskId": self.task_id, "status": self.status, "exitCode": self.exit_code,
            "origin": self.origin.to_dict(), "kind": self.kind, "description": self.description,
            "timeoutSeconds": self.budget.timeout_seconds,
            "durationSeconds": round((self.ended_at or time.time()) - self.started_at, 3),
            "truncated": self.output_chars > self.budget.output_chars,
            "outputChars": self.output_chars, "logPath": self.log_path,
            "logBytes": self.log_bytes, "logComplete": not self.log_truncated and not self.log_error,
            "logTruncated": self.log_truncated, "logError": self.log_error or None, "progress": self.progress,
        }


class TaskExecutor(Protocol):
    """An owned execution must stop its resources when cancel() is called."""

    def run(self, execution: TaskExecution) -> TaskOutcome: ...
    def cancel(self) -> None: ...


@dataclass
class _Active:
    executor: TaskExecutor
    authorized: Callable[[], bool] | None = None
    done: threading.Event = field(default_factory=threading.Event)
    cancel_reason: str | None = None
    thread: threading.Thread | None = None


class TaskExecution:
    """Executor context: bounded log/progress writes and a frozen deadline."""

    def __init__(self, runtime: TaskRuntime, task_id: str, budget: TaskBudget):
        self._runtime = runtime
        self.task_id = task_id
        self.budget = budget
        self.deadline = time.monotonic() + budget.timeout_seconds

    def write(self, text: str) -> None:
        self._runtime._append(self.task_id, text)

    def progress(self, text: str) -> None:
        self._runtime._update(self.task_id, progress=text[:1000])

    def directory(self) -> Path:
        """Allocate a host-chosen working directory for this execution."""
        directory = self._runtime.root / "workspaces" / self.task_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory


class TaskRuntime:
    """Own persistence, execution, budgets and cancellation beyond a parent run.

    Executors own their external resources; the runtime calls cancel on timeout,
    shutdown and explicit stop. Threads and logs belong here, never to AgentLoop.
    Only one application instance may own a given data root at a time.
    """

    def __init__(
        self, root: Path, *, max_concurrent: int = 8, max_per_role: int = 3,
        max_timeout_seconds: float = 600, max_log_bytes: int = 4 * 1024 * 1024,
        retention_seconds: float = 86400, max_retained_per_role: int = 100,
        max_role_log_bytes: int = 32 * 1024 * 1024,
    ):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_concurrent, self.max_per_role = max_concurrent, max_per_role
        self.max_timeout_seconds, self.max_log_bytes = max_timeout_seconds, max_log_bytes
        self.retention_seconds, self.max_retained_per_role = retention_seconds, max_retained_per_role
        self.max_role_log_bytes = max_role_log_bytes
        self._lock = threading.RLock()
        self._active: dict[str, _Active] = {}
        self._disabled_roles: set[str] = set()
        self._closed = False
        self._db = sqlite3.connect(self.root / "tasks.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, origin TEXT NOT NULL, role_id TEXT NOT NULL,
            kind TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL,
            budget TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL,
            exit_code INTEGER, output TEXT NOT NULL DEFAULT '', output_chars INTEGER NOT NULL DEFAULT 0,
            log_path TEXT NOT NULL, log_bytes INTEGER NOT NULL DEFAULT 0,
            log_truncated INTEGER NOT NULL DEFAULT 0, progress TEXT NOT NULL DEFAULT '',
            log_error TEXT NOT NULL DEFAULT '')""")
        if "log_error" not in {row[1] for row in self._db.execute("PRAGMA table_info(tasks)")}:
            self._db.execute("ALTER TABLE tasks ADD COLUMN log_error TEXT NOT NULL DEFAULT ''")
        if "completion" not in {row[1] for row in self._db.execute("PRAGMA table_info(tasks)")}:
            self._db.execute("ALTER TABLE tasks ADD COLUMN completion INTEGER NOT NULL DEFAULT 0")
        self._db.execute("""CREATE TABLE IF NOT EXISTS task_completions (
            task_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
            content TEXT NOT NULL DEFAULT '', message_id TEXT, delivery_id TEXT)""")
        if "error_type" not in {row[1] for row in self._db.execute("PRAGMA table_info(task_completions)")}:
            self._db.execute("ALTER TABLE task_completions ADD COLUMN error_type TEXT NOT NULL DEFAULT ''")
        # An interrupted external send may already have reached the platform.
        self._db.execute("UPDATE task_completions SET status='uncertain' WHERE status='sending'")
        self._db.execute(
            "UPDATE tasks SET status='interrupted', ended_at=? WHERE status IN ('queued','running')", (time.time(),)
        )
        self._db.commit()
        self._db.execute("INSERT OR IGNORE INTO task_completions(task_id) SELECT task_id FROM tasks WHERE completion=1 AND ended_at IS NOT NULL")
        self._db.commit()
        self._prune()

    def submit(self, origin: TaskOrigin, executor: TaskExecutor, *, budget: TaskBudget, kind: str, description: str = "",
               authorized: Callable[[], bool] | None = None, completion: bool = False) -> str:
        if not origin.role_id or not origin.session_key:
            raise ValueError("task owner and original context are required")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", kind):
            raise ValueError("invalid host execution kind")
        if not 0 < budget.timeout_seconds <= self.max_timeout_seconds:
            raise ValueError("task timeout exceeds host budget")
        if not 0 < budget.log_bytes <= self.max_log_bytes or not 0 < budget.output_chars <= 200_000:
            raise ValueError("task output exceeds host budget")
        with self._lock:
            if self._closed or origin.role_id in self._disabled_roles or (authorized is not None and not authorized()):
                raise PermissionError("task execution is disabled")
            if len(self._active) >= self.max_concurrent or sum(
                self._snapshot(key).origin.role_id == origin.role_id for key in self._active
            ) >= self.max_per_role:
                raise ValueError("task concurrency budget reached")
            reserved = sum(self._snapshot(key).budget.log_bytes for key in self._active
                           if self._snapshot(key).origin.role_id == origin.role_id)
            if reserved + budget.log_bytes > self.max_role_log_bytes:
                raise ValueError("role task log budget reached")
            self._prune(reserve_role=origin.role_id, reserve_bytes=budget.log_bytes)
            retained = self._db.execute("SELECT budget,log_bytes,ended_at FROM tasks WHERE role_id=?", (origin.role_id,)).fetchall()
            retained_bytes = sum(json.loads(row["budget"])["log_bytes"] if row["ended_at"] is None else row["log_bytes"] for row in retained)
            if len(retained) >= self.max_retained_per_role or retained_bytes + budget.log_bytes > self.max_role_log_bytes:
                raise ValueError("role task retention budget is occupied by undelivered completions")
            task_id = f"{kind}_{uuid.uuid4().hex}"
            log_dir = self.root / "logs" / hashlib.sha256(origin.role_id.encode()).hexdigest()
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{task_id}.log"
            log_path.touch()
            self._db.execute(
                "INSERT INTO tasks (task_id,origin,role_id,kind,description,status,budget,started_at,log_path,completion) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, json.dumps(asdict(origin)), origin.role_id, kind, description[:500], "queued",
                 json.dumps(asdict(budget)), time.time(), str(log_path), int(completion)),
            )
            self._db.commit()
            active = _Active(executor, authorized)
            self._active[task_id] = active
            active.thread = threading.Thread(target=self._run, args=(task_id, active, budget), daemon=True)
            active.thread.start()
            return task_id

    def list(self, origin: TaskOrigin, *, kind: str) -> tuple[TaskSnapshot, ...]:
        """Observe only executions in the caller's original source scope."""
        with self._lock:
            self._prune()
            items = []
            for row in self._db.execute("SELECT task_id FROM tasks WHERE role_id=? AND kind=? ORDER BY started_at DESC LIMIT 100",
                                        (origin.role_id, kind)).fetchall():
                try:
                    self._authorize(row[0], origin)
                except KeyError:
                    continue
                items.append(self._snapshot(row[0]))
            return tuple(items)

    def pending_completions(self) -> tuple[TaskSnapshot, ...]:
        """Application worker boundary, never exposed directly to agent tools."""
        with self._lock:
            return tuple(self._snapshot(row[0]) for row in self._db.execute(
                "SELECT c.task_id FROM task_completions c JOIN tasks t ON t.task_id=c.task_id WHERE c.status='pending' ORDER BY t.started_at LIMIT 100"
            ).fetchall())

    def uncertain_completions(self) -> tuple[TaskSnapshot, ...]:
        """Recover local message status after a crash inside external delivery."""
        with self._lock:
            return tuple(self._snapshot(row[0]) for row in self._db.execute(
                "SELECT c.task_id FROM task_completions c JOIN tasks t ON t.task_id=c.task_id WHERE c.status='uncertain' ORDER BY t.started_at LIMIT 100"
            ).fetchall())

    def completion(self, task_id: str, origin: TaskOrigin) -> dict[str, object] | None:
        with self._lock:
            self._authorize(task_id, origin)
            row = self._db.execute("SELECT * FROM task_completions WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def update_completion(self, task_id: str, *, status: str, content: str = "", message_id: str | None = None,
                          delivery_id: str | None = None, error_type: str = "") -> None:
        if status not in {"pending", "sending", "sent", "failed", "uncertain"}:
            raise ValueError("invalid completion status")
        with self._lock:
            self._db.execute("UPDATE task_completions SET status=?,content=?,message_id=COALESCE(?,message_id),delivery_id=COALESCE(?,delivery_id),error_type=? WHERE task_id=?",
                             (status, content[:100000], message_id, delivery_id, error_type[:100], task_id))
            self._db.commit()

    def query(self, task_id: str, origin: TaskOrigin, *, wait_seconds: float = 0) -> TaskSnapshot:
        with self._lock:
            self._prune()
            self._authorize(task_id, origin)
            active = self._active.get(task_id)
        if active is not None and wait_seconds:
            active.done.wait(min(max(wait_seconds, 0), 30))
        with self._lock:
            self._authorize(task_id, origin)
            return self._snapshot(task_id)

    def stop(self, task_id: str, origin: TaskOrigin) -> TaskSnapshot:
        with self._lock:
            self._authorize(task_id, origin)
        self._cancel(task_id, "cancelled")
        return self.query(task_id, origin)

    def set_role_enabled(self, role_id: str, enabled: bool) -> None:
        with self._lock:
            if enabled:
                self._disabled_roles.discard(role_id)
                return
            self._disabled_roles.add(role_id)
            task_ids = [key for key in self._active if self._snapshot(key).origin.role_id == role_id]
        for key in task_ids:
            self._cancel(key, "cancelled")

    def cancel_role_tasks(self, role_id: str, *, kind: str | None = None) -> None:
        with self._lock:
            task_ids = [key for key in self._active if self._snapshot(key).origin.role_id == role_id
                        and (kind is None or self._snapshot(key).kind == kind)]
        for key in task_ids:
            self._cancel(key, "cancelled")

    def start(self) -> None:
        """Reopen admission after a graceful service restart; never rerun tasks."""
        with self._lock:
            self._closed = False

    def delete_role(self, role_id: str) -> None:
        self.set_role_enabled(role_id, False)
        with self._lock:
            for row in self._db.execute("SELECT task_id,log_path FROM tasks WHERE role_id=?", (role_id,)).fetchall():
                self._remove_log(Path(row[1]))
                self._remove_directory(row[0])
            self._db.execute("DELETE FROM tasks WHERE role_id=?", (role_id,))
            self._db.execute("DELETE FROM task_completions WHERE task_id NOT IN (SELECT task_id FROM tasks)")
            self._db.commit()

    def reconcile_roles(self, available_roles: set[str]) -> None:
        """Remove orphaned records/logs after an interrupted role deletion."""
        with self._lock:
            role_ids = {row[0] for row in self._db.execute("SELECT DISTINCT role_id FROM tasks").fetchall()}
        for role_id in role_ids - available_roles:
            self.delete_role(role_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            task_ids = list(self._active)
        for key in task_ids:
            self._cancel(key, "interrupted")

    def _run(self, task_id: str, active: _Active, budget: TaskBudget) -> None:
        execution = TaskExecution(self, task_id, budget)

        def watch_budget_and_authorization():
            while not active.done.wait(min(.05, max(0, execution.deadline - time.monotonic()))):
                try:
                    allowed = active.authorized is None or active.authorized()
                except Exception:
                    allowed = False
                if not allowed or time.monotonic() >= execution.deadline:
                    self._cancel(task_id, "timed_out" if allowed else "cancelled")
                    return

        try:
            self._update(task_id, status="running")
            threading.Thread(target=watch_budget_and_authorization, daemon=True).start()
            outcome = active.executor.run(execution)
            if outcome.status not in TERMINAL:
                raise ValueError("executor returned a nonterminal outcome")
        except Exception as error:
            active.executor.cancel()
            # Exception messages may contain command arguments or credentials.
            # Persist the error independently: the log path itself may be the
            # failed resource, and retrying it would lose the terminal state.
            self._update(task_id, progress=f"executor failed: {type(error).__name__}")
            outcome = TaskOutcome("failed")
        with self._lock:
            output = outcome.output_tail if outcome.output_tail is not None else self._snapshot(task_id).output
            self._update(task_id, status=active.cancel_reason or outcome.status, exit_code=outcome.exit_code,
                         ended_at=time.time(), output=output[-budget.output_chars:])
            self._db.execute("INSERT OR IGNORE INTO task_completions(task_id) SELECT task_id FROM tasks WHERE task_id=? AND completion=1", (task_id,))
            self._db.commit()
            self._active.pop(task_id, None)
            active.done.set()
            self._prune()

    def _cancel(self, task_id: str, reason: str) -> None:
        with self._lock:
            active = self._active.get(task_id)
            if active is None:
                return
            active.cancel_reason = active.cancel_reason or reason
        active.executor.cancel()
        if active.thread is not threading.current_thread() and not active.done.wait(10):
            raise TimeoutError("task executor did not release its resources")

    def _authorize(self, task_id: str, origin: TaskOrigin) -> None:
        row = self._db.execute("SELECT origin FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or TaskOrigin(**json.loads(row[0])).scope() != origin.scope() or origin.role_id in self._disabled_roles:
            raise KeyError("task not found in this role and original context")

    def _snapshot(self, task_id: str) -> TaskSnapshot:
        row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskSnapshot(
            task_id, TaskOrigin(**json.loads(row["origin"])), row["kind"], row["description"], row["status"],
            TaskBudget(**json.loads(row["budget"])), row["started_at"], row["ended_at"], row["exit_code"],
            row["output"], row["output_chars"], row["log_path"], row["log_bytes"], bool(row["log_truncated"]), row["progress"], row["log_error"],
        )

    def _append(self, task_id: str, text: str) -> None:
        with self._lock:
            snapshot = self._snapshot(task_id)
            raw = text.encode("utf-8")
            remaining = snapshot.budget.log_bytes - snapshot.log_bytes
            kept = raw[:remaining].decode("utf-8", errors="ignore").encode("utf-8")
            try:
                with Path(snapshot.log_path).open("ab") as log:
                    log.write(kept)
            except OSError as error:
                self._update(task_id, output=(snapshot.output + text)[-snapshot.budget.output_chars:],
                             output_chars=snapshot.output_chars + len(text), log_truncated=1,
                             log_error=type(error).__name__,
                             progress=f"log write failed: {type(error).__name__}")
                raise
            self._update(task_id, output=(snapshot.output + text)[-snapshot.budget.output_chars:],
                         output_chars=snapshot.output_chars + len(text), log_bytes=snapshot.log_bytes + len(kept),
                         log_truncated=int(snapshot.log_truncated or len(kept) < len(raw)))

    def _update(self, task_id: str, **values: object) -> None:
        with self._lock:
            columns = ",".join(f"{key}=?" for key in values)
            self._db.execute(f"UPDATE tasks SET {columns} WHERE task_id=?", (*values.values(), task_id))
            self._db.commit()

    def _prune(self, *, reserve_role: str = "", reserve_bytes: int = 0) -> None:
        with self._lock:
            rows = self._db.execute("SELECT task_id,role_id,log_path,log_bytes,budget,ended_at FROM tasks ORDER BY started_at DESC").fetchall()
            counts: dict[str, int] = {reserve_role: 1} if reserve_role else {}
            sizes: dict[str, int] = {reserve_role: reserve_bytes} if reserve_role else {}
            for row in rows:
                role = row["role_id"]
                counts[role] = counts.get(role, 0) + 1
                sizes[role] = sizes.get(role, 0) + (json.loads(row["budget"])["log_bytes"]
                                                  if row["ended_at"] is None else row["log_bytes"])
                pending = self._db.execute("SELECT status FROM task_completions WHERE task_id=?", (row["task_id"],)).fetchone()
                if pending is not None and pending[0] in {"pending", "sending"}:
                    continue
                if row["ended_at"] is not None and (
                    time.time() - row["ended_at"] > self.retention_seconds or counts[role] > self.max_retained_per_role
                    or sizes[role] > self.max_role_log_bytes
                ):
                    self._remove_log(Path(row["log_path"]))
                    self._remove_directory(row["task_id"])
                    self._db.execute("DELETE FROM tasks WHERE task_id=?", (row["task_id"],))
                    self._db.execute("DELETE FROM task_completions WHERE task_id=?", (row["task_id"],))
            self._db.commit()

    def _remove_log(self, path: Path) -> None:
        if not path.parent.resolve().is_relative_to((self.root / "logs").resolve()):
            raise ValueError("task log path is outside its storage root")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if not path.is_dir():
                raise
            # A failed log may have been replaced by an empty directory. Never
            # walk or recursively delete content supplied at this path.
            path.rmdir()

    def _remove_directory(self, task_id: str) -> None:
        root = self.root / "workspaces"
        directory = root / task_id
        if directory.parent != root or not root.resolve().is_relative_to(self.root):
            raise ValueError("task directory is outside its storage root")
        if directory.is_symlink():
            directory.unlink()
        elif directory.is_junction():
            directory.rmdir()
        elif directory.exists():
            # Check the final absolute target before recursive removal. rmtree
            # does not traverse child symlinks or Windows directory junctions.
            if directory.resolve().parent != root.resolve():
                raise ValueError("task directory is outside its storage root")
            shutil.rmtree(directory)
