"""Computer Use's public AgentTool boundary; OS/driver are injected ports."""
import asyncio
import base64
import json
import pytest
from dataclasses import replace
from time import monotonic
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.computer_use import ComputerUseHost, load_computer_tools

pytestmark=pytest.mark.usefixtures("local_platform_only")


class Desktop:
    def validate(self):
        return "fixture-desktop"

    def inspect(self, pid, hwnd):
        return SimpleNamespace(pid=pid, window_id=hwnd, created=123, bounds=(10, 10, 310, 210),
                               dpi=96, thread_id=3, class_name="fixture", sid="fixture", session_id=1)

    def activity(self):
        return (81, 9, False)


class Lease:
    def close(self):
        pass


class Driver:
    exit_confirmed = False

    def __init__(self):
        self.calls = []

    async def connect(self):
        pass

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "list_windows":
            return {"structuredContent": {"windows": [{"pid": 42, "window_id": h, "title": "fixture"} for h in (81, 82)]}, "content": []}
        if name == "get_window_state":
            pixels = BytesIO()
            Image.new("RGB", (300, 200), "white").save(pixels, "PNG")
            return {"structuredContent": {"pid": 42, "window_id": arguments["window_id"], "snapshot_id": "s00000001",
                "elements": [{"element_token": "s00000001:2", "index": 2}],
                "capture": {"provenance": "print_window", "source_window_id": arguments["window_id"],
                            "source_dimensions": [300, 200], "image_dimensions": [300, 200],
                            "physical_origin": [10, 10], "resize_ratio": 1.0, "pixel_coordinates_available": True}},
                "content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(pixels.getvalue()).decode()}]}
        return {"structuredContent": {"effect": "unverifiable"}, "content": []}

    async def close(self):
        self.exit_confirmed = True


def fixture_host(tmp_path, *, enabled=True, source="desktop_owner", clock=monotonic):
    role = SimpleNamespace(id="r", agentConfig=SimpleNamespace(computerUse=SimpleNamespace(enabled=enabled)), modelConfigurationId="chat")
    configuration = SimpleNamespace(id="chat", model="fixture", baseUrl="http://127.0.0.1", apiKey="")
    run = SimpleNamespace(roleId="r", sessionKey="role:r", status="running", modelConfigurationId="chat",
                          modelSnapshot={"computerUseSource": source})
    user = SimpleNamespace(runId="run", role="user", status="completed", messageType="text", metadata={})
    sessions = SimpleNamespace(get_run=lambda _: run, list_messages=lambda _: [user])
    driver = Driver()
    host = ComputerUseHost(role_getter=lambda _: role, configuration_getter=lambda _: configuration,
        sessions=sessions, model_registry=SimpleNamespace(supports_images=lambda _: True),
        resources=tmp_path, data_root=tmp_path / "data", desktop=Desktop(),
        driver_factory=lambda _: driver, lease_factory=Lease, clock=clock)
    tools = {tool.definition.name: tool for tool in load_computer_tools(host, role_id="r")}
    context = ToolContext("r", "role:r", "run", CancellationToken(), "call")
    return host, tools, context, driver, role


def test_closed_role_cannot_list_windows_even_with_frozen_tool(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path, enabled=False)
    async def run():
        result = await tools["computer_list_windows"].execute({}, context)
        assert result.is_error and "permission_denied" in result.content
        assert driver.calls == []
        await host.close()
    asyncio.run(run())


def test_new_observation_of_another_window_invalidates_previous_window(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    async def run():
        await tools["computer_list_windows"].execute({}, context)
        a = json.loads((await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 81}, context)).content)
        await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 82}, context)
        stale = {k: a[k] for k in ("pid", "window_id", "snapshot_id", "observation_id")}
        stale.update(x=12, y=12)
        refused = await tools["computer_click"].execute(stale, context)
        assert refused.is_error and "stale_observation" in refused.content
        assert not any(c[0] == "click" for c in driver.calls)
        await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("source",["scheduled","channel","spawn","proactive","",None])
def test_external_source_never_starts_native_driver(tmp_path,source):
    host, tools, context, driver, _ = fixture_host(tmp_path, source=source)
    async def run():
        result = await tools["computer_list_windows"].execute({}, context)
        assert result.is_error and not driver.calls
        await host.close()
    asyncio.run(run())


