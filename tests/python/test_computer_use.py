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

    def input_enabled(self, window_id):
        return True

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
        return {"structuredContent": {
            "effect": "unverifiable",
            "input_receipt": {"status": "available", "foreground": [], "business_effect": "unknown",
                              "business": [], "restore": []},
        }, "content": []}

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


def test_failed_foreground_action_preserves_assistance_receipt_and_unknown_effect(tmp_path):
    """A controlled native response proves receipt transport, not real VK delivery."""
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {
        "status": "available",
        "foreground": [{
            "stage": "click",
            "checks": 4,
            "already_foreground_checks": 0,
            "set_foreground_calls": 4,
            "assisted_attempts": 3,
            "vk_noname_down_api_called": True,
            "vk_noname_up_api_called": True,
            "activated": False,
        }],
        "business_effect": "not_started",
        "business": [{"stage": "click", "input_api_called": False}],
        "restore": [],
    }
    original = driver.call

    async def activation_refused(name, arguments):
        if name == "click":
            return {
                "isError": True,
                "structuredContent": {
                    "code": "foreground_unavailable",
                    "input_receipt": receipt,
                },
                "content": [],
            }
        return await original(name, arguments)

    driver.call = activation_refused

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_click"].execute(
                {**action, "x": 12, "y": 12, "delivery_mode": "foreground"}, context
            )
            assert result.is_error
            assert result.details["effect"] == "unknown"
            assert result.details.get("input_receipt") == receipt
            assert json.loads(result.content)["input_receipt"] == receipt
        finally:
            await host.close()

    asyncio.run(run())


def test_malformed_receipt_preserves_original_failure_without_exposing_driver_payload(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    private_payload = "data:image/png;base64,not-a-safe-receipt-field"
    malformed = {
        "status": "available",
        "foreground": [{
            "stage": "click", "checks": 1, "already_foreground_checks": 1,
            "set_foreground_calls": 0, "assisted_attempts": 0,
            "vk_noname_down_api_called": False, "vk_noname_up_api_called": False,
            "activated": True,
        }],
        "business_effect": {"arbitrary_driver_content": private_payload},
        "business": [{"stage": "click", "input_api_called": False}],
        "restore": [],
    }
    original = driver.call

    async def malformed_response(name, arguments):
        if name == "click":
            return {
                "isError": True,
                "structuredContent": {"code": "foreground_unavailable", "input_receipt": malformed},
                "content": [],
            }
        return await original(name, arguments)

    driver.call = malformed_response

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_click"].execute(
                {**action, "x": 12, "y": 12, "delivery_mode": "foreground"}, context
            )
            data = json.loads(result.content)
            assert result.is_error and data["code"] == "foreground_unavailable"
            assert result.details["effect"] == "unknown"
            assert result.details["input_receipt"] == {"status": "unavailable", "reason": "invalid"}
            assert data["input_receipt"] == result.details["input_receipt"]
            assert private_payload not in result.content + json.dumps(result.details)
        finally:
            await host.close()

    asyncio.run(run())


def test_plain_native_failure_marks_receipt_missing_without_inventing_counts(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    original = driver.call

    async def native_failure(name, arguments):
        if name == "click":
            return {"isError": True, "content": [{
                "type": "text", "text": "foreground_unavailable: exact target did not activate"
            }]}
        return await original(name, arguments)

    driver.call = native_failure

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_click"].execute(
                {**action, "x": 12, "y": 12, "delivery_mode": "foreground"}, context
            )
            data = json.loads(result.content)
            assert result.is_error and data.get("code") == "foreground_unavailable"
            assert result.details["effect"] == "unknown"
            missing = {"status": "unavailable", "reason": "missing"}
            assert result.details["input_receipt"] == missing
            assert data["input_receipt"] == missing
            assert "set_foreground_calls" not in result.content
        finally:
            await host.close()

    asyncio.run(run())


