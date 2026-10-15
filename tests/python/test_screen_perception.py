import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import sys
import threading

from PIL import Image
import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import AgentRun, ModelConfigurationInput, RoleInput
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.screen_perception import ScreenPerception, ObserveScreenTool


@contextmanager
def vision_server(reply='{"interface_summary":"fixture editor","activity_key":"writing"}', *, api="openai-completions", chat=False, tool_call=False, status=200, hang=False, thinking=""):
    connected = threading.Event()
    disconnected = threading.Event()
    class Requests(list):
        pass
    requests = Requests()
    requests.connected = connected
    requests.disconnected = disconnected

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            connected.set()
            if hang:
                while not disconnected.is_set():
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        disconnected.wait(0.025)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        disconnected.set()
                        return
            if status != 200:
                # Real remote failure echoes the submitted image, key and text.
                self.wfile.write(json.dumps({"error": {"message": "fixture-secret " + json.dumps(requests[-1])}}).encode())
                return
            answer = reply() if callable(reply) else reply
            if api == "anthropic-messages":
                events = [
                    ("message_start", {"type": "message_start", "message": {"id": "vision", "role": "assistant", "content": [], "model": "vision-only"}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": answer}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                body = "".join(f"event: {kind}\ndata: {json.dumps(payload)}\n\n" for kind, payload in events)
            else:
                calls = tool_call or (chat and len(requests) == 1)
                delta = {"tool_calls": [{"index": 0, "id": "screen-call", "type": "function", "function": {"name": "observe_screen", "arguments": "{}"}}]} if calls else {"content": "I saw the fixture editor" if chat else answer, "reasoning_content": thinking}
                body = "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": None}]}) + "\n\ndata: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls" if calls else "stop"}]}) + "\n\ndata: [DONE]\n\n"
            self.wfile.write(body.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        disconnected.set()
        server.shutdown()
        server.server_close()
        thread.join()


def observation_host(tmp_path, url, *, api="openai-completions", code=None):
    initialize_databases(tmp_path / "data")
    roles = RoleStore(tmp_path / "roles")
    models = ModelConfigurationStore(tmp_path / "models.json")
    config = models.create(ModelConfigurationInput(providerId="custom", provider="fixture", api=api, baseUrl=url, model="vision-only", apiKey="fixture-secret"))
    role = roles.create(RoleInput(name="Private role marker", profile={"profile": "PRIVATE ROLE HISTORY MUST NOT ENTER VISION"}, agentConfig={"screenPerception": {"enabled": True, "visionModelConfigurationId": config.id}}))
    sessions = SessionStore(tmp_path / "data/sessions.db")
    session = sessions.open_role_session(role.id)
    run = AgentRun(runId="screen-run", roleId=role.id, sessionKey=session.sessionKey, status="running", modelSnapshot={"screenPerceptionSource": "desktop_owner"}, startedAt=datetime.now(timezone.utc))
    sessions.create_run_with_messages(run, "PRIVATE USER HISTORY")
    context = ToolContext(role.id, session.sessionKey, run.runId, CancellationToken())
    processes = []
    source = code or '''import io,json,struct,sys
from PIL import Image
image=Image.new("RGB",(32,16),(231,17,42))
buffer=io.BytesIO();image.save(buffer,format="PNG");data=buffer.getvalue()
metadata=json.dumps({"width":32,"height":16,"bytes":len(data)}).encode()
sys.stdout.buffer.write(struct.pack(">I",len(metadata))+metadata+data)
'''

    async def launch():
        process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", source, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        processes.append(process)
        return process

    service = ScreenPerception(role_getter=roles.get, configuration_getter=models.get, sessions=sessions, adapter=RoutedModelAdapter(ModelAdapterRegistry(timeout=12)), launch_capture=launch)
    return service, ObserveScreenTool(service, config.id, role_id=role.id), context, roles, models, sessions, processes


@pytest.mark.parametrize("api", ["openai-completions", "anthropic-messages"])
def test_observe_screen_uses_selected_rich_protocol_and_returns_only_summary(tmp_path, api):
    with vision_server(api=api) as (url, requests):
        service, tool, context, _, _, _, processes = observation_host(tmp_path, url, api=api)
        result = asyncio.run(tool.execute({}, context))
    assert result.is_error is False, result.content
    assert json.loads(result.content) == {"available": True, "interface_summary": "fixture editor", "activity_key": "writing"}
    assert result.images == ()
    assert processes[0].returncode == 0
    assert len(requests) == 1
    path, payload = requests[0]
    assert payload["model"] == "vision-only"
    assert payload.get("tools", []) == []
    assert payload["max_tokens"] == 1200
    serialized = json.dumps(payload)
    assert "PRIVATE" not in serialized and "Private role" not in serialized
    blocks = payload["messages"][-1]["content"]
    image = next(item for item in blocks if item["type"] in {"image_url", "image"})
    encoded = image["image_url"]["url"].split(",", 1)[1] if api == "openai-completions" else image["source"]["data"]
    with Image.open(BytesIO(base64.b64decode(encoded))) as frame:
        assert frame.size == (32, 16)
        assert frame.getpixel((0, 0)) == (231, 17, 42)
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.parametrize("arguments", [{"role": "other"}, {"model": "other"}, {"bbox": [0, 0, 1, 1]}, {"source": "owner"}])
def test_model_cannot_change_capture_source_or_binding(tmp_path, arguments):
    _, tool, context, _, _, _, processes = observation_host(tmp_path, "http://127.0.0.1:9/v1")
    result = asyncio.run(tool.execute(arguments, context))
    assert result.is_error and json.loads(result.content)["error"] == "permission_denied"
    assert processes == []


@pytest.mark.parametrize("denial", ["role", "channel", "run", "stopped", "internal", "scheduled", "paired", "disabled", "binding", "missing", "unsupported"])
def test_only_host_proven_owner_desktop_run_can_capture(tmp_path, denial):
    service, tool, context, roles, models, sessions, processes = observation_host(tmp_path, "http://127.0.0.1:9/v1")
    if denial == "role":
        context = replace(context, role_id="other")
    elif denial == "channel":
        context = replace(context, session_key="thread:paired-owner")
    elif denial == "run":
        context = replace(context, run_id="not-a-run")
    elif denial == "stopped":
        sessions.update_run(context.run_id, status="cancelled")
    elif denial == "internal":
        sessions.update_run(context.run_id, status="running", model_snapshot={"source": "task_completion"})
    elif denial in {"scheduled", "paired"}:
        message = next(item for item in sessions.list_messages(context.session_key) if item.role == "user")
        sessions.update_message(message.id, message.content, message.status, metadata={"scheduled": True} if denial == "scheduled" else {"channel": "qq", "senderIsOwner": True})
    else:
        role = roles.get(context.role_id)
        setting = role.agentConfig.screenPerception
        if denial == "disabled":
            setting.enabled = False
        elif denial == "binding":
            setting.visionModelConfigurationId = None
        elif denial == "missing":
            models.delete(setting.visionModelConfigurationId)
        elif denial == "unsupported":
            configuration = models.get(setting.visionModelConfigurationId)
            models.update(configuration.id, ModelConfigurationInput(**{**configuration.model_dump(exclude={"id"}), "api": "mistral-conversations", "model": "mistral-large"}))
        if denial in {"disabled", "binding"}:
            from backend.app.models import RoleUpdateInput
            roles.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile, agentConfig=role.agentConfig))
    result = asyncio.run(tool.execute({}, context))
    assert result.is_error, result.content
    assert processes == []


