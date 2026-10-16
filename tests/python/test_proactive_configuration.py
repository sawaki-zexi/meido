"""Role autonomy is an explicit, persistent owner configuration."""
import asyncio

import pytest

from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.fixture
def system(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / "sessions.db")
    tasks = TaskRuntime(tmp_path / "tasks")
    now = [1780000000.0]
    calls = []

    def model(identifier):
        calls.append(identifier)
        return {"id": identifier} if identifier == "explicit-model" else None

    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0],
        role_getter=lambda role_id: {"id": role_id} if role_id == "role" else None,
        model_getter=model)
    yield app, sessions, tasks, now, calls
    asyncio.run(app.close())
    tasks.close()


def test_defaults_and_preview_have_no_model_or_source_io(system):
    app, _, _, _, calls = system
    state = app.query("role")
    assert state["config"]["activeDecisionEnabled"] is False
    assert state["config"]["driftEnabled"] is False
    assert state["config"]["sourceGrants"] == []
    assert state["config"]["allowedTargetRefs"] == ["desktop"]
    assert calls == []
    with pytest.raises(KeyError):
        app.query("another-role")


def test_enabling_requires_an_explicit_saved_model_and_consent(system):
    app, _, _, _, _ = system
    with pytest.raises(ValueError, match="model"):
        AutonomyConfig(activeDecisionEnabled=True, consentRevision=1)
    with pytest.raises(ValueError, match="consent"):
        AutonomyConfig(activeDecisionEnabled=True, decisionModelConfigurationId="explicit-model")
    invalid = AutonomyConfig(activeDecisionEnabled=True,
        decisionModelConfigurationId="missing", consentRevision=1)
    with pytest.raises(ValueError, match="model"):
        app.configure("role", invalid, expected_revision=0)
    with pytest.raises(ValueError):
        AutonomyConfig(owner_context=True)


def test_save_reopen_and_revision_cas_do_not_execute_a_tick(system):
    app, sessions, tasks, now, calls = system
    config = AutonomyConfig(activeDecisionEnabled=True,
        decisionModelConfigurationId="explicit-model", consentRevision=1)
    saved = app.configure("role", config, expected_revision=0)
    assert saved["config"]["revision"] == 1
    assert saved["nextDecisionAt"] == now[0] + 1800
    assert saved["runs"] == []
    with pytest.raises(ValueError, match="revision"):
        app.configure("role", config, expected_revision=0)
    asyncio.run(app.close())
    # Reopening retains the grant and advances first decision; no catch-up run.
    now[0] += 7200
    reopened = ProactiveApplication(sessions, tasks, clock=lambda: now[0],
        role_getter=lambda key: {"id": key}, model_getter=lambda key: {"id": key})
    try:
        state = reopened.query("role")
        assert state["config"]["activeDecisionEnabled"] is True
        assert state["config"]["revision"] == 1
        assert state["nextDecisionAt"] == now[0] + 1800
        assert state["runs"] == []
    finally:
        asyncio.run(reopened.close())


def test_config_source_replacement_respects_storage_quota_and_rolls_back_revision(system, monkeypatch):
    import sqlite3
    from backend.app.proactive_runtime import retention
    from backend.app.proactive_runtime.config import SourceGrant

    app, sessions, *_ = system
    monkeypatch.setattr(retention, "DEFAULT_LIMITS", retention.Limits(role_records=2))
    first = SourceGrant(sourceId="first", channel="content", serverId="fixture",
        configRevision="saved", grantRevision="trusted", getTool="get",
        getDefinitionHash="a" * 64, eventDomain="fixture")
    saved = app.configure("role", AutonomyConfig(sourceGrants=[first]), expected_revision=0)
    second = first.model_copy(update={"sourceId": "second"})
    with pytest.raises(retention.StorageLimitError):
        app.configure("role", AutonomyConfig(sourceGrants=[second]), expected_revision=1)
    assert app.query("role")["config"] == saved["config"]
    with sqlite3.connect(sessions.database_path) as db:
        assert db.execute("SELECT source_id,active,revision FROM autonomy_sources").fetchall() == [("first", 1, 1)]