def test_failed_keyboard_after_focus_click_preserves_each_stage_and_restore(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {
        "status": "available",
        "foreground": [{
            "stage": "keyboard", "checks": 4, "already_foreground_checks": 0,
            "set_foreground_calls": 4, "assisted_attempts": 3,
            "vk_noname_down_api_called": True, "vk_noname_up_api_called": True,
            "activated": True,
        }, {
            "stage": "focus_click", "checks": 1, "already_foreground_checks": 1,
            "set_foreground_calls": 0, "assisted_attempts": 0,
            "vk_noname_down_api_called": False, "vk_noname_up_api_called": False,
            "activated": True,
        }],
        "business_effect": "unknown",
        "business": [
            {"stage": "keyboard", "input_api_called": False},
            {"stage": "focus_click", "input_api_called": True},
        ],
        "restore": [{
            "stage": "keyboard", "status": "available", "set_foreground_calls": 1,
            "confirmed": True,
        }],
    }
    original = driver.call

    async def keyboard_focus_refused(name, arguments):
        if name == "press_key":
            return {
                "isError": True,
                "structuredContent": {"code": "foreground_unavailable", "input_receipt": receipt},
                "content": [],
            }
        return await original(name, arguments)

    driver.call = keyboard_focus_refused

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_press_key"].execute({
                **action, "key": "End", "element_token": "s00000001:2",
                "delivery_mode": "foreground",
            }, context)
            assert result.is_error and result.details["effect"] == "unknown"
            assert result.details["input_receipt"] == receipt
            assert json.loads(result.content)["input_receipt"] == receipt
        finally:
            await host.close()

    asyncio.run(run())


def test_scroll_receipt_does_not_invent_foreground_activation(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {
        "status": "available", "foreground": [], "business_effect": "unknown",
        "business": [{"stage": "scroll", "input_api_called": True}], "restore": [],
    }
    original = driver.call

    async def scroll_response(name, arguments):
        if name == "scroll":
            return {"structuredContent": {"input_receipt": receipt}, "content": []}
        return await original(name, arguments)

    driver.call = scroll_response

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_scroll"].execute({
                **action, "direction": "down", "amount": 1, "delivery_mode": "foreground",
            }, context)
            assert not result.is_error and result.details["effect"] == "unknown"
            assert result.details["input_receipt"] == receipt
            assert json.loads(result.content)["input_receipt"] == receipt
        finally:
            await host.close()

    asyncio.run(run())


def test_revocation_after_driver_reply_preserves_only_safe_input_facts(tmp_path):
    host, tools, context, driver, role = fixture_host(tmp_path)
    receipt = {
        "status": "available",
        "foreground": [{
            "stage": "click", "checks": 1, "already_foreground_checks": 1,
            "set_foreground_calls": 0, "assisted_attempts": 0,
            "vk_noname_down_api_called": False, "vk_noname_up_api_called": False,
            "activated": True,
        }],
        "business_effect": "unknown",
        "business": [{"stage": "click", "input_api_called": True}],
        "restore": [{
            "stage": "click", "status": "pending", "set_foreground_calls": None,
            "confirmed": None,
        }],
    }
    private_payload = "data:image/png;base64,revoked-pixels-must-not-publish"
    original = driver.call

    async def completed_before_revocation(name, arguments):
        if name == "click":
            role.agentConfig.computerUse.enabled = False
            return {
                "structuredContent": {"input_receipt": receipt, "untrusted": private_payload},
                "content": [{"type": "image", "data": private_payload}],
            }
        return await original(name, arguments)

    driver.call = completed_before_revocation

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_click"].execute(
                {**action, "x": 12, "y": 12, "delivery_mode": "foreground"}, context
            )
            assert result.is_error and result.details["status"] == "permission_denied"
            assert result.details["effect"] == "unknown"
            assert result.details["input_receipt"] == receipt
            assert json.loads(result.content)["input_receipt"] == receipt
            assert result.images == ()
            assert private_payload not in result.content + json.dumps(result.details)
        finally:
            await host.close()

    asyncio.run(run())


def test_lost_driver_response_reports_unavailable_receipt_and_joins_owner(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    original = driver.call

    async def disconnected(name, arguments):
        if name == "click":
            raise ConnectionError("fixture lost the response after dispatch")
        return await original(name, arguments)

    driver.call = disconnected

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_click"].execute(
                {**action, "x": 12, "y": 12, "delivery_mode": "foreground"}, context
            )
            unavailable = {"status": "unavailable", "reason": "response_missing"}
            assert result.is_error and result.details["effect"] == "unknown"
            assert result.details["input_receipt"] == unavailable
            assert json.loads(result.content)["input_receipt"] == unavailable
            assert "set_foreground_calls" not in result.content
            assert driver.exit_confirmed
            assert (await tools["computer_list_windows"].execute({}, context)).is_error
        finally:
            await host.close()

    asyncio.run(run())


