"""HTTP receipt transport via injected ports and a real localhost provider.

Receipt counters below are declared protocol-fixture values, not native evidence.
The existing public session API omits tool rows; SQLite tool history is inspected
through the approved SessionStore fixture seam. Audit stores terminal categories,
not result receipts.
"""
import asyncio
import copy
import json
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime import RuntimeManager
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.computer_use import ComputerUseHost
from backend.app.memory_config import MemorySettingsStore
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import ModelConfigurationInput, RoleInput
from backend.app.role_store import RoleStore
from backend.app.session_manager import SessionManager
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from test_computer_use import Desktop, Driver, Lease

pytestmark = pytest.mark.usefixtures("local_platform_only")


def _protocol_receipt(name):
    """Complete bounded schema; values deliberately vary by fixture action."""
    if name == "scroll":
        return {
            "status": "available", "foreground": [], "business_effect": "unknown",
            "business": [{"stage": "scroll", "input_api_called": True}], "restore": [],
        }
    stage = "click" if name == "click" else "keyboard"
    retries = {"click": 0, "type_text": 0, "press_key": 1, "hotkey": 2}[name]
    already = int(name == "click")
    return {
        "status": "available",
        "foreground": [{
            "stage": stage, "checks": 1 + retries,
            "already_foreground_checks": already,
            "set_foreground_calls": 1 + retries - already,
            "assisted_attempts": retries,
            "vk_noname_down_api_called": bool(retries),
            "vk_noname_up_api_called": bool(retries), "activated": True,
        }],
        "business_effect": "unknown",
        "business": [{"stage": stage, "input_api_called": True}],
        "restore": [{
            "stage": stage, "status": "pending" if name == "click" else "available",
            "set_foreground_calls": None if name == "click" else 1,
            "confirmed": None if name == "click" else name != "hotkey",
        }],
    }


def _text(content):
    return content if isinstance(content, str) else "\n".join(
        block.get("text", "") for block in content or [] if block.get("type") == "text"
    )


def _sse(text):
    for frame in text.split("\n\n"):
        lines = frame.splitlines()
        kind = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if kind is not None and data is not None:
            yield kind, json.loads(data)


