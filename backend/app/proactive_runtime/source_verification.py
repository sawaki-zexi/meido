"""An explicit desktop-owner code test owns one non-replayable Runtime task.

The HTTP host alone calls submit_owner after checking trustLocalCode=true; no
AgentTool/channel accepts these actions. request_nonce is the stable identity of
that actual owner action, never a synthetic human message or AutonomyOrigin.
Call ensure_schema in the host's existing transaction before constructing this
application. The host owns recover/invalidate/close and must retain failed owners.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from ..mcp_stdio import finish_cleanup
from ..task_runtime import TaskOrigin, TaskBudget, TaskOutcome
from .config import AutonomyConfig, SourceGrant
from .source_validation import SourceContractRegistry
from . import retention


def ensure_schema(db):
    if not db.in_transaction:
        raise RuntimeError("verification schema requires the host transaction")
    db.execute("""CREATE TABLE IF NOT EXISTS autonomy_source_verifications (
        attempt_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,request_nonce TEXT NOT NULL,
        fingerprint TEXT NOT NULL,source_id TEXT NOT NULL,source_revision INTEGER NOT NULL,
        config_revision INTEGER NOT NULL,generation INTEGER NOT NULL,source_grant TEXT NOT NULL,
        manifest_path TEXT NOT NULL,manifest_hash TEXT NOT NULL,origin TEXT NOT NULL,
        created_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'preparing',task_id TEXT,
        launch_pending INTEGER NOT NULL DEFAULT 1,cancel_requested INTEGER NOT NULL DEFAULT 0,
        cleanup_confirmed INTEGER NOT NULL DEFAULT 0,runtime_result TEXT,
        result_ack_pending INTEGER NOT NULL DEFAULT 0,runtime_acknowledged INTEGER NOT NULL DEFAULT 0,
        proof TEXT,error TEXT NOT NULL DEFAULT '',UNIQUE(role_id,request_nonce))""")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


class _VerificationExecutor:
    def __init__(self, application, row):
        self.application, self.row = application, row
        self.cancelled, self.started, self.finished = (threading.Event() for _ in range(3))
        self._gate = threading.Lock()
        self._loop = self._task = None

    def cancel(self):
        if self.cancelled.is_set():
            return
        self.cancelled.set()
        with self._gate:
            if self._loop is not None and self._task is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._task.cancel)

    def run(self, execution):
        self.started.set()
        try:
            return asyncio.run(self._run(execution))
        finally:
            with self._gate:
                self._loop = self._task = None
            self.finished.set()

    async def _run(self, execution):
        with self._gate:
            self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
        app, row = self.application, self.row
        registry = SourceContractRegistry(app.proactive.sessions.database_path, app.proactive.mcp_host.store)
        proof, status, error = None, "failed", "source contract validation failed"
        try:
            if self.cancelled.is_set() or not app._authorized(row["attempt_id"]):
                raise PermissionError("owner source validation was sealed")
            proof = await registry.validate_local(row["role_id"], SourceGrant.model_validate_json(row["source_grant"]),
                manifest_path=Path(row["manifest_path"]), expected_manifest_sha256=row["manifest_hash"],
                signal=self.cancelled, authorized=lambda: app._authorized(row["attempt_id"]), deadline=execution.deadline)
            status, error = "succeeded", ""
        except (asyncio.CancelledError, PermissionError):
            status, error = "cancelled", "owner source validation was sealed"
        except Exception:
            pass  # No untrusted exception text/argv/path enters the result.
        finally:
            # No executor terminal outcome until every resource is joined. A
            # repeated Stop cannot tear down the loop while close needs it.
            async def dispose_owned():
                while True:
                    try:
                        await registry.close()
                        if registry.cleanup_pending:
                            raise RuntimeError("source verification cleanup is pending")
                        return
                    except BaseException:
                        app._note_cleanup(row["attempt_id"])
                        await asyncio.sleep(.05)
            cleanup = asyncio.create_task(dispose_owned())
            try:
                await finish_cleanup(cleanup)
            except asyncio.CancelledError:
                proof, status, error = None, "cancelled", "owner source validation was sealed"
            with self._gate:
                self._loop = self._task = None
        if self.cancelled.is_set() or not app._authorized(row["attempt_id"]):
            proof, status, error = None, "cancelled", "owner source validation was sealed"
        result = {"status": status, "proof": proof, "error": error, "cleanupConfirmed": True}
        return TaskOutcome("succeeded" if status == "succeeded" else "cancelled" if status == "cancelled" else "failed",
                           output_tail=_json(result))


class SourceVerificationApplication:
    def __init__(self, proactive_app):
        self.proactive = proactive_app
        self._closed = False
        self._executors = {}
        self._launches = {}
        self._recovering = threading.Lock()

    @property
    def has_owned_work(self):
        if self._launches or self._executors:
            return True
        try:
            with sqlite3.connect(self.proactive.sessions.database_path, timeout=.05) as db:
                return db.execute("SELECT 1 FROM autonomy_source_verifications WHERE runtime_acknowledged=0 LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            return True

    def delete_role(self, role_id):
        with self.proactive._transaction() as db:
            if db.execute("SELECT 1 FROM autonomy_source_verifications WHERE role_id=? AND runtime_acknowledged=0", (role_id,)).fetchone():
                raise RuntimeError("role source verification cleanup is still pending")
            db.execute("UPDATE autonomy_source_verifications SET source_grant='{}',manifest_path='',manifest_hash='',origin='{}',runtime_result=NULL,proof=NULL WHERE role_id=?", (role_id,))
            db.execute("UPDATE autonomy_source_contracts SET source_binding='{}',artifact='{}',artifact_digest='',verifier_version='deleted' WHERE role_id=?", (role_id,))

    def _authorized(self, identifier):
        """Pure read: safe even inside the validator's BEGIN IMMEDIATE."""
        p = self.proactive
        if self._closed or p._closed:
            return False
        try:
            with sqlite3.connect(p.sessions.database_path, timeout=.05) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT * FROM autonomy_source_verifications WHERE attempt_id=?", (identifier,)).fetchone()
                state = db.execute("SELECT * FROM autonomy_roles WHERE role_id=?", (row["role_id"],)).fetchone() if row else None
            if (row is None or state is None or row["cancel_requested"] or p.role_getter(row["role_id"]) is None
                    or state["human_busy"] or (state["revision"], state["generation"]) != (row["config_revision"], row["generation"])):
                return False
            source = SourceGrant.model_validate_json(row["source_grant"])
            current = AutonomyConfig.model_validate_json(state["config"])
            if source not in current.sourceGrants or not source.enabled or p.mcp_host is None:
                return False
            server = p.mcp_host.store.get(source.serverId)
            return bool(server and server.enabled and row["role_id"] in server.role_ids
                        and (server.revision, server.grant_id) == (source.configRevision, source.grantRevision))
        except (OSError, ValueError, sqlite3.Error):
            return False

    @staticmethod
    def _view(row):
        return {"id": row["attempt_id"], "sourceId": row["source_id"], "sourceRevision": row["source_revision"],
                "status": row["status"], "proof": json.loads(row["proof"]) if row["proof"] else None,
                "runtimeAcknowledged": bool(row["runtime_acknowledged"]),
                "cleanupPending": not bool(row["runtime_acknowledged"]), "error": row["error"],
                "taskId": row["task_id"], "createdAt": row["created_at"], "requestNonce": row["request_nonce"]}

    def query(self, role_id, attempt_id):
        self.proactive._require_role(role_id)
        with self.proactive._transaction() as db:
            row = db.execute("SELECT * FROM autonomy_source_verifications WHERE role_id=? AND attempt_id=?", (role_id, attempt_id)).fetchone()
        if row is None:
            raise KeyError("source verification does not belong to this role")
        return self._view(row)

    def list(self, role_id):
        self.proactive._require_role(role_id)
        with self.proactive._transaction() as db:
            rows = db.execute("SELECT * FROM autonomy_source_verifications WHERE role_id=? ORDER BY created_at DESC,attempt_id DESC LIMIT 100", (role_id,)).fetchall()
        return [self._view(row) for row in rows]

    async def submit_owner(self, role_id, *, request_nonce, source_id, expected_revision,
                           manifest_path: Path, expected_manifest_sha256: str):
        p = self.proactive
        p._require_role(role_id)
        path = Path(manifest_path)
        if (self._closed or p._closed or not isinstance(request_nonce, str)
                or not re.fullmatch(r"[a-f0-9]{32}|[a-f0-9-]{36}", request_nonce)
                or type(expected_revision) is not int or not path.is_absolute() or len(str(path)) > 4096
                or not isinstance(expected_manifest_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256)):
            raise ValueError("explicit owner verification nonce and exact manifest trust are required")
        request_nonce = uuid.UUID(request_nonce).hex
        fingerprint = hashlib.sha256(_json([source_id, expected_revision, str(path), expected_manifest_sha256]).encode()).hexdigest()
        with p._transaction() as db:
            old = db.execute("SELECT * FROM autonomy_source_verifications WHERE role_id=? AND request_nonce=?", (role_id, request_nonce)).fetchone()
            if old is not None:
                if old["fingerprint"] != fingerprint:
                    raise ValueError("owner verification nonce conflicts with its original request")
                return self._view(old)
            state = db.execute("SELECT * FROM autonomy_roles WHERE role_id=?", (role_id,)).fetchone()
            if state is None or state["revision"] != expected_revision or state["human_busy"] or state["cleanup_pending"]:
                raise PermissionError("current owner source configuration is unavailable or busy")
            config = AutonomyConfig.model_validate_json(state["config"])
            source = next((item for item in config.sourceGrants if item.sourceId == source_id), None)
            if source is None or not source.enabled or source.ackContract != "absolute-idempotent" or p.mcp_host is None:
                raise PermissionError("an exact existing enabled ACK source is required")
            if db.execute("SELECT 1 FROM autonomy_source_verifications WHERE role_id=? AND runtime_acknowledged=0", (role_id,)).fetchone():
                raise ValueError("role source verification already owns an unresolved attempt")
            identifier, now = uuid.uuid4().hex, p._now(db)
            retention.reserve(db, role_id, identifier, "autonomy_verify", now)
            origin = TaskOrigin(role_id, f"source-verification:{role_id}", parent_run_id=identifier,
                capability_version=str(state["revision"]), source_scope_id=f"owner-source-verification:{identifier}",
                source_authority_json=_json({"source": "desktop_owner_action", "requestNonce": request_nonce,
                    "generation": state["generation"], "sourceId": source.sourceId, "sourceRevision": source.revision}))
            db.execute("""INSERT INTO autonomy_source_verifications
                (attempt_id,role_id,request_nonce,fingerprint,source_id,source_revision,config_revision,generation,
                 source_grant,manifest_path,manifest_hash,origin,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, role_id, request_nonce, fingerprint, source.sourceId, source.revision,
                 state["revision"], state["generation"], source.model_dump_json(), str(path), expected_manifest_sha256,
                 _json(asdict(origin)), now))
            row = dict(db.execute("SELECT * FROM autonomy_source_verifications WHERE attempt_id=?", (identifier,)).fetchone())
        task = asyncio.create_task(asyncio.to_thread(self._submit, row))
        self._launches[identifier] = task
        try:
            await finish_cleanup(task)
        except (OSError, ValueError, PermissionError, RuntimeError, sqlite3.Error):
            pass  # Original authoritative handover remains owned by recover.
        finally:
            if task.done():
                self._launches.pop(identifier, None)
        await self.recover()
        return self.query(role_id, identifier)

    def _submit(self, row):
        executor = _VerificationExecutor(self, row)
        self._executors[row["attempt_id"]] = executor
        origin = TaskOrigin(**json.loads(row["origin"]))
        identifier = self.proactive.tasks.submit(origin, executor, kind="autonomy_verify",
            budget=TaskBudget(timeout_seconds=120, log_bytes=16384, output_chars=4000),
            description="owner-trusted local source contract test", submission_key=row["attempt_id"],
            retain_until_ack=True, completion=False, authorized=lambda: self._authorized(row["attempt_id"]))
        with self.proactive._transaction() as db:
            db.execute("UPDATE autonomy_source_verifications SET task_id=?,launch_pending=0 WHERE attempt_id=?", (identifier, row["attempt_id"]))

    def _note_cleanup(self, identifier):
        try:
            with self.proactive._transaction() as db:
                db.execute("UPDATE autonomy_source_verifications SET status='cleanup_pending',error='local source cleanup remains owned by its original loop' WHERE attempt_id=? AND runtime_acknowledged=0", (identifier,))
        except sqlite3.Error:
            pass

    def _release_reservation(self, db, identifier):
        # This separate domain owns its exact allocation, after Runtime ACK (or
        # authoritative proof that its original handover never existed).
        db.execute("UPDATE autonomy_storage_reservations SET remaining_bytes=0,rows_reserved=0,metadata_reserved=0,settled=1,settled_at=? WHERE operation_id=?", (self.proactive._now(db), identifier))

    async def recover(self):
        if not self._recovering.acquire(blocking=False):
            return
        try:
            await self._recover()
        finally:
            self._recovering.release()

    async def _recover(self):
        p = self.proactive
        for identifier, launch in tuple(self._launches.items()):
            if launch.done():
                if not launch.cancelled():
                    launch.exception()
                self._launches.pop(identifier, None)
        try:
            with p._transaction() as db:
                rows = db.execute("SELECT * FROM autonomy_source_verifications WHERE runtime_acknowledged=0").fetchall()
        except sqlite3.OperationalError as error:
            if (getattr(error, "sqlite_errorcode", 0) & 0xFF) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise
            # The verifier publishes through another connection to this same
            # database. Keep the original owners, nonce and pending copy/ACK
            # duties for the next bounded recovery pass while it holds a lock.
            return
        for row in rows:
            identifier, origin = row["attempt_id"], TaskOrigin(**json.loads(row["origin"]))
            try:
                executor = self._executors.get(identifier)
                if not self._authorized(identifier):
                    if executor is not None:
                        executor.cancel()
                    with p._transaction() as db:
                        db.execute("UPDATE autonomy_source_verifications SET cancel_requested=1 WHERE attempt_id=?", (identifier,))
                if identifier in self._launches:
                    continue
                task_id = row["task_id"] or await p._owned(p.tasks.find_submission, origin, kind="autonomy_verify", submission_key=identifier)
                if task_id is None:
                    with p._transaction() as db:
                        db.execute("UPDATE autonomy_source_verifications SET status='rejected',launch_pending=0,cleanup_confirmed=1,runtime_acknowledged=1,error='Runtime did not accept the original owner action' WHERE attempt_id=?", (identifier,))
                        self._release_reservation(db, identifier)
                    if executor is not None:
                        executor.finished.set()
                    self._executors.pop(identifier, None)
                    continue
                with p._transaction() as db:
                    db.execute("UPDATE autonomy_source_verifications SET task_id=?,launch_pending=0 WHERE attempt_id=?", (task_id, identifier))
                if row["runtime_result"] is None:
                    snapshot = await p._owned(p.tasks.query, task_id, origin)
                    if snapshot.ended_at is None:
                        continue
                    try:
                        result = json.loads(snapshot.output)
                    except ValueError:
                        result = None
                    if (not isinstance(result, dict) or result.get("cleanupConfirmed") is not True
                            or result.get("status") not in {"succeeded", "cancelled", "failed"}
                            or set(result) != {"status", "proof", "error", "cleanupConfirmed"}):
                        self._note_cleanup(identifier)
                        continue  # interrupted is not evidence of original cleanup.
                    with p._transaction() as db:
                        db.execute("UPDATE autonomy_source_verifications SET runtime_result=?,result_ack_pending=1,cleanup_confirmed=1,status=?,proof=?,error=? WHERE attempt_id=?",
                            (_json(result), result["status"], _json(result["proof"]) if result["proof"] else None,
                             result["error"], identifier))
                await p._owned(p.tasks.acknowledge_result, task_id, origin)
                with p._transaction() as db:
                    changed = db.execute("UPDATE autonomy_source_verifications SET runtime_acknowledged=1,result_ack_pending=0 WHERE attempt_id=? AND cleanup_confirmed=1", (identifier,)).rowcount
                    if changed != 1:
                        raise RuntimeError("source verification cleanup was not confirmed")
                    self._release_reservation(db, identifier)
                self._executors.pop(identifier, None)
            except (OSError, ValueError, PermissionError, KeyError, sqlite3.Error):
                continue

    async def cancel(self, role_id, attempt_id):
        self.query(role_id, attempt_id)
        with self.proactive._transaction() as db:
            db.execute("UPDATE autonomy_source_verifications SET cancel_requested=1 WHERE role_id=? AND attempt_id=? AND runtime_acknowledged=0", (role_id, attempt_id))
        executor = self._executors.get(attempt_id)
        if executor is not None:
            executor.cancel()
        await self.recover()
        return self.query(role_id, attempt_id)

    async def invalidate(self, role_id=None):
        with self.proactive._transaction() as db:
            rows = db.execute("SELECT * FROM autonomy_source_verifications WHERE runtime_acknowledged=0" + (" AND role_id=?" if role_id is not None else ""), (role_id,) if role_id is not None else ()).fetchall()
            for row in rows:
                db.execute("UPDATE autonomy_source_verifications SET cancel_requested=1 WHERE attempt_id=?", (row["attempt_id"],))
        for row in rows:
            executor = self._executors.get(row["attempt_id"])
            if executor is not None:
                executor.cancel()
        for task in tuple(self._launches.values()):
            try:
                await finish_cleanup(task)
            except (OSError, ValueError, PermissionError, RuntimeError, sqlite3.Error):
                pass
        await self.recover()
        for row in rows:
            origin = TaskOrigin(**json.loads(row["origin"]))
            try:
                identifier = await self.proactive._owned(self.proactive.tasks.find_submission, origin,
                    kind="autonomy_verify", submission_key=row["attempt_id"])
                if identifier:
                    await self.proactive._owned(self.proactive.tasks.stop, identifier, origin)
            except (PermissionError, KeyError, OSError):
                pass  # A hidden/unconfirmed owner keeps its durable duty.
            executor = self._executors.get(row["attempt_id"])
            if executor is not None and not await self.proactive._owned(executor.finished.wait, 10):
                raise RuntimeError("source verification cleanup remains owned and pending")
        await self.recover()

    async def close(self):
        self._closed = True
        await self.invalidate()
        with self.proactive._transaction() as db:
            if db.execute("SELECT 1 FROM autonomy_source_verifications WHERE runtime_acknowledged=0").fetchone():
                raise RuntimeError("source verification still owns a pending recovery duty")