def test_native_failure_text_keeps_reason_when_structured_result_contains_receipt(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {
        "status": "available",
        "foreground": [{
            "stage": "click", "checks": 4, "already_foreground_checks": 0,
            "set_foreground_calls": 4, "assisted_attempts": 3,
            "vk_noname_down_api_called": True, "vk_noname_up_api_called": True,
            "activated": False,
        }],
        "business_effect": "not_started",
        "business": [{"stage": "click", "input_api_called": False}], "restore": [],
    }
    original = driver.call

    async def native_error_with_receipt(name, arguments):
        if name == "click":
            return {
                "isError": True, "structuredContent": {"input_receipt": receipt},
                "content": [{"type": "text", "text": "foreground_unavailable: exact target did not activate"}],
            }
        return await original(name, arguments)

    driver.call = native_error_with_receipt

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_click"].execute(
                {**action, "x": 12, "y": 12, "delivery_mode": "foreground"}, context
            )
            assert result.is_error and result.details["effect"] == "unknown"
            assert json.loads(result.content).get("code") == "foreground_unavailable"
            assert result.details["input_receipt"] == receipt
        finally:
            await host.close()

    asyncio.run(run())


def test_unattempted_restore_keeps_confirmation_unknown(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {
        "status": "available",
        "foreground": [{
            "stage": "keyboard", "checks": 1, "already_foreground_checks": 1,
            "set_foreground_calls": 0, "assisted_attempts": 0,
            "vk_noname_down_api_called": False, "vk_noname_up_api_called": False,
            "activated": True,
        }],
        "business_effect": "unknown",
        "business": [{"stage": "keyboard", "input_api_called": True}],
        "restore": [{
            "stage": "keyboard", "status": "available", "set_foreground_calls": 0,
            "confirmed": None,
        }],
    }
    original = driver.call

    async def no_restore_needed(name, arguments):
        if name == "press_key":
            return {"structuredContent": {"input_receipt": receipt}, "content": []}
        return await original(name, arguments)

    driver.call = no_restore_needed

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_press_key"].execute(
                {**action, "key": "End", "delivery_mode": "foreground"}, context
            )
            assert not result.is_error and result.details["effect"] == "unknown"
            assert result.details["input_receipt"] == receipt
            assert json.loads(result.content)["input_receipt"] == receipt
        finally:
            await host.close()

    asyncio.run(run())


