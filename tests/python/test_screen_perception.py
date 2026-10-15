import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import struct
import sys
import threading
import zlib

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
def vision_server(reply='{"interface_summary":"fixture editor","activity_key":"writing"}', *, api="openai-completions", chat=False, tool_call=False, status=200, hang=False, thinking="", keep_open=False):
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
            self.send_header("Content-Type", "application/vnd.amazon.eventstream" if api == "bedrock-converse" else "text/event-stream")
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
                if tool_call:
                    events[1] = ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "forbidden", "name": "forbidden", "input": {}}})
                    events[2] = ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}})
                if keep_open and not disconnected.is_set():
                    events = events[:3]
                body = "".join(f"event: {kind}\ndata: {json.dumps(payload)}\n\n" for kind, payload in events)
            elif api in {"openai-responses", "azure-openai-responses", "openai-codex-responses"}:
                payload = {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "forbidden", "arguments": "{}"}} if tool_call else {"type": "response.output_text.delta", "delta": answer}
                body = f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"
            elif api in {"google-generative", "google-vertex"}:
                part = {"functionCall": {"name": "forbidden", "args": {}}} if tool_call else {"text": answer}
                body = "data: " + json.dumps({"candidates": [{"content": {"parts": [part]}}]}) + "\n\n"
            elif api == "pi-messages":
                payload = {"type": "toolcall_start", "contentIndex": 0, "id": "forbidden", "name": "forbidden"} if tool_call else {"type": "text_delta", "contentIndex": 0, "delta": answer}
                body = f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"
            elif api == "bedrock-converse":
                payload = {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "forbidden", "name": "forbidden"}}}} if tool_call else {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": answer}}}
                encoded = json.dumps(payload).encode()
                prelude = struct.pack(">II", len(encoded) + 16, 0)
                frame = prelude + struct.pack(">I", zlib.crc32(prelude)) + encoded
                body = frame + struct.pack(">I", zlib.crc32(frame))
            else:
                calls = tool_call or (chat and len(requests) == 1)
                delta = {"tool_calls": [{"index": 0, "id": "screen-call", "type": "function", "function": {"name": "observe_screen", "arguments": "{}"}}]} if calls else {"content": "I saw the fixture editor" if chat else answer, "reasoning_content": thinking}
                body = "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": None}]}) + "\n\ndata: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls" if calls else "stop"}]}) + "\n\ndata: [DONE]\n\n"
                if keep_open and not disconnected.is_set():
                    body = "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": None}]}) + "\n\n"
            self.wfile.write(body.encode() if isinstance(body, str) else body)
            self.wfile.flush()
            if keep_open:
                while not disconnected.is_set():
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        disconnected.wait(0.025)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        disconnected.set()
                        return

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
    options = {"accountId": "fixture-account"} if api == "openai-codex-responses" else {"project": "fixture-project"} if api == "google-vertex" else {}
    config = models.create(ModelConfigurationInput(providerId="custom", provider="fixture", api=api, baseUrl=url, model="pixtral-12b" if api == "mistral-conversations" else "vision-only", apiKey="fixture-secret", apiOptions=options))
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


