"""Explicit desktop source tests own real Runtime/results/stdio and recovery."""
import asyncio
import hashlib
import json
import sqlite3
import threading
import time
import uuid

import pytest

from backend.app.proactive_runtime.source_verification import SourceVerificationApplication, ensure_schema
from backend.app.proactive_runtime.source_validation import retry_allowed
from backend.app.proactive_runtime import retention
from backend.app import mcp_stdio
from test_proactive_source_contract import contract, acknowledgements, ack_environment, source  # noqa: F401


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.fixture
def verification(contract):
    value = contract
    with value.app._transaction() as db:
        ensure_schema(db)
    verification = getattr(value.app, "source_verifications", None) or SourceVerificationApplication(value.app)
    yield verification, value
    asyncio.run(verification.close())


def request(value):
    return {"request_nonce": uuid.uuid4().hex, "source_id": value.grant.sourceId,
        "expected_revision": value.app.query("role")["config"]["revision"],
        "manifest_path": value.manifest,
        "expected_manifest_sha256": hashlib.sha256(value.manifest.read_bytes()).hexdigest()}


async def settled(verification, identifier):
    async with asyncio.timeout(8):
        while True:
            await verification.recover()
            try:
                view = verification.query("role", identifier)
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", None) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
            else:
                if view["runtimeAcknowledged"]:
                    return view
            await asyncio.sleep(.01)


@pytest.mark.parametrize("handover", ["normal", "lost_return", "task_id_write"])
def test_busy_recovery_snapshot_defers_original_inflight_owner_action(verification, monkeypatch, handover):
    app, value = verification
    arguments, submissions, calls = request(value), [], []
    entered, release = threading.Event(), threading.Event()
    submit, call = value.app.tasks.submit, mcp_stdio.McpStdioClient.call

    def recorded_submit(origin, executor, **options):
        identifier = submit(origin, executor, **options)
        submissions.append((identifier, origin, options["submission_key"]))
        if handover == "lost_return":
            raise OSError("accepted owner handover response was lost")
        return identifier

    async def held_call(client, name, params):
        response = await call(client, name, params)
        calls.append(name)
        if name == "meido_test_seed":
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.01)
        return response

    async def exercise():
        try:
            with monkeypatch.context() as patch:
                patch.setattr(value.app.tasks, "submit", recorded_submit)
                patch.setattr(mcp_stdio.McpStdioClient, "call", held_call)
                if handover == "task_id_write":
                    with sqlite3.connect(value.sessions.database_path) as db:
                        db.execute("CREATE TRIGGER busy_verification_task_id_failure BEFORE UPDATE OF task_id ON autonomy_source_verifications WHEN NEW.task_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'controlled taskId projection failure before busy recovery'); END")
                first = await app.submit_owner("role", **arguments)
                if handover == "task_id_write":
                    assert first["taskId"] is None and first["cleanupPending"]
                assert await asyncio.to_thread(entered.wait, 3)
                with sqlite3.connect(value.sessions.database_path) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    before = writer.execute("SELECT * FROM autonomy_source_verifications WHERE attempt_id=?", (first["id"],)).fetchone()
                    started = time.monotonic()
                    await app.recover()
                    assert .04 <= time.monotonic() - started < 1
                    assert writer.execute("SELECT * FROM autonomy_source_verifications WHERE attempt_id=?", (first["id"],)).fetchone() == before
                    assert app.has_owned_work and not first["runtimeAcknowledged"]
                    writer.rollback()
                if handover == "task_id_write":
                    with sqlite3.connect(value.sessions.database_path) as db:
                        db.execute("DROP TRIGGER busy_verification_task_id_failure")
                release.set()
                final = await settled(app, first["id"])
                completed_calls = calls[:]
                assert await app.submit_owner("role", **arguments) == final
                assert calls == completed_calls
            assert final["status"] == "succeeded" and not final["cleanupPending"]
            assert final["requestNonce"] == arguments["request_nonce"]
            assert len(submissions) == 1 and submissions[0][0] == final["taskId"]
            assert submissions[0][2] == first["id"]
            assert calls.count("meido_test_seed") == 1
            assert not app.has_owned_work
        finally:
            release.set()
            with sqlite3.connect(value.sessions.database_path) as db:
                db.execute("DROP TRIGGER IF EXISTS busy_verification_task_id_failure")

    asyncio.run(exercise())


