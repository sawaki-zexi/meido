"""Accepted owner input seals autonomous publication before asynchronous join."""
import asyncio
import threading

import pytest

from backend.app.ai.model_types import ModelStreamDone, ModelAssistantMessage
from test_proactive_ack import acknowledgements
from test_proactive_mcp import source
from test_proactive_configuration import system
from backend.app.proactive_runtime.identity import AutonomyOrigin


pytestmark = pytest.mark.usefixtures("local_platform_only")


class CleanupHeldModel:
    def __init__(self):
        self.entered, self.cleaning, self.release = (threading.Event() for _ in range(3))

    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        self.entered.set()
        try:
            while True:
                await asyncio.sleep(.01)
        finally:
            self.cleaning.set()
            while not self.release.is_set():
                await asyncio.sleep(.01)
        yield ModelStreamDone(ModelAssistantMessage(content=()))


def test_human_input_seals_old_generation_immediately_while_owned_cleanup_is_held(acknowledgements):
    app, now, _, _, sessions, *_ = acknowledgements
    model = CleanupHeldModel()
    app.adapter_factory = lambda config: model

    async def exercise():
        try:
            now[0] += 1800
            await app.on_clock()
            async with asyncio.timeout(10):
                while not model.entered.is_set():
                    await asyncio.sleep(.01)
            with app._transaction() as db:
                origin = AutonomyOrigin.restore(db.execute("SELECT origin FROM autonomy_runs WHERE kind='autonomy_tick'").fetchone()[0])
            app.accept_human_run("role", "real-human-run")
            assert not app._authorized(origin)
            assert app.query("role")["humanBusy"] is True
            async with asyncio.timeout(2):
                while not model.cleaning.is_set():
                    await asyncio.sleep(.01)
            assert app.query("role")["cleanupPending"] is True
            now[0] += 1800
            assert await app.on_clock() == 0
            assert len(app.query("role")["runs"]) == 1
            assert sessions.list_messages("role:role") == []
            # A second accepted owner run cannot be cleared by finishing the first.
            app.accept_human_run("role", "second-human-run")
            app.finish_human_run("role", "real-human-run")
            assert app.query("role")["humanBusy"] is True
            model.release.set()
            async with asyncio.timeout(10):
                while app.query("role")["cleanupPending"]:
                    await app.recover()
                    await asyncio.sleep(.01)
            app.finish_human_run("role", "second-human-run")
            assert app.query("role")["humanBusy"] is False
            assert not app._authorized(origin)
            assert sessions.list_messages("role:role") == []
        finally:
            model.release.set()
    asyncio.run(exercise())


def test_role_deletion_seals_even_after_host_has_closed_ordinary_role_access(system):
    app, _, _, _, _ = system
    from backend.app.proactive_runtime.config import AutonomyConfig
    app.configure("role", AutonomyConfig(), expected_revision=0)
    app.role_getter = lambda _: None
    asyncio.run(app.begin_role_deletion("role"))
