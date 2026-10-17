"""Real SQLite storage admission, expiry and durable obligation protection."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import threading

import pytest

from test_proactive_configuration import system
from backend.app.proactive_runtime import retention
from backend.app.proactive_runtime.identity import AutonomyOrigin

pytestmark=pytest.mark.usefixtures("local_platform_only")
DAY=86400


@pytest.fixture
def database(system):
    _,sessions,*_=system
    path=sessions.database_path
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        retention.ensure_schema(db)
    yield path
    # Pure SQL cases do not launch Runtime tasks. Seal their synthetic rows
    # before the application fixture performs its ordinary shutdown recovery.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE autonomy_runs SET runtime_acknowledged=1,result_ack_pending=0,launch_pending=0")


@contextmanager
def transaction(path):
    with sqlite3.connect(path,timeout=10) as db:
        db.row_factory=sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        yield db


def attempt(db,identifier="tick",role="role",kind="autonomy_tick",now=1000,**kwargs):
    assert retention.reserve(db,role,identifier,kind,now,**kwargs)
    db.execute("INSERT INTO autonomy_runs(tick_id,role_id,origin,kind,started_at) VALUES(?,?,?,?,?)",
               (identifier,role,AutonomyOrigin(role,identifier,1,1,("active_decision",)).serialized(),kind,now))


def finish(db,identifier="tick"):
    db.execute("UPDATE autonomy_runs SET status='skipped',runtime_acknowledged=1,launch_pending=0 WHERE tick_id=?",(identifier,))


def test_reservation_and_same_transaction_business_insert_rollback_together(database):
    with pytest.raises(RuntimeError):
        with transaction(database) as db:
            attempt(db)
            raise RuntimeError("rollback whole admission")
    with transaction(database) as db:
        attempt(db)
        assert not retention.reserve(db,"role","tick","autonomy_tick",2000)
        with pytest.raises(ValueError): retention.reserve(db,"other","tick","autonomy_tick",2000)


def test_concurrent_connections_cannot_both_spend_one_global_reservation(database):
    gate=threading.Barrier(2)
    limits=retention.Limits(global_bytes=4*1024*1024)
    def admit(index):
        gate.wait()
        try:
            with transaction(database) as db:
                attempt(db,str(index),role=str(index),limits=limits)
            return True
        except retention.StorageLimitError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(admit,(1,2)))==[False,True]


@pytest.mark.parametrize("limits",[
    {"role_records":10}, {"role_bytes":12*1024},
])
def test_role_metadata_headroom_rejects_new_work_without_forgetting_old_attempt(database,limits):
    cap=retention.Limits(**limits)
    with transaction(database) as db:
        attempt(db,"first",kind="autonomy_ack",limits=cap)
        with pytest.raises(retention.StorageLimitError):
            attempt(db,"second",kind="autonomy_ack",limits=cap)
        # A caught quota error does not leave a half-reservation.
        assert not retention.reserve(db,"role","first","autonomy_ack",1000,limits=cap)
        assert db.execute("SELECT COUNT(*) FROM autonomy_runs").fetchone()[0]==1


def test_utf8_snapshot_and_transcript_limits_and_aggregate_run_limit(database):
    with transaction(database) as db:
        attempt(db)
        with pytest.raises(retention.StorageLimitError):
            retention.store_snapshot(db,"tick",{"bodies":{"id":"字"*180000}},1000)
        assert retention.store_snapshot(db,"tick",{"bodies":{"id":"x"*180000}},1000)
        with pytest.raises(retention.StorageLimitError): retention.record_transcript(db,"tick","字"*22000,1000)
        for _ in range(33): retention.record_transcript(db,"tick","x"*63000,1000)
        with pytest.raises(retention.StorageLimitError): retention.record_transcript(db,"tick","x"*63000,1000)


def test_settle_releases_only_unused_capacity_and_never_reopens_original_attempt(database):
    with transaction(database) as db:
        attempt(db)
        retention.store_snapshot(db,"tick",{"bodies":{"id":"中文"}},1000)
        active=retention.usage(db,"role")["globalBytes"]
        assert retention.settle(db,"tick",now=1001)["status"]=="active"
        finish(db)
        first=retention.settle(db,"tick",now=1001)
        assert first["status"]=="settled" and first["releasedBytes"]>0
        assert 0<retention.usage(db,"role")["globalBytes"]<active
        assert retention.settle(db,"tick",now=1002)["releasedBytes"]==0
        assert not retention.reserve(db,"role","tick","autonomy_tick",1002)
        with pytest.raises(ValueError): retention.record_transcript(db,"tick","late",1002)


def test_prune_body_transcript_and_resolved_details_without_deleting_tombstones(database):
    with transaction(database) as db:
        attempt(db)
        retention.store_snapshot(db,"tick",{"sources":[{"sourceId":"src","status":"ok"}],"bodies":{"id":"body"}},1000)
        retention.record_transcript(db,"tick",{"role":"assistant","text":"private transcript"},1000)
        finish(db)
        retention.settle(db,"tick",now=1000)
        # A poll-only installation has no lazily-created autonomy_turns table.
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='autonomy_turns'").fetchone()
        retention.prune(db,1000+2*DAY)
        snapshot=json.loads(db.execute("SELECT snapshot FROM autonomy_runs").fetchone()[0])
        assert "body" not in json.dumps(snapshot) and snapshot["sources"][0]["status"]=="ok"
        assert retention.usage(db,"role")["globalBytes"]>0
        retention.prune(db,1000+8*DAY)
        assert retention.prune(db,1000+91*DAY)["compactedRuns"]==1
        assert db.execute("SELECT COUNT(*) FROM autonomy_runs").fetchone()[0]==1
        assert not retention.reserve(db,"role","tick","autonomy_tick",1000+100*DAY)


@pytest.mark.parametrize("obligation",["active","uncertain_delivery","ack_uncertain","invalid_receipt","revoked_ack_after_dispatch"])
def test_expired_payload_is_removed_while_active_or_unknown_identity_stays_owned(database,obligation):
    with transaction(database) as db:
        attempt(db)
        retention.store_snapshot(db,"tick",{"bodies":{"id":"must survive"}},1000)
        retention.record_transcript(db,"tick","must survive",1000)
        if obligation!="active": finish(db)
        if obligation=="uncertain_delivery":
            db.execute("INSERT INTO autonomy_deliveries(intent_id,tick_id,role_id,target,draft,status,dispatched,created_at) VALUES('intent','tick','role','desktop','{}','uncertain',1,1000)")
        if obligation=="ack_uncertain":
            db.execute("""INSERT INTO autonomy_ack_outbox(ack_id,role_id,tick_id,source_id,source_revision,event_id,
                decision_at,permanent,reason,source_grant,status) VALUES('ack','role','tick','src',1,'event',1000,1,'referenced','{}','ack_uncertain')""")
        if obligation=="invalid_receipt":
            db.execute("""INSERT INTO autonomy_deliveries(intent_id,tick_id,role_id,target,draft,status,dispatched,created_at,message_id,receipt)
                VALUES('intent','tick','role','desktop','{}','sent',1,1000,'message','not a receipt')""")
        if obligation=="revoked_ack_after_dispatch":
            db.execute("""INSERT INTO autonomy_ack_outbox(ack_id,role_id,tick_id,source_id,source_revision,event_id,
                decision_at,permanent,reason,source_grant,status) VALUES('ack','role','tick','src',1,'event',1000,1,'referenced','{}','revoked')""")
            db.execute("INSERT INTO autonomy_ack_attempts VALUES('ack',1,'tick',1)")
        result=retention.settle(db,"tick",now=1001)
        assert result["status"]==("active" if obligation=="active" else "held")
        before=retention.usage(db,"role")["globalBytes"]
        if obligation!="active": assert before<1024*1024
        deliveries = [tuple(row) for row in db.execute("SELECT * FROM autonomy_deliveries")]
        acks = [tuple(row) for row in db.execute("SELECT * FROM autonomy_ack_outbox")]
        retention.prune(db,1000+100*DAY)
        assert "must survive" not in db.execute("SELECT snapshot FROM autonomy_runs").fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM autonomy_transcripts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM autonomy_runs").fetchone()[0] == 1
        assert [tuple(row) for row in db.execute("SELECT * FROM autonomy_deliveries")] == deliveries
        assert [tuple(row) for row in db.execute("SELECT * FROM autonomy_ack_outbox")] == acks
        assert retention.usage(db,"role")["globalBytes"] < before


def test_prune_can_settle_a_recovered_terminal_before_any_prior_settle_call(database):
    with transaction(database) as db:
        attempt(db)
        finish(db)
        assert retention.prune(db, 1000 + 8 * DAY)["compactedRuns"] == 0
        assert retention.prune(db, 1000 + 100 * DAY)["compactedRuns"] == 1


def test_unrelated_shared_session_bytes_do_not_consume_autonomous_quota(database):
    with transaction(database) as db:
        before=retention.usage(db,"role")
        db.execute("CREATE TABLE unrelated_private_chat(value TEXT)")
        db.execute("INSERT INTO unrelated_private_chat VALUES(?)",("字"*1000000,))
        assert retention.usage(db,"role")==before


def test_mutating_helpers_require_callers_transaction(database):
    with sqlite3.connect(database) as db:
        with pytest.raises(RuntimeError,match="transaction"):
            retention.reserve(db,"role","tick","autonomy_tick",1000)


def test_metadata_accounting_uses_utf8_bytes_instead_of_sqlite_character_count(database):
    with transaction(database) as db:
        db.execute("INSERT INTO autonomy_event_claims VALUES('role',?,'tick',NULL,'referenced')",("字"*10,))
        chinese=retention.usage(db,"role")
        db.execute("DELETE FROM autonomy_event_claims")
        db.execute("INSERT INTO autonomy_event_claims VALUES('role',?,'tick',NULL,'referenced')",("x"*10,))
        ascii_value=retention.usage(db,"role")
        assert chinese["globalBytes"]-ascii_value["globalBytes"]==20
        assert chinese["roleBytes"]-ascii_value["roleBytes"]==20


def test_ack_operation_links_unknown_original_obligation_and_later_releases_recovery_headroom(database):
    with transaction(database) as db:
        attempt(db,"original")
        attempt(db,"ack-operation",kind="autonomy_ack")
        finish(db,"original")
        finish(db,"ack-operation")
        db.execute("""INSERT INTO autonomy_ack_outbox(ack_id,role_id,tick_id,source_id,source_revision,event_id,
            decision_at,permanent,reason,source_grant,status) VALUES('ack','role','original','src',1,'event',1000,1,'referenced','{}','ack_uncertain')""")
        db.execute("INSERT INTO autonomy_ack_attempts VALUES('ack',1,'ack-operation',1)")
        assert retention.settle(db,"original",now=1001)["status"]=="held"
        assert retention.settle(db,"ack-operation",now=1001)["status"]=="held"
        held=retention.usage(db,"role")["globalBytes"]
        assert held<1024*1024
        db.execute("UPDATE autonomy_ack_outbox SET status='acknowledged'")
        assert retention.settle(db,"original",now=1002)["status"]=="settled"
        assert retention.settle(db,"ack-operation",now=1002)["status"]=="settled"
        assert retention.usage(db,"role")["globalBytes"]<held
        retention.prune(db,1002+91*DAY)
        assert db.execute("SELECT COUNT(*) FROM autonomy_ack_attempts").fetchone()[0]==1
        assert tuple(db.execute("SELECT status,source_grant FROM autonomy_ack_outbox").fetchone())==("acknowledged","{}")


def test_pruning_does_not_delete_permanent_business_keys_even_when_over_quota(database):
    with transaction(database) as db:
        attempt(db)
        finish(db)
        retention.settle(db,"tick",now=1000)
        db.execute("INSERT INTO autonomy_event_claims VALUES('role','event-key','tick',NULL,'referenced')")
        db.execute("INSERT INTO autonomy_delivery_keys VALUES('role','message-key','intent',1001)")
        retention.prune(db,1000+100*DAY)
        for table in ("autonomy_event_claims","autonomy_delivery_keys"):
            assert db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]==1
        before=retention.usage(db,"role")
        with pytest.raises(retention.StorageLimitError):
            retention.reserve(db,"role","new","autonomy_tick",1000+101*DAY,limits=retention.Limits(role_records=1))
        assert retention.usage(db,"role")==before