@pytest.mark.parametrize("lock_at", ["before_ack", "after_ack"])
def test_busy_snapshot_preserves_copied_result_and_exact_ack_duty(verification, monkeypatch, lock_at):
    app, value = verification
    tasks = value.app.tasks
    arguments, submissions, acknowledgements, writers = request(value), [], [], []
    submit, acknowledge = tasks.submit, tasks.acknowledge_result

    def recorded_submit(origin, executor, **options):
        submissions.append((origin, options["submission_key"]))
        return submit(origin, executor, **options)

    def ack_with_locked_projection(identifier, origin):
        acknowledgements.append((identifier, origin))
        if not writers:
            if lock_at == "after_ack":
                acknowledge(identifier, origin)
            writer = sqlite3.connect(value.sessions.database_path, check_same_thread=False)
            writer.execute("BEGIN IMMEDIATE")
            writers.append(writer)
            if lock_at == "before_ack":
                raise OSError("Runtime ACK is deferred while local projection is locked")
            return
        return acknowledge(identifier, origin)

    def no_query(*args, **kwargs):
        raise AssertionError("a copied verification result must not be queried or executed again")

    async def exercise():
        try:
            with monkeypatch.context() as patch:
                patch.setattr(tasks, "submit", recorded_submit)
                patch.setattr(tasks, "acknowledge_result", ack_with_locked_projection)
                first = await app.submit_owner("role", **arguments)
                async with asyncio.timeout(8):
                    while not writers:
                        await app.recover()
                        await asyncio.sleep(.01)
                writer = writers[0]
                before = writer.execute("SELECT runtime_result,result_ack_pending,cleanup_confirmed,runtime_acknowledged,request_nonce,task_id FROM autonomy_source_verifications WHERE attempt_id=?", (first["id"],)).fetchone()
                assert before[0] and before[1:4] == (1, 1, 0)
                assert before[4:] == (arguments["request_nonce"], acknowledgements[0][0])
                started = time.monotonic()
                await app.recover()
                assert .04 <= time.monotonic() - started < 1
                assert writer.execute("SELECT runtime_result,result_ack_pending,cleanup_confirmed,runtime_acknowledged,request_nonce,task_id FROM autonomy_source_verifications WHERE attempt_id=?", (first["id"],)).fetchone() == before
                assert app.has_owned_work
                if lock_at == "after_ack":
                    tasks.retention_seconds = -1
                    assert tasks.list(submissions[0][0], kind="autonomy_verify") == ()
                writer.rollback()
                patch.setattr(tasks, "query", no_query)
                final = await settled(app, first["id"])
                assert await app.submit_owner("role", **arguments) == final
            assert final["status"] == "succeeded" and not final["cleanupPending"]
            assert len(submissions) == 1 and submissions[0][1] == final["id"]
            assert len(acknowledgements) == 2
            assert acknowledgements[0] == acknowledgements[1]
            assert not app.has_owned_work
            with sqlite3.connect(value.sessions.database_path) as db:
                assert db.execute("SELECT COUNT(*) FROM autonomy_source_contracts").fetchone()[0] == 1
                assert retention.usage(db, "role")["globalBytes"] < 128 * 1024
        finally:
            for writer in writers:
                writer.rollback()
                writer.close()

    asyncio.run(exercise())


def test_recovery_snapshot_does_not_hide_nonbusy_database_errors(verification):
    app, value = verification

    async def exercise():
        with value.app._transaction() as db:
            db.execute("ALTER TABLE autonomy_source_verifications RENAME TO temporarily_unavailable_verifications")
        try:
            with pytest.raises(sqlite3.OperationalError, match="no such table") as error:
                await app.recover()
            assert error.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
        finally:
            with value.app._transaction() as db:
                db.execute("ALTER TABLE temporarily_unavailable_verifications RENAME TO autonomy_source_verifications")
        await app.recover()
        assert not app.has_owned_work

    asyncio.run(exercise())


def test_actual_owner_nonce_verifies_once_without_a_human_chat_or_autonomy_tick(verification, monkeypatch):
    app, value = verification
    arguments, calls = request(value), []
    original = value.app.tasks.submit

    def record(origin, executor, **kwargs):
        calls.append((origin, kwargs))
        return original(origin, executor, **kwargs)

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(value.app.tasks, "submit", record)
            first = await app.submit_owner("role", **arguments)
            final = await settled(app, first["id"])
            repeated = await app.submit_owner("role", **arguments)
        assert repeated == final
        assert final["status"] == "succeeded" and final["proof"]["proofId"]
        assert not final["cleanupPending"]
        assert len(calls) == 1
        origin, options = calls[0]
        assert origin.session_key == "source-verification:role"
        assert options["kind"] == "autonomy_verify"
        assert options["submission_key"] == final["id"]
        assert options["budget"].timeout_seconds == 120
        assert options["retain_until_ack"] and not options["completion"]
        assert value.app.query("role")["runs"] == []
        assert value.sessions.list_messages("role:role") == []
        with sqlite3.connect(value.sessions.database_path) as db:
            assert retry_allowed(db, "role", value.grant.model_copy(update={"ackProofId": final["proof"]["proofId"]}))
            assert retention.usage(db, "role")["globalBytes"] < 128 * 1024
        with pytest.raises(KeyError):
            app.query("role", "unknown-owner-attempt")
        with pytest.raises(ValueError, match="conflicts"):
            await app.submit_owner("role", **{**arguments, "expected_manifest_sha256": "0" * 64})

    asyncio.run(exercise())


