"""Opt-in Windows checks exclusively target the process created by this test."""
import asyncio
import importlib.util
import os
from pathlib import Path
from datetime import datetime, timezone
import json
import time
import subprocess
import sys
from backend.app.computer_use.lease import DesktopLease, LeaseError, JOB_NAME
from backend.app.browser_use.process import WindowsJob

import pytest

from backend.app.computer_use.driver import PinnedDriverSession
from backend.app.computer_use.windows import WindowsDesktop
from backend.app.computer_use import ComputerUseHost, load_computer_tools
from backend.app.models import RoleInput, ModelConfigurationInput, AgentRun
from backend.app.role_store import RoleStore
from backend.app.model_config import ModelConfigurationStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry
from backend.app.agent_runtime.tools import ToolContext
from backend.app.agent_runtime.provider import CancellationToken

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures/computer_use/window.py"
spec = importlib.util.spec_from_file_location("computer_fixture", FIXTURE_PATH)
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)
RESOURCES = os.environ.get("MEIDO_COMPUTER_USE_NATIVE_RESOURCES")
pytestmark = [pytest.mark.skipif(not RESOURCES or os.name != "nt", reason="explicit owned-window native fixture only"),
              pytest.mark.usefixtures("local_platform_only")]


def test_pinned_driver_observes_only_its_owned_fixture_with_provenance(tmp_path,record_property):
    window = fixture_module.FixtureWindow(tmp_path / "owned-window")
    try:
        state = window.state()
        pid, hwnd = state["pid"], state["a"]["hwnd"]
        identity=WindowsDesktop().inspect(pid,hwnd)
        assert identity.pid == pid
        record_property("owned_fixture",json.dumps({"pid":pid,"hwnd":hwnd,"dpi":identity.dpi,"bounds":identity.bounds,"title":window.title}))
        async def run():
            client = PinnedDriverSession(Path(RESOURCES), tmp_path / "driver")
            try:
                await client.connect()
                listed = await client.call("list_windows", {"pid": pid})
                assert str(hwnd) in str(listed), str(listed)[:500]
                result = await client.call("get_window_state", {"pid": pid, "window_id": hwnd, "max_depth": 6, "max_elements": 200, "max_dimension": 1600})
                data = result.get("structuredContent", {})
                assert data.get("pid") == pid, {k:v for k,v in data.items() if k != "screenshot"}
                assert data.get("window_id") == hwnd
                assert data["capture"]["provenance"] in {"print_window", "wgc"}
                assert data["capture"]["source_window_id"] == hwnd
                record_property("capture",json.dumps(data["capture"]))
                assert any(x["type"] == "image" for x in result["content"])
                assert any(x.get("label") == "Fixture button" for x in data["elements"])
                window.maximize()
                maximized=WindowsDesktop().inspect(pid,hwnd)
                assert maximized.bounds!=identity.bounds
                large=await client.call("get_window_state",{"pid":pid,"window_id":hwnd,"max_dimension":1600})
                capture=large["structuredContent"]["capture"]
                assert capture["source_window_id"]==hwnd and max(capture["image_dimensions"])<=1600
                assert any(x["type"]=="image" for x in large["content"])
                record_property("maximized_capture",json.dumps(capture))
            finally:
                await client.close()
                assert client.exit_confirmed
        asyncio.run(run())
    finally:
        window.close()