def test_generic_mcp_error_keeps_native_code_and_limited_foreground_cause(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {
        "status": "available",
        "foreground": [{
            "stage": "click", "checks": 4, "already_foreground_checks": 0,
            "set_foreground_calls": 4, "assisted_attempts": 3,
            "vk_noname_down_api_called": True, "vk_noname_up_api_called": True,
            "activated": False,
        }],
        "business_effect": "not_started",
        "business": [{"stage": "click", "input_api_called": False}],
        "restore": [{
            "stage": "click", "status": "pending", "set_foreground_calls": None,
            "confirmed": None,
        }],
    }
    original = driver.call

    async def normalized_native_error(name, arguments):
        if name == "click":
            return {
                "isError": True,
                "structuredContent": {"code": "tool_invocation_failed", "input_receipt": receipt},
                "content": [{"type": "text", "text": "foreground_unavailable: exact target did not activate"}],
            }
        return await original(name, arguments)

    driver.call = normalized_native_error

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            observed = json.loads((await tools["computer_get_window_state"].execute(
                {"pid": 42, "window_id": 81}, context
            )).content)
            action = {key: observed[key] for key in (
                "pid", "window_id", "snapshot_id", "observation_id"
            )}
            result = await tools["computer_click"].execute(
                {**action, "x": 12, "y": 12, "delivery_mode": "foreground"}, context
            )
            data = json.loads(result.content)
            assert result.is_error and result.details["effect"] == "unknown"
            assert data["code"] == "tool_invocation_failed"
            assert data.get("cause_code") == "foreground_unavailable"
            assert result.details["input_receipt"] == receipt
        finally:
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


def test_closed_action_result_preserves_unique_native_receipt_text_carrier(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    # Protocol declarations, not an observation of native foreground input.
    receipt = {
        "status": "available",
        "foreground": [{"stage": "click", "checks": 1, "already_foreground_checks": 1,
                        "set_foreground_calls": 0, "assisted_attempts": 0,
                        "vk_noname_down_api_called": False, "vk_noname_up_api_called": False,
                        "activated": True}],
        "business_effect": "unknown", "business": [{"stage": "click", "input_api_called": True}],
        "restore": [{"stage": "click", "status": "pending", "set_foreground_calls": None, "confirmed": None}],
    }
    original = driver.call

    async def call(name, arguments):
        if name != "click":
            return await original(name, arguments)
        driver.calls.append((name, arguments))
        # Canonical CUA publishes only this closed ActionResult, retaining the
        # outer content. The dedicated carrier has no prose or image payload.
        return {"structuredContent": {"effect": "unverifiable", "route": "global_input",
                                      "delivery": {"mode": "unknown"}},
                "content": [{"type": "text", "text": "private-driver-prose-sentinel"},
                            {"type": "text", "text": json.dumps({"meido_input_receipt_v1": receipt})}]}

    driver.call = call

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            data = json.loads((await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 81}, context)).content)
            action = {key: data[key] for key in ("pid", "window_id", "snapshot_id", "observation_id")}
            action.update(element_token="s00000001:2", delivery_mode="foreground")
            result = await tools["computer_click"].execute(action, context)
            assert not result.is_error
            assert result.details["input_receipt"] == receipt
            assert json.loads(result.content)["input_receipt"] == receipt
            assert "meido_input_receipt_v1" not in result.content
            assert "private-driver-prose-sentinel" not in result.content
            assert not result.images
        finally:
            await host.close()

    asyncio.run(run())


def test_duplicate_native_receipt_carriers_preserve_failure_without_selecting_one(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {"status": "available", "foreground": [], "business_effect": "unknown",
               "business": [], "restore": []}
    original = driver.call

    async def call(name, arguments):
        if name != "click":
            return await original(name, arguments)
        driver.calls.append((name, arguments))
        carrier = {"type": "text", "text": json.dumps({"meido_input_receipt_v1": receipt})}
        return {"isError": True, "structuredContent": {"code": "tool_invocation_failed"},
                "content": [{"type": "text", "text": "foreground_unavailable: private-error-sentinel"},
                            carrier, carrier]}

    driver.call = call

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            data = json.loads((await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 81}, context)).content)
            action = {key: data[key] for key in ("pid", "window_id", "snapshot_id", "observation_id")}
            action.update(element_token="s00000001:2", delivery_mode="foreground")
            result = await tools["computer_click"].execute(action, context)
            assert result.is_error and result.details["effect"] == "unknown"
            assert result.details["input_receipt"] == {"status": "unavailable", "reason": "invalid"}
            payload = json.loads(result.content)
            assert payload["code"] == "tool_invocation_failed"
            assert payload["cause_code"] == "foreground_unavailable"
            assert payload["input_receipt"] == result.details["input_receipt"]
            assert "private-error-sentinel" not in result.content and "meido_input_receipt_v1" not in result.content
            assert len([name for name, _ in driver.calls if name == "click"]) == 1
        finally:
            await host.close()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["extra_key", "invalid_json", "duplicate_json_key", "oversize",
                                 "too_many_blocks", "structured_and_carrier", "invalid_structured_with_carrier"])