@pytest.mark.parametrize("seal", ["cancel", "human"])
def test_owner_cancellation_or_genuine_human_priority_seals_and_joins_validation(verification, monkeypatch, seal):
    app, value = verification
    entered = threading.Event()
    original = mcp_stdio.McpStdioClient.call

    async def held(client, name, arguments):
        response = await original(client, name, arguments)
        if name == "meido_test_seed":
            entered.set()
            while True:
                await asyncio.sleep(.01)
        return response

    async def exercise():
        finish_human = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(mcp_stdio.McpStdioClient, "call", held)
                first = await app.submit_owner("role", **request(value))
                assert await asyncio.to_thread(entered.wait, 3)
                assert app.has_owned_work
                with pytest.raises(RuntimeError, match="pending"):
                    app.delete_role("role")
                if seal == "cancel":
                    await app.cancel("role", first["id"])
                else:
                    finish_human = value.app.accept_human_run("role", "genuine-host-owner-input")
                final = await settled(app, first["id"])
            assert final["status"] == "cancelled" and final["proof"] is None
            assert not app.has_owned_work
            with sqlite3.connect(value.sessions.database_path) as db:
                assert db.execute("SELECT COUNT(*) FROM autonomy_source_contracts").fetchone()[0] == 0
                assert retention.usage(db, "role")["globalBytes"] < 128 * 1024
        finally:
            if finish_human is not None:
                finish_human()

    asyncio.run(exercise())


def test_cleanup_failure_keeps_original_loop_and_reservation_until_close_can_join(verification, monkeypatch):
    from backend.app.proactive_runtime import source_validation
    app, value = verification
    roots = []

    def disk_failure(path):
        roots.append(path)
        raise OSError("controlled verification owned directory cleanup failure")

    async def exercise():
        closing = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(source_validation.shutil, "rmtree", disk_failure)
                first = await app.submit_owner("role", **request(value))
                async with asyncio.timeout(8):
                    while app.query("role", first["id"])["status"] != "cleanup_pending":
                        await app.recover()
                        await asyncio.sleep(.01)
                assert roots and roots[0].exists()
                view = app.query("role", first["id"])
                assert view["cleanupPending"] and not view["runtimeAcknowledged"]
                with sqlite3.connect(value.sessions.database_path) as db:
                    assert db.execute("SELECT COUNT(*) FROM autonomy_source_contracts").fetchone()[0] == 0
                    assert retention.usage(db, "role")["globalBytes"] >= 16 * 1024 * 1024
                closing = asyncio.create_task(app.close())
                await asyncio.sleep(.05)
                assert not closing.done()
            await asyncio.wait_for(closing, 8)
            final = app.query("role", first["id"])
            assert final["runtimeAcknowledged"] and not final["cleanupPending"]
            assert not roots[0].exists() and not app.has_owned_work
        finally:
            if closing is not None and not closing.done():
                await closing

    asyncio.run(exercise())


def test_late_original_submit_is_not_reclassified_before_human_seal_and_join(verification, monkeypatch):
    app, value = verification
    entered, release = threading.Event(), threading.Event()
    original, origins = value.app.tasks.submit, []

    def delayed(origin, executor, **kwargs):
        origins.append(origin)
        entered.set()
        assert release.wait(5), "controlled original owner handover release timed out"
        return original(origin, executor, **kwargs)

    async def exercise():
        submitted, finish_human = None, None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(value.app.tasks, "submit", delayed)
                submitted = asyncio.create_task(app.submit_owner("role", **request(value)))
                assert await asyncio.to_thread(entered.wait, 3)
                old = app.list("role")[0]
                with pytest.raises(ValueError, match="unresolved"):
                    await app.submit_owner("role", **request(value))
                finish_human = value.app.accept_human_run("role", "genuine-human-during-owner-handover")
                await app.recover()
                assert app.query("role", old["id"])["status"] != "rejected"
                assert app.query("role", old["id"])["cleanupPending"]
                release.set()
                first = await submitted
            final = await settled(app, first["id"])
            assert final["status"] == "rejected" and final["proof"] is None
            assert len(origins) == 1
            assert value.app.tasks.find_submission(origins[0], kind="autonomy_verify", submission_key=old["id"]) is None
            assert not (value.root / "calls.jsonl").exists()
            with sqlite3.connect(value.sessions.database_path) as db:
                assert retention.usage(db, "role")["globalBytes"] < 128 * 1024
        finally:
            release.set()
            if submitted is not None:
                await asyncio.gather(submitted, return_exceptions=True)
            if finish_human is not None:
                finish_human()

    asyncio.run(exercise())