def test_public_eight_tools_act_on_owned_window_and_release(tmp_path):
    window = fixture_module.FixtureWindow(tmp_path / "owned-window")
    host = None
    exercise_started = False
    try:
        initialize_databases(tmp_path / "data")
        roles, models = RoleStore(tmp_path / "roles"), ModelConfigurationStore(tmp_path / "models.json")
        config = models.create(ModelConfigurationInput(providerId="custom", provider="fixture", baseUrl="http://127.0.0.1:9/v1", model="fixture", apiKey="fixture"))
        role = roles.create(RoleInput(name="fixture", profile={"profile": "fixture"}, agentConfig={"computerUse": {"enabled": True}}))
        roles.set_model_configuration(role.id, config.id)
        sessions = SessionStore(tmp_path / "data/sessions.db")
        session = sessions.open_role_session(role.id)
        run = AgentRun(runId="native-run", roleId=role.id, sessionKey=session.sessionKey, status="running", modelConfigurationId=config.id,
                       modelSnapshot={"computerUseSource": "desktop_owner"}, startedAt=datetime.now(timezone.utc))
        sessions.create_run_with_messages(run, "operate only this test window")
        host = ComputerUseHost(role_getter=roles.get, configuration_getter=models.get, sessions=sessions,
            model_registry=ModelAdapterRegistry(), resources=Path(RESOURCES), data_root=tmp_path / "driver")
        tools = {t.definition.name:t for t in load_computer_tools(host, role.id)}
        context = ToolContext(role.id, session.sessionKey, run.runId, CancellationToken(), "native-call")
        state = window.state()
        pid, hwnd = state["pid"], state["a"]["hwnd"]
        def input_receipt(result):
            receipt = result.details["input_receipt"]
            assert receipt["status"] == "available", result.content
            assert json.loads(result.content)["input_receipt"] == receipt
            return receipt
        async def invoke(name, arguments):
            result = await tools["computer_" + name].execute(arguments, context)
            assert not result.is_error, (name, result.content)
            if name in {"click", "type_text", "press_key", "hotkey", "scroll"}:
                input_receipt(result)
            return result
        async def observe():
            # This fixture's native input queue must settle before taking the next
            # observation; a fast local model is not evidence of UI completion.
            await asyncio.sleep(.15)
            return json.loads((await invoke("get_window_state", {"pid":pid,"window_id":hwnd})).content)
        def target(data):
            return {k:data[k] for k in ("pid","window_id","snapshot_id","observation_id")}
        async def perform(name, extra, *, label=None):
            data=await observe()
            arguments=target(data)
            if label:
                element=next(x for x in data["elements"] if x.get("label")==label)
                arguments["element_token"]=element["element_token"]
            arguments.update(extra)
            return await invoke(name,arguments)
        async def exercise():
            nonlocal exercise_started
            exercise_started = True
            try:
                listed=await invoke("list_windows",{"pid":pid})
                assert str(hwnd) in listed.content, listed.content
                b=window.state()["b"]
                b_state=json.loads((await invoke("get_window_state",{"pid":pid,"window_id":b["hwnd"]})).content)
                b_action=target(b_state)
                b_action.update(element_token=next(e["element_token"] for e in b_state["elements"] if e.get("label")=="Fixture button"),delivery_mode="background")
                refused=await tools["computer_click"].execute(b_action,context)
                assert refused.is_error and "background_unavailable" in refused.content
                input_receipt(refused)
                assert window.state()["b"]["clicks"]==0
                reduced = await invoke("get_window_state", {"pid":pid,"window_id":hwnd,"max_dimension":200})
                reduced_data=json.loads(reduced.content)
                capture=reduced_data["capture"]
                actual_button=window.state()["a"]
                assert max(capture["image_dimensions"])<=200
                args=target(reduced_data)
                args.update(x=(actual_button["button_x"]-capture["physical_origin"][0])/capture["resize_ratio"],
                            y=(actual_button["button_y"]-capture["physical_origin"][1])/capture["resize_ratio"],delivery_mode="foreground")
                await invoke("click",args)
                await perform("click",{"delivery_mode":"background"},label="Fixture button")
                await perform("type_text",{"text":"你好 Meido", "delivery_mode":"background"},label="Fixture input")
                await perform("press_key",{"key":"end","delivery_mode":"background"},label="Fixture input")
                await perform("hotkey",{"keys":["ctrl","a"],"delivery_mode":"foreground"},label="Fixture input")
                # Fixed upstream rejects WinForms background wheel before input.
                # Observe again and explicitly choose foreground for its real path.
                data=await observe()
                args=target(data)
                args.update(direction="down",amount=1,delivery_mode="background",x=80,y=160)
                unsupported=await tools["computer_scroll"].execute(args,context)
                assert unsupported.is_error and "background_unavailable" in unsupported.content
                input_receipt(unsupported)
                for direction in ("down","up","right","left"):
                    data=await observe()
                    args=target(data)
                    args.update(direction=direction,amount=1,delivery_mode="foreground",x=80,y=160)
                    await invoke("scroll",args)
                await invoke("release",{})
                assert (await tools["computer_list_windows"].execute({"pid":pid},context)).is_error
            finally:
                try:
                    await host.close()
                finally:
                    assert not WindowsJob.named_job_active(JOB_NAME)
        asyncio.run(exercise())
        actual=window.state()["a"]
        assert actual["clicks"]==2, actual
        assert actual["text"]=="你好 Meido", actual
        assert actual["keys"]>0, actual
        assert "End," in actual["key_events"] and "A," in actual["key_events"],actual
        assert actual["enabled"] is True
        assert all(direction in actual["directions"] for direction in ("up,","down,","left,","right,")), {k:actual[k] for k in ("directions","scroll_x","scroll_y","wheel_v","wheel_h")}
    finally:
        try:
            if host is not None and not exercise_started:
                asyncio.run(host.close())
        finally:
            try:
                assert not WindowsJob.named_job_active(JOB_NAME)
            finally:
                try:
                    window.close()
                finally:
                    assert window.process.poll() is not None



