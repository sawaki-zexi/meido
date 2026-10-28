"""Own autonomy configuration, host admission and durable business recovery."""
from contextlib import contextmanager
import asyncio
import json
import sqlite3
import threading
import time
import uuid
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .drift import DriftTurn

from ..session_store import SessionStore
from ..task_runtime import TaskRuntime, TaskBudget, TaskOutcome
from ..mcp_stdio import finish_cleanup
from .config import AutonomyConfig, MemePurposeConflict
from .preferences import ProactivePreferences
from .identity import AutonomyOrigin
from .execution import AutonomyExecutor
from .sources import collect_snapshot
from ..mcp_autonomy import AutonomyMcpGrant
from .delivery import ensure_schema as ensure_delivery_schema
from . import bindings
from . import retention
from .source_validation import ensure_schema as ensure_source_validation_schema
from .source_verification import SourceVerificationApplication, ensure_schema as ensure_verification_schema

OPERATION_TIMEOUTS = {"autonomy_tick": 600, "autonomy_poll": 120, "autonomy_ack": 30}


class ProactiveApplication:
    def __init__(self, sessions: SessionStore, tasks: TaskRuntime, *,
                 role_getter: Callable, model_getter: Callable, clock: Callable = time.time,
                 mcp_host=None, adapter_factory=None, fetch_body=None, recall_memory=None, web_tools=None,
                 context_reader=None, activity_reader=None, workspace_getter=None, channel_host=None, enabled=lambda: True,
                 role_model_getter=lambda role_id: None):
        self.sessions, self.tasks = sessions, tasks
        self.role_getter, self.model_getter, self.clock = role_getter, model_getter, clock
        self.role_model_getter = role_model_getter
        self.mcp_host, self.adapter_factory, self.fetch_body = mcp_host, adapter_factory, fetch_body
        self.recall_memory, self.web_tools = recall_memory, web_tools
        self.context_reader = context_reader
        self.activity_reader = activity_reader
        self.enabled = enabled
        self._gate = threading.RLock()
        self._closed = False
        self._executors: dict[str, AutonomyExecutor] = {}
        self._drift_owners: dict[str, 'DriftTurn'] = {}
        self._handovers: set[str] = set()
        self._cleanup_tasks: dict[str, asyncio.Task] = {}
        self._worker = None
        self._closing = None
        self._human_finish_pending: set[tuple[str, str]] = set()
        self.errors: list[str] = []
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS autonomy_meta (
                key TEXT PRIMARY KEY, value REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS autonomy_roles (
                role_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                generation INTEGER NOT NULL, config TEXT NOT NULL,
                next_at REAL NOT NULL, next_poll_at REAL NOT NULL,
                last_human_end REAL NOT NULL, human_busy INTEGER NOT NULL DEFAULT 0,
                cleanup_pending INTEGER NOT NULL DEFAULT 0,content_rotation INTEGER NOT NULL DEFAULT 0,
                admission_error TEXT NOT NULL DEFAULT '')""")
            db.execute("""CREATE TABLE IF NOT EXISTS autonomy_runs (
                tick_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,origin TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'autonomy_tick',mode TEXT NOT NULL DEFAULT 'pending',
                status TEXT NOT NULL DEFAULT 'preparing',started_at REAL NOT NULL,
                task_id TEXT,runtime_result TEXT,result_ack_pending INTEGER NOT NULL DEFAULT 0,
                runtime_acknowledged INTEGER NOT NULL DEFAULT 0,
                launch_pending INTEGER NOT NULL DEFAULT 1,error TEXT NOT NULL DEFAULT '',
                frozen_config TEXT NOT NULL DEFAULT '{}',snapshot TEXT,payload TEXT,
                bindings TEXT NOT NULL DEFAULT '{}')""")
            db.execute("""CREATE TABLE IF NOT EXISTS autonomy_sources (
                role_id TEXT NOT NULL,source_id TEXT NOT NULL,revision INTEGER NOT NULL,
                definition TEXT NOT NULL,active INTEGER NOT NULL,
                PRIMARY KEY(role_id,source_id))""")
            role_columns = {row[1] for row in db.execute("PRAGMA table_info(autonomy_roles)")}
            for name, definition in (("initial_poll_id", "TEXT"),
                                     ("poll_ready_generation", "INTEGER NOT NULL DEFAULT 0"),
                                     ("last_poll_at", "REAL"),
                                     ("poll_not_before", "REAL NOT NULL DEFAULT 0"),
                                     ("poll_health", "TEXT NOT NULL DEFAULT '{}'")):
                if name not in role_columns:
                    db.execute(f"ALTER TABLE autonomy_roles ADD COLUMN {name} {definition}")
            if 'last_drift_at' not in role_columns:
                db.execute('ALTER TABLE autonomy_roles ADD COLUMN last_drift_at REAL')
            db.execute("""CREATE TABLE IF NOT EXISTS autonomy_human_runs (
                role_id TEXT NOT NULL,run_id TEXT NOT NULL,ended INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(role_id,run_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS autonomy_event_claims (
                role_id TEXT NOT NULL,event_key TEXT NOT NULL,tick_id TEXT NOT NULL,
                expires_at REAL,status TEXT NOT NULL,PRIMARY KEY(role_id,event_key))""")
            ensure_delivery_schema(db)
            from .channel_delivery import ensure_schema as ensure_channel_schema
            ensure_channel_schema(db)
            db.execute("""CREATE TABLE IF NOT EXISTS autonomy_budgets (
                role_id TEXT NOT NULL,day INTEGER NOT NULL,decisions INTEGER NOT NULL DEFAULT 0,
                drifts INTEGER NOT NULL DEFAULT 0,polls INTEGER NOT NULL DEFAULT 0,
                acks INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(role_id,day))""")
            retention.ensure_schema(db)
            ensure_source_validation_schema(db)
            ensure_verification_schema(db)
            now = self._now(db)
            # Preserve the hard interval when upgrading an existing ledger.
            # An admitted attempt consumes it even if its handover failed.
            db.execute("""UPDATE autonomy_roles SET last_poll_at=COALESCE(last_poll_at,
                (SELECT MAX(started_at) FROM autonomy_runs WHERE kind='autonomy_poll'
                 AND role_id=autonomy_roles.role_id))""")
            db.execute("UPDATE autonomy_roles SET poll_not_before=MAX(poll_not_before,COALESCE(last_poll_at+150,0))")
            # Process downtime never creates catch-up decisions. Original claims
            # will be recovered separately; no old model action is replayed.
            self._postpone_decisions(db, now)
            db.execute("UPDATE autonomy_roles SET next_poll_at=MAX(?,poll_not_before),human_busy=0,last_human_end=?,initial_poll_id=NULL,poll_ready_generation=0",
                       (now, now))
            db.execute("UPDATE autonomy_human_runs SET ended=1")
        self.source_verifications = SourceVerificationApplication(self)
        from .drift_skills import DriftSkillCatalog
        self.drift_skills = DriftSkillCatalog(self, workspace_getter)
        from .drift_workspace import DriftFiles
        self.drift_files = DriftFiles(self, self.drift_skills.workspace)
        from .meme_library import MemeLibrary
        self.memes = MemeLibrary(self)
        from .drift_delivery import DesktopDriftDelivery
        self.drift_delivery = DesktopDriftDelivery(self)
        from .targets import TargetGrants
        self.targets = TargetGrants(self, channel_host)
        from .channel_delivery import ChannelAutonomyDelivery
        self.channel_delivery = ChannelAutonomyDelivery(self)

    @contextmanager
    def _transaction(self):
        with self._gate, sqlite3.connect(self.sessions.database_path, timeout=.05) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _now(self, db):
        row = db.execute("SELECT value FROM autonomy_meta WHERE key='clock'").fetchone()
        now = max(float(self.clock()), float(row[0]) if row else 0)
        db.execute("INSERT OR REPLACE INTO autonomy_meta VALUES('clock',?)", (now,))
        return now

    def _require_role(self, role_id):
        if self.role_getter(role_id) is None:
            raise KeyError("role is unavailable")

    @staticmethod
    def _postpone_decisions(db, now):
        for row in db.execute('SELECT role_id,config FROM autonomy_roles').fetchall():
            config = AutonomyConfig.model_validate_json(row['config'])
            db.execute('UPDATE autonomy_roles SET next_at=? WHERE role_id=?',
                (now + config.decision_interval, row['role_id']))

    def effective_config(self, role_id, config):
        if not config.inheritRoleModel or not (config.activeDecisionEnabled or config.driftEnabled):
            return config
        model = self.role_model_getter(role_id)
        identifier = bindings.shape(model).get('id') if model is not None else None
        if identifier is None or self.model_getter(identifier) is None:
            raise ValueError('role model is unavailable')
        # Inheriting a chat model cannot retarget an existing vision grant.
        return AutonomyConfig.model_validate({**config.model_dump(),
            'decisionModelConfigurationId': identifier, 'driftModelConfigurationId': identifier})

    def _retains_grants_on_shutdown(self, role_id, config):
        if config.activeDecisionEnabled or config.driftEnabled:
            return False
        with self._transaction() as db:
            row = db.execute('SELECT config FROM autonomy_roles WHERE role_id=?', (role_id,)).fetchone()
        if row is None:
            return False
        previous = AutonomyConfig.model_validate_json(row[0])
        if not (previous.activeDecisionEnabled or previous.driftEnabled):
            return False
        ignored = {'revision', 'activeDecisionEnabled', 'driftEnabled', 'inheritRoleModel',
            'consentRevision', *ProactivePreferences.model_fields}
        return previous.model_dump(exclude=ignored) == config.model_dump(exclude=ignored)

    def configure(self, role_id: str, config: AutonomyConfig, *, expected_revision: int, target_selections=None):
        self._require_role(role_id)
        if self._closed:
            raise RuntimeError("autonomy application is closed")
        config = self.effective_config(role_id, config)
        # Turning off must remain possible after a model/source/target expires.
        # Only an enabled-to-disabled transition may retain unchanged grants;
        # ordinary saves and re-enabling still run all dependency checks.
        retain_inactive = self._retains_grants_on_shutdown(role_id, config)
        for enabled, identifier in ((config.activeDecisionEnabled, config.decisionModelConfigurationId),
                                    (config.driftEnabled, config.driftModelConfigurationId)):
            if enabled and (identifier is None or self.model_getter(identifier) is None):
                raise ValueError("explicit saved model is unavailable")
        if config.driftMeme.enabled and not retain_inactive:
            from ..ai.model_adapter_registry import ModelAdapterRegistry
            model = self.model_getter(config.driftMeme.visionModelConfigurationId)
            if model is None or not ModelAdapterRegistry().supports_images(model):
                raise ValueError('meme purpose requires an explicitly saved image-capable Drift model')
        for source in (() if retain_inactive else config.sourceGrants):
            if source.enabled:
                server = self.mcp_host.store.get(source.serverId) if self.mcp_host else None
                if (server is None or not server.enabled or role_id not in server.role_ids
                        or (server.revision, server.grant_id) != (source.configRevision, source.grantRevision)):
                    raise ValueError("enabled source requires its exact saved MCP grant")
        for grant in (() if retain_inactive else config.driftMcpGrants):
            server = self.mcp_host.store.get(grant.serverId) if self.mcp_host else None
            if (server is None or not server.enabled or role_id not in server.role_ids
                    or (server.revision, server.grant_id, server.risk) != (grant.configRevision, grant.grantRevision, grant.risk)):
                raise ValueError('Drift MCP requires the exact saved role/server/risk grant')
        with self._transaction() as db:
            old = db.execute("SELECT * FROM autonomy_roles WHERE role_id=?", (role_id,)).fetchone()
            revision = old["revision"] if old else 0
            if expected_revision != revision:
                raise ValueError("autonomy configuration revision changed")
            if not retain_inactive or target_selections is not None:
                self.targets.save(db, role_id, config, expected_revision, target_selections)
            now = self._now(db)
            sources = []
            for source in config.sourceGrants:
                definition = json.dumps(source.model_dump(exclude={"revision", "ackProofId"}), sort_keys=True, separators=(",", ":"))
                prior = db.execute("SELECT * FROM autonomy_sources WHERE role_id=? AND source_id=?",
                                   (role_id, source.sourceId)).fetchone()
                source_revision = prior["revision"] if prior and prior["active"] and prior["definition"] == definition else (prior["revision"] + 1 if prior else 1)
                sources.append(source.model_copy(update={"revision": source_revision}))
                db.execute("INSERT OR REPLACE INTO autonomy_sources VALUES(?,?,?,?,1)",
                           (role_id, source.sourceId, source_revision, definition))
            known = {source.sourceId for source in sources}
            for prior in db.execute("SELECT source_id FROM autonomy_sources WHERE role_id=? AND active=1", (role_id,)).fetchall():
                if prior[0] not in known:
                    db.execute("UPDATE autonomy_sources SET active=0,revision=revision+1 WHERE role_id=? AND source_id=?", (role_id, prior[0]))
            from .drift_skills import authoring_revision
            previous = AutonomyConfig.model_validate_json(old['config']).driftAuthoring if old else AutonomyConfig().driftAuthoring
            authoring = authoring_revision(config.driftAuthoring, previous)
            previous_meme = AutonomyConfig.model_validate_json(old['config']).driftMeme if old else AutonomyConfig().driftMeme
            requested_meme = config.driftMeme
            if (requested_meme.enabled and not previous_meme.enabled and old is not None
                    and (old['cleanup_pending'] or db.execute('SELECT 1 FROM autonomy_runs WHERE role_id=? AND runtime_acknowledged=0', (role_id,)).fetchone())):
                raise MemePurposeConflict('meme purpose cannot be re-enabled until the original owned resources finish cleanup')
            if requested_meme.managedEpoch and requested_meme.managedEpoch != previous_meme.managedEpoch:
                raise ValueError('meme managed epoch is allocated by the host')
            unchanged_meme = requested_meme.model_dump(exclude={'revision', 'managedEpoch'}) == previous_meme.model_dump(exclude={'revision', 'managedEpoch'})
            meme = previous_meme if unchanged_meme else requested_meme.model_copy(update={
                'managedEpoch': uuid.uuid4().hex if requested_meme.enabled and not previous_meme.enabled else previous_meme.managedEpoch,
                'revision': previous_meme.revision + 1})
            if meme.enabled and not retain_inactive:
                self.memes.adopt(db, role_id, meme)
            saved = config.model_copy(update={"revision": revision + 1, "sourceGrants": sources, 'driftAuthoring': authoring, 'driftMeme': meme})
            db.execute("""INSERT INTO autonomy_roles
                (role_id,revision,generation,config,next_at,next_poll_at,last_human_end)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(role_id) DO UPDATE SET
                revision=excluded.revision,generation=excluded.generation,
                config=excluded.config,next_at=excluded.next_at,next_poll_at=excluded.next_poll_at,
                initial_poll_id=NULL,poll_ready_generation=0,admission_error=''""",
                (role_id, revision + 1, (old["generation"] if old else 0) + 1,
                 json.dumps(saved.model_dump(mode="json")), now + saved.decision_interval,
                 max(now, old["poll_not_before"]) if old else now, now))
            if old:
                db.execute("UPDATE autonomy_roles SET cleanup_pending=EXISTS(SELECT 1 FROM autonomy_runs WHERE role_id=? AND runtime_acknowledged=0) WHERE role_id=?", (role_id, role_id))
            retention.check_capacity(db, role_id)
        if old:
            self._kick_cleanup(role_id)
        return self.query(role_id)

    def query(self, role_id: str):
        self._require_role(role_id)
        source_verifications = self.source_verifications.list(role_id)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM autonomy_roles WHERE role_id=?", (role_id,)).fetchone()
            config = AutonomyConfig.model_validate_json(row['config']) if row else AutonomyConfig()
            return {"config": config.model_dump(mode='json'),
                    'targetGrants': self.targets.public(db, role_id, config),
                    'deliveries': self._public_deliveries(db, role_id),
                    "nextDecisionAt": row["next_at"] if row else None,
                    "cleanupPending": bool(row["cleanup_pending"]) if row else False,
                    "humanBusy": bool(row["human_busy"]) if row else False,
                    "admissionError": row["admission_error"] if row else "",
                    "sourceVerifications": source_verifications,
                    "acknowledgements": [{"id": item["ack_id"], "sourceId": item["source_id"],
                        "status": item["status"], "attempts": item["attempts"],
                        "decisionAt": item["decision_at"], "expiresAt": item["expires_at"],
                        "permanent": bool(item["permanent"]), "reason": item["reason"]}
                        for item in db.execute("SELECT * FROM autonomy_ack_outbox WHERE role_id=? ORDER BY decision_at DESC,ack_id LIMIT 100", (role_id,))],
                    "runs": [self._public_run(item, db) for item in db.execute(
                        "SELECT * FROM autonomy_runs WHERE role_id=? ORDER BY started_at DESC,tick_id DESC LIMIT 100", (role_id,))]}

    @staticmethod
    def _public_deliveries(db, role_id):
        from .channel_delivery import public_deliveries
        return public_deliveries(db, role_id)

    @staticmethod
    def _public_run(row, db):
        snapshot = json.loads(row["snapshot"]) if row["snapshot"] else {}
        poll = json.loads(row["payload"] or "{}") if row["kind"] == "autonomy_poll" else {}
        from .channel_delivery import public_deliveries
        deliveries = public_deliveries(db, row['role_id'], tick_id=row['tick_id'])
        return {"id": row["tick_id"], "kind": row["kind"], "mode": row["mode"],
                "status": row["status"], "startedAt": row["started_at"],
                "timeoutSeconds": OPERATION_TIMEOUTS[row["kind"]], "origin": AutonomyOrigin.restore(row["origin"]).public(),
                "taskId": row["task_id"], "error": row["error"],
                "runtimeAcknowledged": bool(row["runtime_acknowledged"]), "sources": snapshot.get("sources", poll.get("sources", [])),
                "delivery": deliveries[0] if deliveries else None}

    def _authorized(self, origin):
        try:
            with self._transaction() as db:
                return self._authorized_in(db, origin)
        except (sqlite3.Error, OSError, ValueError):
            return False

    def _authorized_in(self, db, origin):
        if self._closed or not self.enabled() or self.role_getter(origin.role_id) is None:
            return False
        config = db.execute("SELECT revision,generation,human_busy FROM autonomy_roles WHERE role_id=?", (origin.role_id,)).fetchone()
        run = db.execute("SELECT origin,kind,bindings,frozen_config FROM autonomy_runs WHERE tick_id=?", (origin.tick_id,)).fetchone()
        return bool(config and run and not config["human_busy"]
            and (config["revision"], config["generation"]) == (origin.grant_revision, origin.generation)
            and run["origin"] == origin.serialized()
            and self.source_permissions_current(origin.role_id, AutonomyConfig.model_validate_json(run["frozen_config"]).sourceGrants)
            and ('drift' not in origin.allowed_modes or self.drift_permissions_current(origin.role_id,
                AutonomyConfig.model_validate_json(run['frozen_config']).driftMcpGrants))
            and (run['kind'] != 'autonomy_tick' or self.targets.current(db, origin))
            and (run["kind"] != "autonomy_tick" or bindings.current(self, origin.role_id, json.loads(run["bindings"]))))

    def source_permissions_current(self, role_id, sources):
        for source in sources:
            if not source.enabled:
                continue
            server = self.mcp_host.store.get(source.serverId) if self.mcp_host else None
            if (server is None or not server.enabled or role_id not in server.role_ids
                    or (server.revision, server.grant_id) != (source.configRevision, source.grantRevision)):
                return False
        return True

    def frozen_bindings(self, origin):
        with self._transaction() as db:
            row = db.execute("SELECT bindings FROM autonomy_runs WHERE tick_id=?", (origin.tick_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def frozen_configuration(self, origin):
        with self._transaction() as db:
            row = db.execute('SELECT frozen_config FROM autonomy_runs WHERE tick_id=?', (origin.tick_id,)).fetchone()
            if row is None:
                raise PermissionError('autonomous run is unavailable')
            return AutonomyConfig.model_validate_json(row[0])

    def drift_permissions_current(self, role_id, grants):
        for grant in grants:
            server = self.mcp_host.store.get(grant.serverId) if self.mcp_host else None
            if (server is None or not server.enabled or role_id not in server.role_ids
                    or (server.revision, server.grant_id, server.risk) != (grant.configRevision, grant.grantRevision, grant.risk)):
                return False
        return True

    def tool_permitted(self, origin, name):
        if not self._authorized(origin):
            return False
        role = self.frozen_bindings(origin).get("role") or {}
        policy = role.get("agentConfig", {})
        if name == "recall_memory":
            return bool(policy.get("memoryRecall", {}).get("enabled"))
        return name in bindings.local_tool_names(role)

    async def _owned(self, function, *args, **kwargs):
        pending = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        await finish_cleanup(pending)
        return pending.result()

    def accept_human_run(self, role_id: str, run_id: str):
        """Seal publication synchronously; network cleanup never blocks intake."""
        self._require_role(role_id)
        if not run_id or len(run_id) > 256:
            raise ValueError("a genuine host run identity is required")
        with self._transaction() as db:
            exists = db.execute("SELECT 1 FROM autonomy_roles WHERE role_id=?", (role_id,)).fetchone()
            if exists is None:
                return lambda: None
            inserted = db.execute("INSERT OR IGNORE INTO autonomy_human_runs(role_id,run_id) VALUES(?,?)", (role_id, run_id)).rowcount
            if inserted:
                db.execute("UPDATE autonomy_roles SET generation=generation+1,human_busy=human_busy+1,cleanup_pending=1 WHERE role_id=?", (role_id,))
        self._kick_cleanup(role_id)
        return lambda: self.finish_human_run(role_id, run_id)

    def finish_human_run(self, role_id: str, run_id: str):
        try:
            with self._transaction() as db:
                changed = db.execute("UPDATE autonomy_human_runs SET ended=1 WHERE role_id=? AND run_id=? AND ended=0", (role_id, run_id)).rowcount
                if changed:
                    db.execute("UPDATE autonomy_roles SET human_busy=MAX(0,human_busy-1),last_human_end=? WHERE role_id=?", (self._now(db), role_id))
            self._human_finish_pending.discard((role_id, run_id))
        except sqlite3.Error:
            self._human_finish_pending.add((role_id, run_id))

    def stop(self, role_id):
        self._require_role(role_id)
        with self._transaction() as db:
            db.execute("UPDATE autonomy_roles SET generation=generation+1,cleanup_pending=1 WHERE role_id=?", (role_id,))
        self._kick_cleanup(role_id)
        return self.query(role_id)

    def invalidate_model(self, configuration_id):
        with self._transaction() as db:
            affected = []
            for row in db.execute("SELECT role_id,config FROM autonomy_roles"):
                config = AutonomyConfig.model_validate_json(row["config"])
                identifiers = {config.decisionModelConfigurationId, config.driftModelConfigurationId}
                if config.inheritRoleModel:
                    model = bindings.shape(self.role_model_getter(row['role_id']))
                    identifiers = {model.get('id')} if model else set()
                    for run in db.execute('SELECT bindings FROM autonomy_runs WHERE role_id=? AND runtime_acknowledged=0', (row['role_id'],)):
                        identifiers.update(json.loads(run['bindings']).get('models', {}))
                if configuration_id in identifiers:
                    db.execute("UPDATE autonomy_roles SET generation=generation+1,cleanup_pending=1 WHERE role_id=?", (row["role_id"],))
                    affected.append(row["role_id"])
        for role_id in affected:
            self._kick_cleanup(role_id)

    @property
    def has_owned_work(self):
        return bool(self._handovers or self._executors or self._drift_owners or self._worker is not None
                    or any(not task.done() for task in self._cleanup_tasks.values())
                    or self.source_verifications.has_owned_work)

    async def begin_role_deletion(self, role_id):
        # The host has already closed ordinary role access. Deletion still
        # owns the original resources and must be able to seal and join them.
        with self._transaction() as db:
            db.execute("UPDATE autonomy_roles SET generation=generation+1,cleanup_pending=1 WHERE role_id=?", (role_id,))
        await self._drain_role(role_id)
        await self.recover()
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM autonomy_runs WHERE role_id=? AND runtime_acknowledged=0", (role_id,)).fetchone():
                raise RuntimeError("role autonomous tasks still require recovery")
            row = db.execute("SELECT cleanup_pending FROM autonomy_roles WHERE role_id=?", (role_id,)).fetchone()
            if row and row[0]:
                raise RuntimeError("role autonomous process cleanup remains pending")

    async def suspend_role(self, role_id):
        # The same generation fence and owned cleanup apply to app suspension;
        # retain the role's strategy, grants, receipts and recovery journals.
        await self.begin_role_deletion(role_id)

    def delete_role(self, role_id):
        self.source_verifications.delete_role(role_id)
        with self._transaction() as db:
            row = db.execute("SELECT revision FROM autonomy_roles WHERE role_id=?", (role_id,)).fetchone()
            if row:
                config = AutonomyConfig(revision=row[0] + 1)
                db.execute("UPDATE autonomy_roles SET revision=revision+1,generation=generation+1,config=?,human_busy=0 WHERE role_id=?",
                           (config.model_dump_json(), role_id))
                db.execute("UPDATE autonomy_runs SET snapshot=NULL,bindings='{}',frozen_config='{}',runtime_result=NULL WHERE role_id=?", (role_id,))
                db.execute("UPDATE autonomy_ack_outbox SET status='revoked',source_grant='{}' WHERE role_id=? AND status NOT IN ('acknowledged','unsupported','revoked','expired')", (role_id,))
                db.execute("UPDATE autonomy_sources SET active=0,revision=revision+1 WHERE role_id=?", (role_id,))
                db.execute("DELETE FROM autonomy_human_runs WHERE role_id=?", (role_id,))
        self._cleanup_tasks.pop(role_id, None)

    def start(self):
        if self._worker is None or self._worker.done():
            self._closed = False
            with self._transaction() as db:
                now = self._now(db)
                self._postpone_decisions(db, now)
                db.execute("UPDATE autonomy_roles SET next_poll_at=MAX(?,poll_not_before),initial_poll_id=NULL,poll_ready_generation=0", (now,))
            self._worker = asyncio.create_task(self._clock_loop())

    async def _clock_loop(self):
        while not self._closed:
            try:
                await self.on_clock()
            except (sqlite3.Error, OSError, RuntimeError, PermissionError):
                self.errors[:] = [*self.errors[-49:], "autonomous clock/recovery deferred; owned duties retained"]
            await asyncio.sleep(1)

    def _kick_cleanup(self, role_id):
        existing = self._cleanup_tasks.get(role_id)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # The durable pending flag is sufficient when called outside the
            # host loop; the next recover owns cancellation and join.
            return
        task = loop.create_task(self._drain_role(role_id))
        self._cleanup_tasks[role_id] = task
        def observe(done):
            if not done.cancelled():
                done.exception()
        task.add_done_callback(observe)

    async def _drain_role(self, role_id):
        await self.source_verifications.invalidate(role_id)
        with self._transaction() as db:
            state = db.execute("SELECT generation FROM autonomy_roles WHERE role_id=?", (role_id,)).fetchone()
            rows = db.execute("SELECT * FROM autonomy_runs WHERE role_id=? AND runtime_acknowledged=0", (role_id,)).fetchall()
        for row in rows:
            origin = AutonomyOrigin.restore(row["origin"])
            if state and origin.generation == state[0] and not self._closed:
                continue
            executor = self._executors.get(origin.tick_id)
            if executor is not None:
                executor.cancel()
            # A handover can be in flight on another owned thread. Keep the
            # duty instead of interpreting a missing taskId as non-execution.
            if origin.tick_id in self._handovers:
                return
            identifier = row["task_id"] or await self._owned(self._find_task, origin)
            if identifier:
                await self._owned(self.tasks.stop, identifier, origin.task_origin())
                if executor is not None and not await self._owned(executor.finished.wait, 10):
                    raise RuntimeError("autonomous owner cleanup remains pending")
        if self.mcp_host is not None and self.mcp_host.autonomy_cleanup_pending(role_id):
            return
        with self._transaction() as db:
            # A new acceptance may have sealed another generation while we
            # joined. Its own cleanup task must complete before reopening.
            current = db.execute("SELECT generation FROM autonomy_roles WHERE role_id=?", (role_id,)).fetchone()
            if current and state and current[0] == state[0] and not self.drift_files.pending(role_id, db):
                db.execute("UPDATE autonomy_roles SET cleanup_pending=0 WHERE role_id=?", (role_id,))

    async def _launch(self, origin):
        self._handovers.add(origin.tick_id)
        try:
            await self._owned(self._submit, origin)
        finally:
            self._handovers.discard(origin.tick_id)

    async def on_clock(self):
        if self._closed:
            return 0
        await self.recover()
        if not self.enabled():
            return 0
        await self._schedule_polls()
        admitted = []
        with self._transaction() as db:
            now = self._now(db)
            retention.prune(db, now)
            rows = db.execute("SELECT * FROM autonomy_roles WHERE next_at<=?", (now,)).fetchall()
            for row in rows:
                config = AutonomyConfig.model_validate_json(row["config"])
                if db.execute("SELECT 1 FROM autonomy_runs WHERE role_id=? AND kind='autonomy_poll' AND runtime_acknowledged=0",
                              (row["role_id"],)).fetchone():
                    # A newly due refresh belongs to this source observation,
                    # including its resource cleanup and durable Runtime ACK.
                    continue
                if (any(source.enabled and source.pollTool for source in config.sourceGrants)
                        and row["poll_ready_generation"] != row["generation"]):
                    # The first poll must actually finish, not just win a
                    # submit race with the snapshot. Keep the original due
                    # time while this prerequisite is still in flight.
                    continue
                db.execute("UPDATE autonomy_roles SET next_at=? WHERE role_id=?", (now + config.decision_interval, row["role_id"]))
                if (not (config.activeDecisionEnabled or config.driftEnabled) or row["human_busy"]
                        or now - row["last_human_end"] < 600 or row["cleanup_pending"]
                        or self.role_getter(row["role_id"]) is None
                        or self.mcp_host is not None and self.mcp_host.autonomy_cleanup_pending(row["role_id"])):
                    continue
                if db.execute("SELECT 1 FROM autonomy_drift_write_journal WHERE role_id=? AND status='pending'", (row['role_id'],)).fetchone():
                    db.execute("UPDATE autonomy_roles SET cleanup_pending=1,admission_error='长期文件预留仍待核对' WHERE role_id=?", (row['role_id'],))
                    continue
                if db.execute("SELECT 1 FROM autonomy_runs WHERE role_id=? AND kind='autonomy_tick' AND runtime_acknowledged=0",
                              (row["role_id"],)).fetchone():
                    continue
                try:
                    config = self.effective_config(row['role_id'], config)
                except ValueError:
                    db.execute("UPDATE autonomy_roles SET admission_error='角色模型或表情维护授权已变化，请检查设置' WHERE role_id=?", (row['role_id'],))
                    continue
                day = int(now // 86400)
                db.execute("INSERT OR IGNORE INTO autonomy_budgets(role_id,day) VALUES(?,?)", (row["role_id"], day))
                quota = db.execute("SELECT decisions FROM autonomy_budgets WHERE role_id=? AND day=?", (row["role_id"], day)).fetchone()[0]
                total = db.execute("SELECT COALESCE(SUM(decisions),0) FROM autonomy_budgets WHERE day=?", (day,)).fetchone()[0]
                if quota >= 24 or total >= 120:
                    db.execute("UPDATE autonomy_roles SET admission_error='今日自主决策次数已用完' WHERE role_id=?", (row["role_id"],))
                    continue
                modes = tuple(mode for mode, enabled in (("active_decision", config.activeDecisionEnabled), ("drift", config.driftEnabled)) if enabled)
                origin = AutonomyOrigin(row["role_id"], uuid.uuid4().hex, row["revision"], row["generation"], modes)
                try:
                    retention.reserve(db, origin.role_id, origin.tick_id, "autonomy_tick", now)
                    self.targets.freeze(db, origin, config)
                except PermissionError:
                    db.execute('DELETE FROM autonomy_storage_reservations WHERE operation_id=?', (origin.tick_id,))
                    db.execute("UPDATE autonomy_roles SET admission_error='原自主目标许可已变化，请重新选择' WHERE role_id=?", (origin.role_id,))
                    continue
                except retention.StorageLimitError:
                    db.execute("UPDATE autonomy_roles SET admission_error='自主存储额度已满；待处理记录仍保留' WHERE role_id=?", (origin.role_id,))
                    continue
                frozen = bindings.freeze(self, origin.role_id, config)
                db.execute("INSERT INTO autonomy_runs(tick_id,role_id,origin,started_at,frozen_config,bindings) VALUES(?,?,?,?,?,?)",
                           (origin.tick_id, origin.role_id, origin.serialized(), now, config.model_dump_json(), json.dumps(frozen, ensure_ascii=False)))
                db.execute("UPDATE autonomy_budgets SET decisions=decisions+1 WHERE role_id=? AND day=?", (origin.role_id, day))
                db.execute("UPDATE autonomy_roles SET admission_error='' WHERE role_id=?", (origin.role_id,))
                admitted.append(origin)
        for origin in admitted:
            await self._launch(origin)
        return len(admitted)

    async def _schedule_polls(self):
        admitted = []
        with self._transaction() as db:
            now = self._now(db)
            for row in db.execute("SELECT * FROM autonomy_roles WHERE next_poll_at<=? AND poll_not_before<=?", (now, now)).fetchall():
                db.execute("UPDATE autonomy_roles SET next_poll_at=? WHERE role_id=?", (now + 150, row["role_id"]))
                config = AutonomyConfig.model_validate_json(row["config"])
                if (not (config.activeDecisionEnabled or config.driftEnabled) or row["human_busy"]
                        or row["cleanup_pending"] or self.role_getter(row["role_id"]) is None
                        or not any(source.enabled and source.pollTool for source in config.sourceGrants)
                        or self.mcp_host is None or self.mcp_host.autonomy_cleanup_pending(row["role_id"])):
                    continue
                if db.execute("SELECT 1 FROM autonomy_runs WHERE role_id=? AND kind='autonomy_poll' AND runtime_acknowledged=0", (row["role_id"],)).fetchone():
                    continue
                day = int(now // 86400)
                db.execute("INSERT OR IGNORE INTO autonomy_budgets(role_id,day) VALUES(?,?)", (row["role_id"], day))
                if db.execute("SELECT polls FROM autonomy_budgets WHERE role_id=? AND day=?", (row["role_id"], day)).fetchone()[0] >= 576:
                    continue
                origin = AutonomyOrigin(row["role_id"], uuid.uuid4().hex, row["revision"], row["generation"], ("source_poll",))
                try:
                    retention.reserve(db, origin.role_id, origin.tick_id, "autonomy_poll", now)
                except retention.StorageLimitError:
                    db.execute("UPDATE autonomy_roles SET admission_error='自主存储额度已满；轮询暂缓' WHERE role_id=?", (origin.role_id,))
                    continue
                states = [{"sourceId": source.sourceId, "revision": source.revision,
                           "channel": source.channel, "observedAt": now, "truncated": False,
                           "status": "incomplete", "error": "source poll has not finished"}
                          for source in config.sourceGrants if source.enabled and source.pollTool]
                payload = {"pollAttempted": False, "sources": states}
                health = {"generation": origin.generation, "tickId": origin.tick_id, "sources": states}
                db.execute("INSERT INTO autonomy_runs(tick_id,role_id,origin,kind,mode,started_at,frozen_config,payload) VALUES(?,?,?,'autonomy_poll','source_poll',?,?,?)",
                           (origin.tick_id, origin.role_id, origin.serialized(), now, row["config"], json.dumps(payload)))
                db.execute("UPDATE autonomy_budgets SET polls=polls+1 WHERE role_id=? AND day=?", (origin.role_id, day))
                db.execute("UPDATE autonomy_roles SET last_poll_at=?,poll_not_before=?,poll_health=? WHERE role_id=?",
                           (now, now + 150, json.dumps(health), origin.role_id))
                if row["poll_ready_generation"] != row["generation"]:
                    db.execute("UPDATE autonomy_roles SET initial_poll_id=? WHERE role_id=?", (origin.tick_id, origin.role_id))
                admitted.append(origin)
        for origin in admitted:
            await self._launch(origin)

    def _submit(self, origin):
        executor = AutonomyExecutor(self, origin)
        self._executors[origin.tick_id] = executor
        with self._transaction() as db:
            kind = db.execute("SELECT kind FROM autonomy_runs WHERE tick_id=?", (origin.tick_id,)).fetchone()[0]
        try:
            task_id = self.tasks.submit(origin.task_origin(), executor, kind=kind,
                budget=TaskBudget(timeout_seconds=OPERATION_TIMEOUTS[kind], log_bytes=65536, output_chars=16000),
                submission_key=origin.tick_id, retain_until_ack=True, completion=False,
                authorized=lambda: self._authorized(origin), description="role autonomous tick")
        except Exception:
            # Unknown acceptance retains the original key and cleanup duty.
            # Recovery only queries this identity; it never resubmits a model.
            try:
                self._find_task(origin)
            except (PermissionError, KeyError, sqlite3.Error):
                pass
            return
        with self._transaction() as db:
            db.execute("UPDATE autonomy_runs SET task_id=?,launch_pending=0 WHERE tick_id=?", (task_id, origin.tick_id))

    def _find_task(self, origin):
        with self._transaction() as db:
            kind = db.execute("SELECT kind FROM autonomy_runs WHERE tick_id=?", (origin.tick_id,)).fetchone()[0]
        identifier = self.tasks.find_submission(origin.task_origin(), kind=kind, submission_key=origin.tick_id)
        if identifier is not None:
            with self._transaction() as db:
                db.execute("UPDATE autonomy_runs SET task_id=?,launch_pending=0 WHERE tick_id=?",
                           (identifier, origin.tick_id))
            return identifier
        return None

    async def _execute_operation(self, origin, execution, cancelled):
        with self._transaction() as db:
            kind = db.execute("SELECT kind FROM autonomy_runs WHERE tick_id=?", (origin.tick_id,)).fetchone()[0]
        if kind == "autonomy_poll":
            return await self._execute_poll(origin, execution)
        if kind == "autonomy_ack":
            from .acknowledgements import execute_ack
            return await execute_ack(self, origin, execution)
        return await self._execute_tick(origin, execution, cancelled)

    async def _finish_operation(self, origin):
        while True:
            failed = False
            owner = self._drift_owners.get(origin.tick_id)
            if owner is not None:
                try:
                    await owner.close()
                    self._drift_owners.pop(origin.tick_id, None)
                except Exception:
                    failed = True
            try:
                if self.mcp_host is not None:
                    await self.mcp_host.close_autonomy_run(origin.tick_id)
            except Exception:
                failed = True
            if not failed:
                return
            else:
                try:
                    with self._transaction() as db:
                        db.execute("UPDATE autonomy_roles SET cleanup_pending=1 WHERE role_id=?", (origin.role_id,))
                except sqlite3.Error:
                    pass  # The host registry independently keeps this duty.
                await asyncio.sleep(.25)

    async def _execute_poll(self, origin, execution):
        if not self._authorized(origin):
            raise PermissionError("source poll grant was revoked")
        with self._transaction() as db:
            row = db.execute("SELECT frozen_config,started_at FROM autonomy_runs WHERE tick_id=?", (origin.tick_id,)).fetchone()
            config = AutonomyConfig.model_validate_json(row[0])
            observed_at = self._now(db)
            remaining = min(execution.deadline - time.monotonic(), row[1] + 120 - observed_at)
            sources = [source for source in config.sourceGrants if source.enabled and source.pollTool]
            states = [{"sourceId": source.sourceId, "revision": source.revision,
                       "channel": source.channel, "observedAt": observed_at, "truncated": False,
                       "status": "incomplete", "error": "source poll did not finish"} for source in sources]
            payload = {"pollAttempted": True, "sources": states}
            db.execute("UPDATE autonomy_runs SET status='running',payload=? WHERE tick_id=?",
                       (json.dumps(payload), origin.tick_id))
        grants = []
        for source in sources:
            name, digest = source.pollTool, source.pollDefinitionHash
            if name is None or digest is None:
                raise PermissionError("source poll definition is unavailable")
            grants.append(AutonomyMcpGrant(source.serverId, source.configRevision, source.grantRevision, ((name, digest),)))
        bundle = self.mcp_host.freeze_autonomy(origin, tuple(grants), authorized=lambda: self._authorized(origin))
        try:
            async with asyncio.timeout(max(0, remaining)):
                # A shared poll operation is invoked once even when a server
                # exposes several event channels through that operation.
                seen = set()
                for source in sources:
                    key = (source.serverId, source.pollTool)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        response = await bundle.call(source.serverId, source.pollTool, {})
                        succeeded = not response.get("isError")
                    except (ValueError, PermissionError, RuntimeError, TimeoutError, OSError):
                        succeeded = False
                    if not self._authorized(origin):
                        raise PermissionError("source poll grant was revoked")
                    # Independent sources still get their one approved call;
                    # shared server/tool channels reuse this same outcome.
                    for candidate, state in zip(sources, states):
                        if (candidate.serverId, candidate.pollTool) == key:
                            state.update(status="ok" if succeeded else "failed")
                            if succeeded:
                                state.pop("error", None)
                            else:
                                state["error"] = "source poll failed"
                    with self._transaction() as db:
                        db.execute("UPDATE autonomy_runs SET payload=? WHERE tick_id=?", (json.dumps(payload), origin.tick_id))
            if any(state["status"] != "ok" for state in states):
                raise ValueError("one or more source polls failed")
            with self._transaction() as db:
                db.execute("UPDATE autonomy_runs SET status='polled' WHERE tick_id=?", (origin.tick_id,))
            return TaskOutcome("succeeded", output_tail="authorized sources polled")
        finally:
            await bundle.close()

    async def _execute_tick(self, origin, execution, cancelled):
        if not self._authorized(origin):
            raise PermissionError("autonomy grant was revoked")
        with self._transaction() as db:
            run = db.execute("SELECT * FROM autonomy_runs WHERE tick_id=?", (origin.tick_id,)).fetchone()
            config = AutonomyConfig.model_validate_json(run["frozen_config"])
            role_state = db.execute("SELECT content_rotation,poll_health FROM autonomy_roles WHERE role_id=?", (origin.role_id,)).fetchone()
            rotation, poll_health = role_state[0], json.loads(role_state[1])
            remaining = min(execution.deadline - time.monotonic(), run["started_at"] + 120 - self._now(db))
            observed_at = self._now(db)
        if remaining <= 0:
            raise TimeoutError("snapshot admission deadline expired")
        sources = [source for source in config.sourceGrants if source.enabled]
        bundle = None
        try:
            async with asyncio.timeout(remaining):
                mounts = tuple(AutonomyMcpGrant(grant.serverId, grant.configRevision, grant.grantRevision,
                    tuple((operation.name, operation.definitionHash) for operation in grant.operations))
                    for grant in config.driftMcpGrants) if 'drift' in origin.allowed_modes else ()
                if sources or mounts:
                    if self.mcp_host is None:
                        raise PermissionError("autonomous MCP host is unavailable")
                    grants = tuple(AutonomyMcpGrant(source.serverId, source.configRevision, source.grantRevision,
                        ((source.getTool, source.getDefinitionHash),)) for source in sources)
                    bundle = self.mcp_host.freeze_autonomy(origin, grants, authorized=lambda: self._authorized(origin), mount_grants=mounts)
                snapshot = await collect_snapshot(bundle, sources, observed_at=observed_at,
                    eligible=lambda grant, identifier: self._event_eligible(origin.role_id, grant, identifier),
                    rotation=rotation, fetch_body=(lambda url: self.fetch_body(origin, url)) if self.fetch_body else None,
                    content_limit=config.agentContentLimit, web_fetch_max_chars=config.agentWebFetchMaxChars)
            health = {(state["sourceId"], state["revision"]): state
                      for state in poll_health.get("sources", [])} if poll_health.get("generation") == origin.generation else {}
            polled = {(source.sourceId, source.revision) for source in sources if source.pollTool}
            for state in snapshot["sources"]:
                key = (state["sourceId"], state["revision"])
                if key in polled and health.get(key, {}).get("status") != "ok":
                    # A successful empty get cannot turn a failed refresh into
                    # trusted emptiness. Existing candidates remain usable by
                    # A01; only the empty-source/Drift inference is withheld.
                    state.update(status="failed", error="source poll failed or incomplete")
                    snapshot["trustedEmpty"] = False
            with self._transaction() as db:
                retention.store_snapshot(db, origin.tick_id, snapshot, self._now(db))
                db.execute("UPDATE autonomy_roles SET content_rotation=content_rotation+? WHERE role_id=?",
                           (len(snapshot["contents"]), origin.role_id))
            if snapshot["alerts"] or snapshot["contents"]:
                if "active_decision" not in origin.allowed_modes:
                    status = "decision_disabled"
                else:
                    from .turn import run_decision
                    return await run_decision(self, origin, execution, cancelled, config, snapshot)
            elif not snapshot["trustedEmpty"]:
                status = "sources_unavailable"
            elif "drift" in origin.allowed_modes:
                from .drift import run_drift
                return await run_drift(self, origin, execution, cancelled, config, snapshot, bundle=bundle)
            else:
                status = "no_events"
            with self._transaction() as db:
                db.execute("UPDATE autonomy_runs SET mode='none',status=? WHERE tick_id=?", (status, origin.tick_id))
            return TaskOutcome("succeeded" if status in {"no_events", "decision_disabled"} else "failed", output_tail=status)
        finally:
            if bundle is not None:
                await bundle.close()

    @staticmethod
    def _event_key(grant, identifier):
        import hashlib
        fields = [grant.serverId, grant.configRevision, grant.eventDomain, grant.channel, identifier]
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()

    def _event_eligible(self, role_id, grant, identifier):
        with self._transaction() as db:
            now = self._now(db)
            occupied = db.execute("SELECT 1 FROM autonomy_event_claims WHERE role_id=? AND event_key=? AND (expires_at IS NULL OR expires_at>?)",
                                  (role_id, self._event_key(grant, identifier), now)).fetchone()
            return occupied is None

    def _abort_tick(self, origin, status):
        with self._transaction() as db:
            db.execute("UPDATE autonomy_runs SET status=?,error=? WHERE tick_id=? AND status IN ('preparing','running')",
                       ("aborted", status, origin.tick_id))

    async def recover(self):
        try:
            await self._recover()
        except sqlite3.OperationalError as error:
            # Every phase, including ACK scheduling and local projections, uses
            # the same short connection timeout. A busy read is no evidence of
            # an absent operation. Executors keep their independent finally
            # cleanup duty; this path never discards an owner or creates a key.
            if (getattr(error, 'sqlite_errorcode', 0) & 0xFF) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise

    async def _recover(self):
        await self.source_verifications.recover()
        from .drift_delivery import reconcile_receipts
        await reconcile_receipts(self)
        await self.channel_delivery.recover()
        for role_id, run_id in tuple(self._human_finish_pending):
            self.finish_human_run(role_id, run_id)
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM autonomy_runs WHERE runtime_acknowledged=0").fetchall()
            pending_roles = [row[0] for row in db.execute("SELECT role_id FROM autonomy_roles WHERE cleanup_pending=1")]
        if not self._closed:
            for role_id in pending_roles:
                self._kick_cleanup(role_id)
        for row in rows:
            origin = AutonomyOrigin.restore(row["origin"])
            try:
                if self.mcp_host is not None and self.mcp_host.autonomy_cleanup_pending(origin.role_id):
                    continue
                task_id = row["task_id"] or await self._owned(self._find_task, origin)
                if not task_id:
                    if origin.tick_id not in self._handovers:
                        with self._transaction() as db:
                            db.execute("UPDATE autonomy_runs SET status='rejected',launch_pending=0,runtime_acknowledged=1,error='runtime did not accept this original handover' WHERE tick_id=?", (origin.tick_id,))
                            if row["kind"] == "autonomy_ack":
                                from .acknowledgements import settle_attempt
                                settle_attempt(self, db, row)
                            retention.settle(db, origin.tick_id, now=self._now(db))
                        executor = self._executors.pop(origin.tick_id, None)
                        if executor is not None:
                            executor.finished.set()
                    continue
                if row["runtime_result"] is None:
                    snapshot = await self._owned(self.tasks.query, task_id, origin.task_origin())
                    if snapshot.ended_at is None:
                        continue
                    with self._transaction() as db:
                        db.execute("UPDATE autonomy_runs SET runtime_result=?,result_ack_pending=1 WHERE tick_id=?",
                                   (json.dumps({"status": snapshot.status, "output": snapshot.output[-4000:]}), origin.tick_id))
                        retention.retain_runtime_log(db, origin.tick_id, snapshot.log_bytes)
                        db.execute("UPDATE autonomy_runs SET status='aborted',error='runtime interrupted' WHERE tick_id=? AND status IN ('preparing','running')", (origin.tick_id,))
                        if row["kind"] == "autonomy_ack":
                            from .acknowledgements import settle_attempt
                            settle_attempt(self, db, row)
                await self._owned(self.tasks.acknowledge_result, task_id, origin.task_origin())
                with self._transaction() as db:
                    db.execute("UPDATE autonomy_runs SET result_ack_pending=0,runtime_acknowledged=1 WHERE tick_id=?", (origin.tick_id,))
                    if row["kind"] == "autonomy_poll":
                        completed = db.execute("SELECT payload FROM autonomy_runs WHERE tick_id=?", (origin.tick_id,)).fetchone()
                        poll_result = json.loads(completed[0] or "{}") if completed else {}
                        if poll_result.get("pollAttempted"):
                            poll_health = {"generation": origin.generation, "tickId": origin.tick_id,
                                           "sources": poll_result.get("sources", [])}
                            db.execute("UPDATE autonomy_roles SET poll_health=? WHERE role_id=? AND generation=?",
                                       (json.dumps(poll_health), origin.role_id, origin.generation))
                            # A rejected Runtime handover never opens this
                            # gate. Old generations and pre-restart polls
                            # cannot satisfy the new initial-poll identity.
                            db.execute("UPDATE autonomy_roles SET poll_ready_generation=? WHERE role_id=? AND generation=? AND initial_poll_id=?",
                                       (origin.generation, origin.role_id, origin.generation, origin.tick_id))
                    retention.settle(db, origin.tick_id, now=self._now(db))
                self._executors.pop(origin.tick_id, None)
            except (PermissionError, KeyError, ValueError, sqlite3.Error):
                # Disabled/filtered origins and database contention prove
                # nothing about whether an old submit/ACK reached its owner.
                continue
        if not self._closed and self.enabled():
            from .acknowledgements import schedule_acks
            await schedule_acks(self)
        with self._transaction() as db:
            projections = [row[0] for row in db.execute('SELECT DISTINCT role_id FROM autonomy_drift_progress WHERE projection_pending=1')]
            journals = [row[0] for row in db.execute("SELECT DISTINCT role_id FROM autonomy_drift_write_journal WHERE status='pending'")]
            meme_roles = [row[0] for row in db.execute("SELECT DISTINCT role_id FROM autonomy_meme_operations WHERE status NOT IN ('recorded','not_published','reconciled')")]
        for role_id in meme_roles:
            if (self.role_getter(role_id) is None or self.sessions.active_run(role_id) is not None
                    or any(executor.origin.role_id == role_id for executor in self._executors.values())):
                continue
            try:
                await self._owned(self.memes.reconcile, role_id)
            except (ValueError, OSError, sqlite3.Error):
                pass  # A journal is responsibility, never authority to replay a copy.
        for role_id in journals:
            if self.role_getter(role_id) is None or any(executor.origin.role_id == role_id for executor in self._executors.values()):
                continue
            try:
                await self._owned(self.drift_files.reconcile, role_id)
            except (ValueError, OSError):
                pass
        for role_id in projections:
            if self.role_getter(role_id) is None or any(executor.origin.role_id == role_id for executor in self._executors.values()):
                continue
            try:
                from .drift import project_progress
                await self._owned(self.drift_files.reconcile, role_id)
                await self._owned(project_progress, self, role_id)
            except (ValueError, OSError, sqlite3.Error):
                pass  # Keep the exact committed version and its durable obligation.

    async def close(self):
        self._closed = True
        if self._closing is None or self._closing.done() and (self._closing.cancelled() or self._closing.exception() is not None):
            self._closing = asyncio.create_task(self._close())
        await finish_cleanup(self._closing)

    async def _close(self):
        self._closed = True
        await self.source_verifications.close()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        # Retry each durable owner below even if an earlier background join
        # failed. Its completed exception is not proof that cleanup finished.
        for task in tuple(self._cleanup_tasks.values()):
            if task.cancelled():
                continue
            try:
                await finish_cleanup(task)
            except (RuntimeError, OSError, KeyError, PermissionError, sqlite3.Error):
                pass
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM autonomy_runs WHERE runtime_acknowledged=0").fetchall()
        for row in rows:
            origin = AutonomyOrigin.restore(row["origin"])
            task_id = row["task_id"] or await self._owned(self._find_task, origin)
            if task_id:
                await self._owned(self.tasks.stop, task_id, origin.task_origin())
            executor = self._executors.get(origin.tick_id)
            if executor is not None and not executor.finished.is_set():
                executor.cancel()
                if not await self._owned(executor.finished.wait, 10):
                    raise RuntimeError("autonomy cleanup is still pending")
        await self.recover()
        if self.mcp_host is not None:
            with self._transaction() as db:
                roles = [row[0] for row in db.execute("SELECT role_id FROM autonomy_roles")]
            if any(self.mcp_host.autonomy_cleanup_pending(role_id) for role_id in roles):
                raise RuntimeError("autonomous MCP cleanup remains owned and pending")