@pytest.mark.parametrize("reply", [
    '```json\n{"interface_summary":"a","activity_key":"b"}\n```',
    '{"interface_summary":"a","interface_summary":"b","activity_key":"b"}',
    '{"interface_summary":"a","activity_key":"b","actions":[]}',
    '{"interface_summary":"a","activity_key":NaN}',
    '{"interface_summary":"a","activity_key":Infinity}',
    '{"interface_summary":"a","activity_key":"b"} trailing',
    '{"interface_summary":3,"activity_key":"b"}',
    '{"interface_summary":" ","activity_key":"b"}',
    '["a","b"]',
    json.dumps({"interface_summary": "x" * 2001, "activity_key": "b"}),
    "x" * 17000,
])
def test_invalid_vision_response_never_becomes_an_observation(tmp_path, reply):
    with vision_server(reply) as (url, _):
        _, tool, context, _, _, _, processes = observation_host(tmp_path, url)
        result = asyncio.run(tool.execute({}, context))
    assert result.is_error
    assert json.loads(result.content)["error"] == "invalid_observation"
    assert len(result.content.encode()) <= 4096 and result.images == ()
    assert reply not in result.content
    assert processes[0].returncode == 0


def test_valid_summary_is_whitespace_normalized_and_bounded(tmp_path):
    reply = json.dumps({"interface_summary": "  " + "a\n" * 500, "activity_key": "  " + "b " * 100})
    with vision_server(reply) as (url, _):
        _, tool, context, *_ = observation_host(tmp_path, url)
        result = asyncio.run(tool.execute({}, context))
    observation = json.loads(result.content)
    assert observation["interface_summary"] == ("a " * 200)
    assert observation["activity_key"] == ("b " * 40)