def test_malformed_or_ambiguous_native_receipt_carrier_cannot_fall_back(tmp_path, case):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {"status": "available", "foreground": [], "business_effect": "unknown",
               "business": [], "restore": []}
    marker = "meido_input_receipt_v1"
    valid = {"type": "text", "text": json.dumps({marker: receipt})}
    structured = {"code": "tool_invocation_failed"}
    if case == "extra_key":
        candidates = [{"type": "text", "text": json.dumps({marker: receipt, "extra": "carrier-private-sentinel"})}, valid]
    elif case == "invalid_json":
        candidates = [{"type": "text", "text": '{"meido_input_receipt_v1":'}, valid]
    elif case == "duplicate_json_key":
        candidates = [{"type": "text", "text": '{"meido_input_receipt_v1":null,"meido_input_receipt_v1":'
                                               + json.dumps(receipt) + '}'}]
    elif case == "oversize":
        candidates = [{"type": "text", "text": '{"meido_input_receipt_v1":' + ' ' * 8193
                                               + json.dumps(receipt) + '}'}, valid]
    elif case == "too_many_blocks":
        candidates = [{"type": "text", "text": "ordinary private prose"}] * 8 + [valid]
    else:
        structured["input_receipt"] = receipt if case == "structured_and_carrier" else {"status": "forged"}
        candidates = [valid]
    original = driver.call

    async def call(name, arguments):
        if name != "click":
            return await original(name, arguments)
        driver.calls.append((name, arguments))
        return {"isError": True, "structuredContent": structured,
                "content": [{"type": "text", "text": "foreground_unavailable: original-private-sentinel"}, *candidates]}

    driver.call = call

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            data = json.loads((await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 81}, context)).content)
            action = {key: data[key] for key in ("pid", "window_id", "snapshot_id", "observation_id")}
            action.update(element_token="s00000001:2", delivery_mode="foreground")
            result = await tools["computer_click"].execute(action, context)
            assert result.is_error and result.details["effect"] == "unknown"
            assert result.details["input_receipt"] == {"status": "unavailable", "reason": "invalid"}
            payload = json.loads(result.content)
            assert payload["code"] == "tool_invocation_failed" and payload["cause_code"] == "foreground_unavailable"
            assert payload["input_receipt"] == result.details["input_receipt"]
            assert "private-sentinel" not in result.content and marker not in result.content
            assert len([name for name, _ in driver.calls if name == "click"]) == 1
        finally:
            await host.close()

    asyncio.run(run())


def test_deep_native_receipt_json_preserves_original_failure_and_does_not_replay(tmp_path):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    text = '{"meido_input_receipt_v1":' + '[' * 4000 + '0' + ']' * 4000 + '}'
    assert len(text) <= 8192
    original = driver.call

    async def call(name, arguments):
        if name != "click":
            return await original(name, arguments)
        driver.calls.append((name, arguments))
        return {"isError": True, "structuredContent": {"code": "tool_invocation_failed"},
                "content": [{"type": "text", "text": "foreground_unavailable: private-deep-sentinel"},
                            {"type": "text", "text": text}]}

    driver.call = call

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            data = json.loads((await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 81}, context)).content)
            action = {key: data[key] for key in ("pid", "window_id", "snapshot_id", "observation_id")}
            action.update(element_token="s00000001:2", delivery_mode="foreground")
            result = await tools["computer_click"].execute(action, context)
            payload = json.loads(result.content)
            assert result.is_error and result.details["effect"] == "unknown"
            assert payload["code"] == "tool_invocation_failed"
            assert payload["cause_code"] == "foreground_unavailable"
            assert result.details["input_receipt"] == {"status": "unavailable", "reason": "invalid"}
            assert "private-deep-sentinel" not in result.content and "meido_input_receipt_v1" not in result.content
            assert len([name for name, _ in driver.calls if name == "click"]) == 1
        finally:
            await host.close()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["valid", "escaped_marker", "malformed", "deep"])
def test_text_only_native_receipt_never_becomes_generic_structured_payload(tmp_path, case):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {"status": "available", "foreground": [], "business_effect": "unknown",
               "business": [], "restore": []}
    value = receipt if case in {"valid", "escaped_marker"} else {"status": "carrier-private-sentinel"}
    carrier = json.dumps({"meido_input_receipt_v1": value})
    if case == "escaped_marker":
        carrier = carrier.replace("meido_input_receipt_v1", r"meido_input_receipt_\u0076\u0031")
    elif case == "deep":
        carrier = '{"meido_input_receipt_v1":' + '[' * 4000 + '0' + ']' * 4000 + '}'
    assert len(carrier) <= 8192
    original = driver.call

    async def call(name, arguments):
        if name != "click":
            return await original(name, arguments)
        driver.calls.append((name, arguments))
        return {"isError": True,
                "content": [{"type": "text", "text": "foreground_unavailable: original-private-sentinel"},
                            {"type": "text", "text": carrier}]}

    driver.call = call

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            data = json.loads((await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 81}, context)).content)
            action = {key: data[key] for key in ("pid", "window_id", "snapshot_id", "observation_id")}
            action.update(element_token="s00000001:2", delivery_mode="foreground")
            result = await tools["computer_click"].execute(action, context)
            payload = json.loads(result.content)
            assert result.is_error and result.details["effect"] == "unknown"
            assert payload["code"] == "foreground_unavailable"
            expected = receipt if case in {"valid", "escaped_marker"} else {"status": "unavailable", "reason": "invalid"}
            assert result.details["input_receipt"] == payload["input_receipt"] == expected
            assert set(payload) == {"code", "input_receipt"}
            assert "private-sentinel" not in result.content and "meido_input_receipt_v1" not in result.content
            assert len([name for name, _ in driver.calls if name == "click"]) == 1
        finally:
            await host.close()

    asyncio.run(run())