def test_process_death_keeps_single_lease_until_owned_job_is_gone(tmp_path):
    # No UI calls: the child owns only a freshly started idle driver.
    import concurrent.futures

    owner = subprocess.Popen([sys.executable, "-u", "-c", "import runpy,sys; runpy.run_path(sys.argv.pop(1),run_name='__main__')",
        str(FIXTURE_PATH.with_name("lease_owner.py")),
        RESOURCES, str(tmp_path / "driver")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    executor = None
    try:
        executor = concurrent.futures.ThreadPoolExecutor()
        ready = executor.submit(owner.stdout.readline).result(timeout=20)
        assert ready, owner.stderr.read() if owner.poll() is not None else "driver readiness missing"
        assert json.loads(ready)["ready"]
        with pytest.raises(LeaseError,match="busy"):
            DesktopLease()
        assert WindowsJob.named_job_active(JOB_NAME)
        owner.kill()  # Exclusively the test-created owner process.
        owner.wait(10)
        deadline=time.monotonic()+5
        while WindowsJob.named_job_active(JOB_NAME) and time.monotonic()<deadline:
            time.sleep(.02)
        assert not WindowsJob.named_job_active(JOB_NAME)
        next_owner=DesktopLease()
        next_owner.close()
    finally:
        try:
            try:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(10)
            finally:
                try:
                    if executor is not None:
                        executor.shutdown(wait=True)
                finally:
                    try:
                        owner.stdout.close()
                    finally:
                        owner.stderr.close()
        finally:
            deadline = time.monotonic() + 5
            while WindowsJob.named_job_active(JOB_NAME) and time.monotonic() < deadline:
                time.sleep(.02)
            assert not WindowsJob.named_job_active(JOB_NAME)


def test_stop_during_native_invoke_does_not_mutate_fixture_window_flags(tmp_path):
    window=fixture_module.FixtureWindow(tmp_path/"owned-blocking-window")
    host = None
    exercise_started = False
    try:
        initialize_databases(tmp_path/"data")
        roles,models=RoleStore(tmp_path/"roles"),ModelConfigurationStore(tmp_path/"models.json")
        config=models.create(ModelConfigurationInput(providerId="custom",provider="fixture",baseUrl="http://127.0.0.1:9/v1",model="fixture",apiKey="fixture"))
        role=roles.create(RoleInput(name="fixture",profile={"profile":"fixture"},agentConfig={"computerUse":{"enabled":True}}))
        roles.set_model_configuration(role.id,config.id)
        sessions=SessionStore(tmp_path/"data/sessions.db")
        session=sessions.open_role_session(role.id)
        run=AgentRun(runId="blocked-native",roleId=role.id,sessionKey=session.sessionKey,status="running",modelConfigurationId=config.id,
            modelSnapshot={"computerUseSource":"desktop_owner"},startedAt=datetime.now(timezone.utc))
        sessions.create_run_with_messages(run,"operate only own blocked fixture")
        host=ComputerUseHost(role_getter=roles.get,configuration_getter=models.get,sessions=sessions,
            model_registry=ModelAdapterRegistry(),resources=Path(RESOURCES),data_root=tmp_path/"driver")
        tools={t.definition.name:t for t in load_computer_tools(host,role.id)}
        context=ToolContext(role.id,session.sessionKey,run.runId,CancellationToken(),"blocked-click")
        pid,hwnd=window.state()["pid"],window.state()["a"]["hwnd"]
        baseline=window.native_flags()
        async def run_blocked():
            nonlocal exercise_started
            exercise_started = True
            try:
                listed=await tools["computer_list_windows"].execute({"pid":pid},context)
                assert not listed.is_error,listed.content
                result=await tools["computer_get_window_state"].execute({"pid":pid,"window_id":hwnd},context)
                assert not result.is_error,result.content
                data=json.loads(result.content)
                action={k:data[k] for k in ("pid","window_id","snapshot_id","observation_id")}
                action.update(element_token=next(e["element_token"] for e in data["elements"] if e.get("label")=="Fixture button"),delivery_mode="background")
                (window.directory/"block-click").touch()
                task=asyncio.create_task(tools["computer_click"].execute(action,context))
                deadline=time.monotonic()+5
                while window.state()["a"]["clicks"]<1 and time.monotonic()<deadline:
                    await asyncio.sleep(.02)
                assert window.state()["a"]["clicks"]==1
                assert window.native_flags()==baseline
                context.signal.cancel()
                try:
                    await asyncio.wait_for(task,5)
                except asyncio.CancelledError:
                    pass
                await host.close()
                assert not WindowsJob.named_job_active(JOB_NAME)
                assert window.native_flags()==baseline
                assert window.process.poll() is None
                assert window.state()["a"]["clicks"]==1
                next_owner=await asyncio.to_thread(DesktopLease)
                await asyncio.to_thread(next_owner.close)
            finally:
                try:
                    (window.directory/"block-click").unlink(missing_ok=True)
                finally:
                    try:
                        await host.close()
                    finally:
                        assert not WindowsJob.named_job_active(JOB_NAME)
        asyncio.run(run_blocked())
    finally:
        try:
            if host is not None and not exercise_started:
                asyncio.run(host.close())
        finally:
            try:
                assert not WindowsJob.named_job_active(JOB_NAME)
            finally:
                try:
                    window.close()
                finally:
                    assert window.process.poll() is not None