@pytest.mark.parametrize("initial_cancel", ["role", "deadline"])
def test_overlapping_revocations_wait_for_slow_real_http_close(tmp_path, monkeypatch, initial_cancel):
    from httpcore._async.http11 import AsyncHTTP11Connection

    async def run(url, requests):
        service, tool, context, roles, models, sessions, processes = observation_host(tmp_path, url)
        other_configuration = models.create(ModelConfigurationInput(providerId="custom", provider="fixture", baseUrl=url, model="vision-other"))
        other_role = roles.create(RoleInput(name="Other observer", profile={"profile": "other"}, agentConfig={"screenPerception": {"enabled": True, "visionModelConfigurationId": other_configuration.id}}))
        other_session = sessions.open_role_session(other_role.id)
        other_run = AgentRun(runId="other-run", roleId=other_role.id, sessionKey=other_session.sessionKey, status="running", modelSnapshot={"screenPerceptionSource": "desktop_owner"}, startedAt=datetime.now(timezone.utc))
        sessions.create_run_with_messages(other_run, "observe")
        other_context = ToolContext(other_role.id, other_session.sessionKey, other_run.runId, CancellationToken())
        other_tool = ObserveScreenTool(service, other_configuration.id, role_id=other_role.id)
        original_close = AsyncHTTP11Connection.aclose
        closing = asyncio.Event()
        release = asyncio.Event()
        connections = []
        interrupted = []

        async def slow_close(connection):
            connections.append(connection)
            closing.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.append(connection)
                raise
            await original_close(connection)

        monkeypatch.setattr(AsyncHTTP11Connection, "aclose", slow_close)
        started = asyncio.get_running_loop().time()
        observation = asyncio.create_task(tool.execute({}, context))
        revocations = []
        disconnected = False
        try:
            async with asyncio.timeout(23):
                while not requests:
                    await asyncio.sleep(0.01)
                if initial_cancel == "role":
                    revocations.append(asyncio.create_task(service.invalidate(role_id=context.role_id)))
                await closing.wait()
                if initial_cancel == "deadline":
                    # Keep real transport close pending past both the 12-second
                    # vision budget and the 18-second overall deadline.
                    await asyncio.sleep(max(0, 18.2 - (asyncio.get_running_loop().time() - started)))
                identifier = roles.get(context.role_id).agentConfig.screenPerception.visionModelConfigurationId
                revocations.append(asyncio.create_task(service.invalidate(configuration_id=identifier)))
                await asyncio.sleep(0.025)
                revocations.append(asyncio.create_task(service.invalidate(role_id=context.role_id)))
                await asyncio.sleep(0.025)
                context.signal.cancel()
                observation.cancel()
                await asyncio.sleep(0.025)
                observation.cancel()
                await asyncio.sleep(0.025)
                assert not observation.done(), "observation returned before real HTTP close"
                assert all(not task.done() for task in revocations), "revocation returned before real HTTP close"
                assert not interrupted, "repeated cancellation interrupted the transport's cleanup"
                assert not requests.disconnected.is_set()
                # Other allowed runs still see the one occupied observation slot.
                assert json.loads((await other_tool.execute({}, other_context)).content)["error"] == "busy"
        finally:
            release.set()
            await asyncio.gather(observation, *revocations, return_exceptions=True)
            disconnected = await asyncio.to_thread(requests.disconnected.wait, 2)
            if not disconnected:
                # Failure-only fixture cleanup must not make the production
                # disconnect assertion pass by closing escaped sockets itself.
                for connection in connections:
                    await original_close(connection)
        assert disconnected, "production observation did not close the real peer"
        assert processes[0].returncode == 0
        assert not interrupted
        next_result = await other_tool.execute({}, other_context)
        assert next_result.is_error is False, next_result.content
        assert len(processes) == 2 and processes[1].returncode == 0

    with vision_server(hang=True) as (url, requests):
        asyncio.run(run(url, requests))


@pytest.mark.parametrize("api", [
    "openai-completions", "anthropic-messages", "openai-responses", "azure-openai-responses", "openai-codex-responses",
    "google-generative", "google-vertex", "mistral-conversations", "pi-messages", "bedrock-converse",
])
@pytest.mark.parametrize("rejection", ["text", "tool"])
def test_early_vision_rejection_waits_for_owned_real_http_close(tmp_path, monkeypatch, api, rejection):
    from httpcore._async.http11 import AsyncHTTP11Connection

    with vision_server(reply="x" * 17000, api=api, tool_call=rejection == "tool", keep_open=True) as (url, requests):
        async def run():
            service, tool, context, _, _, _, processes = observation_host(tmp_path, url, api=api)
            original_close = AsyncHTTP11Connection.aclose
            closing = asyncio.Event()
            release = asyncio.Event()
            connections = []
            disconnected = False

            async def slow_close(connection):
                connections.append(connection)
                closing.set()
                await release.wait()
                await original_close(connection)

            monkeypatch.setattr(AsyncHTTP11Connection, "aclose", slow_close)
            observation = asyncio.create_task(tool.execute({}, context))
            try:
                async with asyncio.timeout(5):
                    await closing.wait()
                    await asyncio.sleep(0.025)
                    assert not observation.done(), "invalid observation returned before the underlying HTTP close"
                    assert not requests.disconnected.is_set()
                    second = await tool.execute({}, context)
                    assert json.loads(second.content)["error"] == "busy"
                    assert len(processes) == 1 and processes[0].returncode == 0
            finally:
                release.set()
                await asyncio.gather(observation, return_exceptions=True)
                disconnected = await asyncio.to_thread(requests.disconnected.wait, 2)
                if not disconnected:
                    # Only failed production cleanup is repaired by the fixture.
                    for connection in connections:
                        await original_close(connection)
            result = await observation
            assert result.is_error and json.loads(result.content)["error"] == "invalid_observation"
            assert disconnected, "production early rejection did not disconnect its real peer"
            await service.invalidate(role_id=context.role_id)
            assert all(process.returncode is not None for process in processes)
        asyncio.run(run())


def test_discarded_thinking_counts_toward_vision_text_budget(tmp_path):
    with vision_server(thinking="private reasoning " * 1100) as (url, _):
        _, tool, context, *_ = observation_host(tmp_path, url)
        result = asyncio.run(tool.execute({}, context))
    assert result.is_error and json.loads(result.content)["error"] == "invalid_observation"
    assert "private reasoning" not in result.content


def test_hanging_vision_deadline_returns_timeout_after_real_disconnect(tmp_path):
    with vision_server(hang=True) as (url, requests):
        async def run():
            _, tool, context, _, _, _, processes = observation_host(tmp_path, url)
            async with asyncio.timeout(16):
                result = await tool.execute({}, context)
            assert result.is_error and json.loads(result.content)["error"] == "timeout"
            assert processes[0].returncode == 0
        asyncio.run(run())
        assert requests.disconnected.wait(2)


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