@pytest.mark.parametrize("name", ["click", "type_text", "press_key", "hotkey", "scroll"])
def test_disabled_target_remains_observable_but_never_dispatches_any_input(tmp_path, name):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    host.desktop.input_enabled = lambda window_id: False

    async def run():
        try:
            listed = await tools["computer_list_windows"].execute({}, context)
            assert not listed.is_error
            observed = await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 81}, context)
            assert not observed.is_error and observed.images
            data = json.loads(observed.content)
            action = {key: data[key] for key in ("pid", "window_id", "snapshot_id", "observation_id")}
            action["delivery_mode"] = "foreground"
            if name == "scroll":
                action.update(direction="down", amount=1, x=80, y=160)
            else:
                action["element_token"] = "s00000001:2"
                if name == "type_text":
                    action["text"] = "must not be dispatched"
                elif name == "press_key":
                    action["key"] = "end"
                elif name == "hotkey":
                    action["keys"] = ["ctrl", "a"]
            result = await tools["computer_" + name].execute(action, context)
            assert result.is_error
            assert result.details == {"status": "target_disabled", "effect": "not_started"}
            assert json.loads(result.content) == result.details
            assert not result.images and "input_receipt" not in result.content
            assert [called for called, _ in driver.calls] == ["list_windows", "get_window_state"]
        finally:
            await host.close()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["duplicate_key", "malformed_json"])
def test_malformed_escaped_native_receipt_object_cannot_hide_before_valid_carrier(tmp_path, case):
    host, tools, context, driver, _ = fixture_host(tmp_path)
    receipt = {"status": "available", "foreground": [], "business_effect": "unknown",
               "business": [], "restore": []}
    marker = "meido_input_receipt_v1"
    escaped_marker = r"meido_input_receipt_\u0076\u0031"
    if case == "duplicate_key":
        malformed = ('{"' + escaped_marker + '":null,"' + escaped_marker + '":'
                     + json.dumps(receipt) + '}')
    else:
        malformed = '{"' + escaped_marker + '":' + json.dumps(receipt) + ',}'
    assert marker not in malformed
    valid = json.dumps({marker: receipt})
    original = driver.call

    async def call(name, arguments):
        if name != "click":
            return await original(name, arguments)
        driver.calls.append((name, arguments))
        return {"isError": True, "structuredContent": {"code": "tool_invocation_failed"},
                "content": [{"type": "text", "text": "foreground_unavailable: private-error-sentinel"},
                            {"type": "text", "text": malformed},
                            {"type": "text", "text": valid}]}

    driver.call = call

    async def run():
        try:
            await tools["computer_list_windows"].execute({}, context)
            data = json.loads((await tools["computer_get_window_state"].execute({"pid": 42, "window_id": 81}, context)).content)
            action = {key: data[key] for key in ("pid", "window_id", "snapshot_id", "observation_id")}
            action.update(element_token="s00000001:2", delivery_mode="foreground")
            result = await tools["computer_click"].execute(action, context)
            assert result.is_error and result.details["effect"] == "unknown"
            assert result.details["input_receipt"] == {"status": "unavailable", "reason": "invalid"}
            payload = json.loads(result.content)
            assert payload["code"] == "tool_invocation_failed"
            assert payload["cause_code"] == "foreground_unavailable"
            assert payload["input_receipt"] == result.details["input_receipt"]
            assert "private-error-sentinel" not in result.content and marker not in result.content
            assert len([name for name, _ in driver.calls if name == "click"]) == 1
        finally:
            await host.close()

    asyncio.run(run())