def test_http_receipt_reaches_next_provider_sse_and_persisted_tool_history(tmp_path, monkeypatch, record_property):
    action_phases = {3: "click", 5: "type_text", 7: "press_key", 9: "hotkey",
                     11: "scroll", 13: "scroll", 15: "scroll", 17: "scroll"}
    expected = {f"call-{phase}-0": _protocol_receipt(name) for phase, name in action_phases.items()}
    requests, consumed, provider_errors = [], {}, []
    facts = {"scope": "injected driver protocol, real localhost HTTP provider, no native input"}

    class ReceiptDriver(Driver):
        def __init__(self):
            super().__init__()
            self.png = None

        async def call(self, name, arguments):
            if name in action_phases.values():
                self.calls.append((name, arguments))
                return {"structuredContent": {
                    "effect": "unverifiable", "input_receipt": copy.deepcopy(_protocol_receipt(name)),
                }, "content": []}
            result = await super().call(name, arguments)
            if name == "get_window_state":
                self.png = result["content"][0]["data"]
            return result

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                self.respond()
            except Exception as error:
                provider_errors.append(type(error).__name__)
                self.send_error(500, "fixture provider failed")

        def respond(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            phase = len(requests)
            previous = f"call-{phase - 1}-0"
            if previous in expected:
                message = next(item for item in reversed(payload["messages"])
                               if item.get("role") == "tool" and item.get("tool_call_id") == previous)
                receipt = json.loads(_text(message["content"]))["input_receipt"]
                assert receipt == expected[previous]
                consumed[previous] = receipt
            calls = []
            if phase == 1:
                calls = [("computer_list_windows", {"pid": 42})]
            elif phase in range(2, 19, 2):
                calls = [("computer_get_window_state", {"pid": 42, "window_id": 81})]
            elif phase in action_phases:
                observed = next(json.loads(_text(message.get("content")))
                                for message in reversed(payload["messages"])
                                if message.get("role") == "tool" and '"observation_id"' in _text(message.get("content")))
                action = {key: observed[key] for key in ("pid", "window_id", "snapshot_id", "observation_id")}
                action["delivery_mode"] = "foreground"
                name = action_phases[phase]
                if name != "scroll":
                    action["element_token"] = observed["elements"][0]["element_token"]
                if name == "type_text":
                    action["text"] = "你好 HTTP"
                elif name == "press_key":
                    action["key"] = "end"
                elif name == "hotkey":
                    action["keys"] = ["ctrl", "a"]
                elif name == "scroll":
                    action.update(direction={11: "down", 13: "up", 15: "right", 17: "left"}[phase],
                                  amount=1, x=80, y=160)
                calls = [("computer_" + name, action)]
            elif phase == 19:
                calls = [("computer_release", {})]
            else:
                assert phase == 20
            delta = {"tool_calls": [{"index": index, "id": f"call-{phase}-{index}", "type": "function",
                                     "function": {"name": name, "arguments": json.dumps(arguments)}}
                                    for index, (name, arguments) in enumerate(calls)]} if calls else {
                "content": "Fixture receipt checked and released"}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"choices": [{"delta": delta, "finish_reason": None}]},
                      {"choices": [{"delta": {}, "finish_reason": "tool_calls" if calls else "stop"}]}]
            self.wfile.write(("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                              + "data: [DONE]\n\n").encode())

    server = thread = host = client = None
    request_started = finish_run_returned = False
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server.daemon_threads = False  # server_close joins its owned request handlers.
        thread = threading.Thread(target=server.serve_forever, name="computer-receipt-provider")
        thread.start()
        initialize_databases(tmp_path / "data")
        roles = RoleStore(tmp_path / "roles")
        models = ModelConfigurationStore(tmp_path / "models.json")
        config = models.create(ModelConfigurationInput(
            providerId="custom", provider="fixture", baseUrl=f"http://127.0.0.1:{server.server_port}/v1",
            model="fixture-chat", apiKey="fixture"))
        role = roles.create(RoleInput(name="receipt fixture", profile={"profile": "fixture"},
                                     agentConfig={"computerUse": {"enabled": True}}))
        roles.set_model_configuration(role.id, config.id)
        sessions = SessionStore(tmp_path / "data/sessions.db")
        driver = ReceiptDriver()
        registry = ModelAdapterRegistry(timeout=10)
        host = ComputerUseHost(role_getter=roles.get, configuration_getter=models.get, sessions=sessions,
                               model_registry=registry, resources=tmp_path, data_root=tmp_path / "driver",
                               desktop=Desktop(), driver_factory=lambda _: driver, lease_factory=Lease)
        original_finish_run = host.finish_run

        async def record_finish_run(role_id, run_id):
            nonlocal finish_run_returned
            await original_finish_run(role_id, run_id)
            finish_run_returned = True

        monkeypatch.setattr(host, "finish_run", record_finish_run)
        memory = MemorySettingsStore(tmp_path / "memory.json")
        memory.update(enabled=False, embedding_enabled=False)
        for key, value in {
            "store": roles, "model_configuration_store": models, "session_store": sessions,
            "session_manager": SessionManager(roles, sessions), "runtime_manager": RuntimeManager(sessions),
            "role_locks": {}, "model_adapter": RoutedModelAdapter(registry), "model_adapter_registry": registry,
            "computer_use": host, "memory_settings_store": memory,
        }.items():
            monkeypatch.setattr(main, key, value)
        # As in the existing HTTP fixture, avoid a lifespan context that would
        # start unrelated application workers. Stream finally joins this run.
        client = TestClient(main.app)
        request_started = True
        response = client.post(f"/api/roles/{role.id}/messages", json={"content": "Use only the receipt fixture then release"})
        assert response.status_code == 200
        assert not provider_errors, provider_errors
        assert "assistant_completed" in response.text and "Fixture receipt checked and released" in response.text
        assert len(requests) == 20 and consumed == expected
        for phase, payload in enumerate(requests, 1):
            image_count = json.dumps(payload).count("data:image/png;base64,")
            assert image_count == int(phase in range(3, 20, 2)), phase
        ended = {payload["toolCallId"]: payload for kind, payload in _sse(response.text)
                 if kind == "tool_execution_end"}
        for call_id, receipt in expected.items():
            assert not ended[call_id]["isError"]
            assert json.loads(ended[call_id]["result"])["input_receipt"] == receipt
        public_history = client.get(f"/api/roles/{role.id}/session").json()
        stored = sessions.list_messages(f"role:{role.id}")
        tools = {message.toolCallId: message for message in stored if message.messageType == "tool_result"}
        for call_id, receipt in expected.items():
            assert tools[call_id].toolResult["input_receipt"] == receipt
            assert json.loads(tools[call_id].content)["input_receipt"] == receipt
        user = next(message for message in stored if message.role == "user")
        run = sessions.get_run(user.runId)
        assert run.modelSnapshot["computerUseSource"] == run.modelSnapshot["humanInputSource"] == "desktop_owner"
        audits = {item["callId"]: item for item in sessions.list_tool_audits(run.runId)}
        for phase, name in action_phases.items():
            audit = audits[f"call-{phase}-0"]
            assert audit["toolName"] == "computer_" + name
            assert audit["resultCategory"] == "succeeded" and audit["endedAt"] is not None
            assert audit["toolSource"] == "plugin:computer_use" and audit["policyDecision"] == "allowed"
        stored_json = json.dumps([message.model_dump(mode="json") for message in stored])
        persisted = json.dumps(public_history) + stored_json + json.dumps(audits) + response.text
        assert driver.png is not None
        assert driver.png not in persisted and "data:image" not in persisted and "iVBOR" not in persisted
        assert driver.png.encode() not in Path(sessions.database_path).read_bytes()
        assert driver.exit_confirmed
        facts.update(providerRequests=len(requests), receiptsConsumed=len(consumed), inputAudits=len(expected),
                     pngExpiredAfterNextRequest=True, persistedToolReceipts=len(expected))
    finally:
        try:
            if host is not None:
                # The original public finish_run must have actually returned;
                # a stream response alone can hide cleanup errors.
                assert not request_started or finish_run_returned, "fixture request cleanup unconfirmed"
                asyncio.run(host.close())
                facts["finishRunReturned"] = finish_run_returned
                facts["hostClosed"] = True
        finally:
            try:
                if client is not None:
                    client.close()
                    facts["clientClosed"] = True
            finally:
                try:
                    if thread is not None and thread.is_alive():
                        server.shutdown()
                finally:
                    try:
                        if server is not None:
                            server.server_close()
                    finally:
                        if thread is not None and thread.ident is not None:
                            thread.join()
                        facts["providerThreadJoined"] = thread is None or not thread.is_alive()
                        record_property("receipt_http_fixture_facts", json.dumps(facts))
                        assert facts["providerThreadJoined"]