@pytest.mark.parametrize("source,expected", [
    ("import struct,sys;sys.stdout.buffer.write(struct.pack('>I',1025))", "capture_unavailable"),
    ("import struct,sys;sys.stdout.buffer.write(struct.pack('>I',1)+b'{')", "capture_unavailable"),
    ("import struct,sys;data=b'{\"width\":32,\"height\":16,\"bytes\":4194305}';sys.stdout.buffer.write(struct.pack('>I',len(data))+data)", "frame_too_large"),
    ("import struct,sys;data=b'{\"width\":32,\"height\":16,\"bytes\":3}';sys.stdout.buffer.write(struct.pack('>I',len(data))+data+b'bad')", "capture_unavailable"),
    ("import sys;sys.stderr.buffer.write(b'capture_unavailable');sys.exit(1)", "capture_unavailable"),
    ("import sys;sys.stderr.buffer.write(b'frame_too_large');sys.exit(1)", "frame_too_large"),
    ("import sys;sys.stderr.buffer.write(b'x'*2048);sys.exit(1)", "capture_unavailable"),
])
def test_untrusted_capture_pipe_is_bounded_and_child_is_reaped(tmp_path, source, expected):
    _, tool, context, _, _, _, processes = observation_host(tmp_path, "http://127.0.0.1:9/v1", code=source)
    result = asyncio.run(tool.execute({}, context))
    assert result.is_error and json.loads(result.content)["error"] == expected
    assert processes[0].returncode is not None


def test_capture_timeout_keeps_event_loop_responsive_and_reaps_real_process(tmp_path):
    async def run():
        _, tool, context, _, _, _, processes = observation_host(tmp_path, "http://127.0.0.1:9/v1", code="import time;time.sleep(30)")
        ticks = 0
        task = asyncio.create_task(tool.execute({}, context))
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.05)
        result = await task
        assert json.loads(result.content)["error"] == "timeout"
        assert ticks > 50
        assert processes[0].returncode is not None
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["tools", "remote_error"])
def test_provider_cannot_return_tools_or_leak_echoed_image_errors(tmp_path, mode):
    with vision_server(tool_call=mode == "tools", status=401 if mode == "remote_error" else 200) as (url, _):
        _, tool, context, *_ = observation_host(tmp_path, url)
        result = asyncio.run(tool.execute({}, context))
    assert result.is_error
    assert json.loads(result.content)["error"] == ("invalid_observation" if mode == "tools" else "vision_failed")
    assert "fixture-secret" not in result.content
    assert "data:image" not in result.content


def test_vision_cancellation_closes_real_http_stream(tmp_path):
    with vision_server(hang=True) as (url, requests):
        async def run():
            service, tool, context, _, _, _, processes = observation_host(tmp_path, url)
            task = asyncio.create_task(tool.execute({}, context))
            while not requests:
                await asyncio.sleep(0.01)
            await service.invalidate(role_id=context.role_id)
            with pytest.raises(asyncio.CancelledError):
                await task
            assert processes[0].returncode == 0
        asyncio.run(run())
        assert requests.disconnected.wait(2), "vision server did not observe client disconnect"


def test_discarded_thinking_counts_toward_vision_text_budget(tmp_path):
    with vision_server(thinking="private reasoning " * 1100) as (url, _):
        _, tool, context, *_ = observation_host(tmp_path, url)
        result = asyncio.run(tool.execute({}, context))
    assert result.is_error and json.loads(result.content)["error"] == "invalid_observation"
    assert "private reasoning" not in result.content


def test_configuration_content_change_discards_otherwise_valid_late_vision_result(tmp_path):
    def answer():
        configuration = models.get(roles.get(context.role_id).agentConfig.screenPerception.visionModelConfigurationId)
        models.update(configuration.id, ModelConfigurationInput(**{**configuration.model_dump(exclude={"id"}), "apiKey": "changed-during-request"}))
        return '{"interface_summary":"late response","activity_key":"writing"}'
    with vision_server(answer) as (url, _):
        _, tool, context, roles, models, *_ = observation_host(tmp_path, url)
        result = asyncio.run(tool.execute({}, context))
    assert result.is_error and json.loads(result.content)["error"] == "configuration_changed"
    assert "late response" not in result.content and "changed-during-request" not in result.content


@pytest.mark.parametrize("cancel", ["task", "signal", "role", "configuration", "shutdown"])
def test_cancel_and_revoke_wait_for_real_child_before_reusing_slot(tmp_path, cancel):
    async def run():
        service, tool, context, roles, _, _, processes = observation_host(tmp_path, "http://127.0.0.1:9/v1", code="import time;time.sleep(30)")
        task = asyncio.create_task(tool.execute({}, context))
        while not processes:
            await asyncio.sleep(0.01)
        assert json.loads((await tool.execute({}, context)).content)["error"] == "busy"
        if cancel == "task":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        elif cancel == "signal":
            context.signal.cancel()
        elif cancel == "role":
            await service.invalidate(role_id=context.role_id)
        elif cancel == "configuration":
            await service.invalidate(configuration_id=roles.get(context.role_id).agentConfig.screenPerception.visionModelConfigurationId)
        else:
            await service.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        # A frozen candidate cannot revive after revoke, even if config is unchanged.
        if cancel in {"role", "configuration"}:
            denied = await tool.execute({}, context)
            assert json.loads(denied.content)["error"] == "configuration_changed"
        assert len(processes) == 1
    asyncio.run(run())