def test_completed_role_deletion_erases_trust_details_but_keeps_nonce_tombstone(verification):
    app, value = verification
    arguments = request(value)

    async def exercise():
        first = await app.submit_owner("role", **arguments)
        final = await settled(app, first["id"])
        proof = final["proof"]["proofId"]
        app.delete_role("role")
        erased = app.query("role", first["id"])
        assert erased["requestNonce"] == final["requestNonce"] and erased["proof"] is None
        with sqlite3.connect(value.sessions.database_path) as db:
            assert db.execute("SELECT manifest_path,manifest_hash,source_grant,origin FROM autonomy_source_verifications WHERE attempt_id=?", (first["id"],)).fetchone() == ("", "", "{}", "{}")
            assert not retry_allowed(db, "role", value.grant.model_copy(update={"ackProofId": proof}))
        assert (await app.submit_owner("role", **arguments))["id"] == first["id"]

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["lost_return", "task_id_sqlite"])
def test_owner_handover_fault_recovers_original_task_without_revalidating(verification, monkeypatch, failure):
    app, value = verification
    calls, accepted = [], []
    original = value.app.tasks.submit

    def submit(origin, executor, **kwargs):
        calls.append((origin, kwargs))
        identifier = original(origin, executor, **kwargs)
        accepted.append(identifier)
        if failure == "lost_return":
            raise RuntimeError("controlled accepted submit response loss")
        return identifier

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(value.app.tasks, "submit", submit)
            if failure == "task_id_sqlite":
                with sqlite3.connect(value.sessions.database_path) as db:
                    db.execute("CREATE TRIGGER verification_task_id_failure BEFORE UPDATE OF task_id ON autonomy_source_verifications WHEN NEW.task_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'controlled verification handover write failure'); END")
            try:
                first = await app.submit_owner("role", **request(value))
                if failure == "task_id_sqlite":
                    assert first["taskId"] is None and first["cleanupPending"]
            finally:
                if failure == "task_id_sqlite":
                    with sqlite3.connect(value.sessions.database_path) as db:
                        db.execute("DROP TRIGGER verification_task_id_failure")
            final = await settled(app, first["id"])
            for _ in range(3):
                await app.recover()
        assert final["status"] == "succeeded"
        assert len(calls) == len(accepted) == 1
        assert final["taskId"] == accepted[0]

    asyncio.run(exercise())


def test_post_ack_sqlite_failure_and_pruned_result_reack_only_durable_copy(verification, monkeypatch):
    app, value = verification
    tasks, calls, acknowledgements = value.app.tasks, [], []
    real_submit, real_ack = tasks.submit, tasks.acknowledge_result

    def submit(origin, executor, **kwargs):
        calls.append(origin)
        return real_submit(origin, executor, **kwargs)

    def acknowledge(identifier, origin):
        real_ack(identifier, origin)
        acknowledgements.append((identifier, origin))

    def no_query(*args, **kwargs):
        raise AssertionError("verification recovery must use its durable result copy")

    async def exercise():
        with monkeypatch.context() as patch:
            patch.setattr(tasks, "submit", submit)
            patch.setattr(tasks, "acknowledge_result", acknowledge)
            with sqlite3.connect(value.sessions.database_path) as db:
                db.execute("CREATE TRIGGER verification_ack_clear_failure BEFORE UPDATE OF result_ack_pending ON autonomy_source_verifications WHEN OLD.result_ack_pending=1 AND NEW.result_ack_pending=0 BEGIN SELECT RAISE(ABORT,'controlled verification post-ACK failure'); END")
            try:
                first = await app.submit_owner("role", **request(value))
                async with asyncio.timeout(8):
                    while not acknowledgements:
                        await app.recover()
                        await asyncio.sleep(.01)
                assert app.query("role", first["id"])["cleanupPending"]
                tasks.retention_seconds = -1
                assert tasks.list(calls[0], kind="autonomy_verify") == ()
            finally:
                with sqlite3.connect(value.sessions.database_path) as db:
                    db.execute("DROP TRIGGER verification_ack_clear_failure")
            patch.setattr(tasks, "query", no_query)
            final = await settled(app, first["id"])
        assert final["status"] == "succeeded"
        assert len(calls) == 1 and len(acknowledgements) >= 2
        assert len(set(identifier for identifier, _ in acknowledgements)) == 1

    asyncio.run(exercise())
