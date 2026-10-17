"""Host clock admits one genuinely owned tick and never catches up downtime."""
import asyncio
import pytest

from test_proactive_configuration import system
from backend.app.proactive_runtime.config import AutonomyConfig

pytestmark = pytest.mark.usefixtures("local_platform_only")


def enable(app):
    return app.configure("role", AutonomyConfig(activeDecisionEnabled=True,
        decisionModelConfigurationId="explicit-model", consentRevision=1), expected_revision=0)


def test_first_tick_waits_and_empty_snapshot_uses_one_bounded_owned_task(system):
    app, _, tasks, now, _ = system
    enable(app)

    async def exercise():
        assert await app.on_clock() == 0
        now[0] += 1800
        assert await app.on_clock() == 1
        assert await app.on_clock() == 0
        for _ in range(100):
            await app.recover()
            runs = app.query("role")["runs"]
            if runs[0]["runtimeAcknowledged"]:
                break
            await asyncio.sleep(.01)
        assert len(runs) == 1
        assert runs[0]["mode"] == "none"
        assert runs[0]["status"] == "no_events"
        assert runs[0]["runtimeAcknowledged"] is True
        assert runs[0]["timeoutSeconds"] == 600
        assert runs[0]["origin"]["allowedModes"] == ["active_decision"]
        assert runs[0]["origin"]["roleId"] == "role"
        assert "owner" not in runs[0]["origin"]
    asyncio.run(exercise())


def test_clock_jump_is_one_new_tick_and_rollback_does_not_reopen_old_budget(system):
    app, _, _, now, _ = system
    enable(app)

    async def exercise():
        now[0] += 7200
        assert await app.on_clock() == 1
        next_at = app.query("role")["nextDecisionAt"]
        now[0] -= 3600
        assert await app.on_clock() == 0
        assert app.query("role")["nextDecisionAt"] == next_at
        assert len(app.query("role")["runs"]) == 1
    asyncio.run(exercise())