def test_stop_cancels_an_inflight_native_request_and_joins_driver(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    async def run():
        entered = asyncio.Event()
        async def block(name, arguments):
            entered.set()
            await asyncio.Event().wait()
        driver.call = block
        task = asyncio.create_task(tools["computer_list_windows"].execute({}, context))
        await entered.wait()
        context.signal.cancel()
        try:
            await asyncio.wait_for(task, .5)
        except asyncio.CancelledError:
            pass
        assert driver.exit_confirmed
        await host.close()
    asyncio.run(run())


def test_unmapped_wgc_image_allows_token_but_refuses_pixel_before_input(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    original = driver.call
    async def wgc(name, arguments):
        result = await original(name, arguments)
        if name == "get_window_state":
            result["structuredContent"]["capture"].update(provenance="wgc", physical_origin=None,
                                                        resize_ratio=None, pixel_coordinates_available=False)
        return result
    driver.call = wgc
    async def run():
        await tools["computer_list_windows"].execute({}, context)
        state = await tools["computer_get_window_state"].execute({"pid":42,"window_id":81},context)
        assert state.images
        data = json.loads(state.content)
        action = {k:data[k] for k in ("pid","window_id","snapshot_id","observation_id")}
        pixel = await tools["computer_click"].execute({**action,"x":10,"y":10},context)
        assert pixel.is_error and "pixel_mapping_unavailable" in pixel.content
        token = await tools["computer_click"].execute({**action,"element_token":"s00000001:2"},context)
        assert not token.is_error
        assert len([c for c in driver.calls if c[0]=="click"])==1
        await host.close()
    asyncio.run(run())


def test_a_failed_observation_of_another_window_also_consumes_old_receipt(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    original = driver.call
    async def failing(name, arguments):
        if name=="get_window_state" and arguments["window_id"]==82:
            return {"isError":True,"content":[{"type":"text","text":"fixture failure"}]}
        return await original(name,arguments)
    driver.call = failing
    async def run():
        await tools["computer_list_windows"].execute({},context)
        data=json.loads((await tools["computer_get_window_state"].execute({"pid":42,"window_id":81},context)).content)
        assert (await tools["computer_get_window_state"].execute({"pid":42,"window_id":82},context)).is_error
        action={k:data[k] for k in ("pid","window_id","snapshot_id","observation_id")}
        action["element_token"]="s00000001:2"
        assert (await tools["computer_click"].execute(action,context)).is_error
        await host.close()
    asyncio.run(run())


def test_unlisted_state_attempt_also_revokes_the_old_actionable_observation(tmp_path):
    host,tools,context,driver,_=fixture_host(tmp_path)
    async def run():
        await tools["computer_list_windows"].execute({},context)
        data=json.loads((await tools["computer_get_window_state"].execute({"pid":42,"window_id":81},context)).content)
        refused=await tools["computer_get_window_state"].execute({"pid":42,"window_id":999},context)
        assert refused.is_error
        action={k:data[k] for k in ("pid","window_id","snapshot_id","observation_id")}
        action["element_token"]="s00000001:2"
        assert (await tools["computer_click"].execute(action,context)).is_error
        assert not any(name=="click" for name,_ in driver.calls)
        await host.close()
    asyncio.run(run())


def test_role_observes_clicks_and_must_refresh_then_releases_same_run(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    async def run():
        assert not (await tools["computer_list_windows"].execute({}, context)).is_error
        state = await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 81}, context)
        assert not state.is_error, state.content
        assert len(state.images) == 1
        import json
        observed = json.loads(state.content)
        action = {"pid": 42, "window_id": 81, "snapshot_id": observed["snapshot_id"],
                  "observation_id": observed["observation_id"], "element_token": "s00000001:2"}
        assert not (await tools["computer_click"].execute(action, context)).is_error
        assert (await tools["computer_click"].execute(action, context)).is_error
        assert len([c for c in driver.calls if c[0] == "click"]) == 1
        assert not (await tools["computer_release"].execute({}, context)).is_error
        assert driver.exit_confirmed
        assert (await tools["computer_list_windows"].execute({}, context)).is_error
        await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["origin", "ratio", "dimensions", "provenance"])
def test_invalid_capture_metadata_never_grants_actionable_image(tmp_path, change):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    original = driver.call
    async def invalid(name, arguments):
        result = await original(name, arguments)
        if name == "get_window_state":
            capture = result["structuredContent"]["capture"]
            if change == "origin": capture["physical_origin"] = [float("nan"), 10]
            if change == "ratio": capture["resize_ratio"] = -1
            if change == "dimensions": capture["source_dimensions"] = [1, 1]
            if change == "provenance": capture["provenance"] = "desktop_region"
        return result
    driver.call = invalid
    async def run():
        await tools["computer_list_windows"].execute({}, context)
        result = await tools["computer_get_window_state"].execute({"pid":42,"window_id":81}, context)
        assert result.is_error and not result.images
        assert not any(name == "click" for name, _ in driver.calls)
        await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["ttl", "move", "activity", "snapshot", "token", "pixel", "role"])
def test_changed_observation_context_refuses_input(tmp_path, change):
    now = [100.0]
    host, tools, context, driver, _ = fixture_host(tmp_path,clock=lambda:now[0])
    async def run():
        await tools["computer_list_windows"].execute({},context)
        data=json.loads((await tools["computer_get_window_state"].execute({"pid":42,"window_id":81},context)).content)
        action={k:data[k] for k in ("pid","window_id","snapshot_id","observation_id")}
        action["element_token"]="s00000001:2"
        if change=="ttl": now[0]+=31
        if change=="move":
            original=host.desktop.inspect
            def moved(pid,hwnd):
                result=original(pid,hwnd); result.bounds=(20,20,320,220); return result
            host.desktop.inspect=moved
        if change=="activity": host.desktop.activity=lambda:(82,10,False)
        if change=="snapshot": action["snapshot_id"]="old"
        if change=="token": action["element_token"]="forged"
        if change=="pixel": action.pop("element_token"); action.update(x=300,y=0)
        selected=replace(context,role_id="other") if change=="role" else context
        result=await tools["computer_click"].execute(action,selected)
        assert result.is_error
        assert not any(name=="click" for name,_ in driver.calls)
        await host.close()
    asyncio.run(run())
