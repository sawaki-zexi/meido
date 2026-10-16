"""A01 tools use real source/Runtime admission and keep model actions scoped."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace

import pytest

from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.mcp_autonomy import declaration_hash
from backend.app.mcp_runtime import McpHost
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.proactive_runtime.identity import AutonomyOrigin
from backend.app.proactive_runtime.turn import DecisionTurn
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskRuntime
from test_mcp_http import process_alive
from test_proactive_mcp import source  # noqa: F401
from test_proactive_snapshot import grant_for


@pytest.fixture(autouse=True)
def protocol_environment(tmp_path, monkeypatch, local_platform_only):
    monkeypatch.setenv("MEIDO_DATA_DIR", str(tmp_path / "isolated-data"))
    monkeypatch.setenv("MEIDO_ROLES_DIR", str(tmp_path / "isolated-roles"))
    monkeypatch.setenv("MEIDO_OWNER_KNOWLEDGE_DATA_DIR", str(tmp_path / "isolated-owner"))
    monkeypatch.delenv("MEIDO_PROJECT_ROOT", raising=False)
    yield


def call(name, **arguments):
    return ModelToolCallBlock(name, name, arguments)


def skip():
    return call("finish_turn", decision="skip", reason="other", note="Explicit local fixture decision")


def draft(message, evidence=()):
    return call("message_push", message=message, mood="neutral", thought="Local protocol acceptance",
                evidence=list(evidence))


class ScriptedProtocol:
    def __init__(self):
        self.requests = []
        self.script = lambda step, messages: skip()

    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        self.requests.append((messages, tools, max_tokens))
        blocks = self.script(len(self.requests), messages)
        if isinstance(blocks, ModelToolCallBlock):
            blocks = (blocks,)
        yield ModelStreamDone(ModelAssistantMessage(content=tuple(blocks)))

    def result(self, request_number):
        return self.requests[request_number - 1][0][-1]


@pytest.fixture
def protocol(source, tmp_path):
    root, store, server_input, *_ = source
    declaration = {"name": "ack", "description": "explicit single legacy acknowledgement",
        "inputSchema": {"type": "object", "properties": {
            "event_ids": {"type": "array", "items": {"type": "string"}},
            "ttl_hours": {"type": "integer", "minimum": 0}},
            "required": ["event_ids", "ttl_hours"], "additionalProperties": False}}
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    (root / "catalog.json").write_text(json.dumps([*catalog, declaration]), encoding="utf-8")
    server = store.save(server_input)
    grant = grant_for(source).model_copy(update={"ackTool": "ack", "ackDefinitionHash": declaration_hash(declaration),
        "ackContract": "legacy-ttl", "configRevision": server.revision, "grantRevision": server.grant_id})
    data = tmp_path / "protocol"
    initialize_databases(data)
    sessions, tasks, host = SessionStore(data / "sessions.db"), TaskRuntime(data / "tasks"), McpHost(store)
    now, adapter, callbacks = [1780000000.0], ScriptedProtocol(), []
    role = {"id": "role", "name": "Protocol fixture", "profile": {"profile": "Owned local source"},
        "agentConfig": {"memoryRecall": {"enabled": False}, "enabledTools": []}}
    records = [{"role": owner, "target": target, "content": content} for owner, target, content in (
        ("role", "desktop", "first"), ("role", "desktop", "second"), ("role", "desktop", "third"),
        ("another-role", "desktop", "foreign-role"), ("role", "qq:other", "foreign-target"))]

    async def recall(origin, query, *, limit):
        assert origin.role_id == "role" and origin.allowed_modes == ("active_decision",)
        callbacks.append(("recall_memory", origin, limit))
        # Deliberately return three eligible hits: the host must still cap two.
        return [item["content"] for item in records if item["role"] == origin.role_id and item["target"] == "desktop"]

    async def webpages(origin, name, arguments):
        assert origin.role_id == "role" and origin.allowed_modes == ("active_decision",)
        callbacks.append((name, origin, arguments))
        return {"body": "local response", "source": name}

    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0],
        role_getter=lambda key: role if key == role["id"] else None,
        model_getter=lambda key: {"id": "explicit-model"} if key == "explicit-model" else None,
        mcp_host=host, adapter_factory=lambda config: adapter, recall_memory=recall, web_tools=webpages)
    app.configure("role", AutonomyConfig(activeDecisionEnabled=True,
        decisionModelConfigurationId="explicit-model", consentRevision=1, sourceGrants=[grant]), expected_revision=0)
    yield SimpleNamespace(app=app, sessions=sessions, tasks=tasks, host=host, now=now, adapter=adapter,
        root=root, role=role, callbacks=callbacks)
    asyncio.run(app.close())
    asyncio.run(host.close())
    tasks.close()
    if (root / "pid").exists():
        assert not process_alive(int((root / "pid").read_text()))


def tick(rig):
    async def exercise():
        rig.now[0] += 1800
        assert await rig.app.on_clock() == 1
        async with asyncio.timeout(10):
            while True:
                await rig.app.recover()
                state = rig.app.query("role")
                if state["runs"] and all(run["runtimeAcknowledged"] for run in state["runs"]):
                    return state
                await asyncio.sleep(.01)
    return asyncio.run(exercise())


def remote_calls(rig):
    return [json.loads(line) for line in (rig.root / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def test_multi_tool_response_executes_neither_draft_nor_scoped_callback(protocol):
    rig = protocol
    rig.role["agentConfig"]["memoryRecall"]["enabled"] = True
    rig.adapter.script = lambda step, messages: (
        (draft("must never be staged"), call("recall_memory", query="owned query")) if step == 1 else skip())
    state = tick(rig)
    assert state["runs"][0]["status"] == "skipped"
    results = [message for message in rig.adapter.requests[1][0] if getattr(message, "role", None) == "tool_result"]
    assert len(results) == 2 and all(message.is_error for message in results)
    assert rig.callbacks == [] and state["acknowledgements"] == []
    assert rig.sessions.list_messages("role:role") == []
    assert len(remote_calls(rig)) == 1
    names = {tool["function"]["name"] for tool in rig.adapter.requests[0][1]}
    assert names == {"get_alert_events", "get_content_events", "get_context_data", "recall_memory", "get_content",
        "web_fetch", "web_search", "get_recent_chat", "message_push", "mark_interesting", "mark_not_interesting", "finish_turn"}


def test_second_draft_and_skip_with_existing_draft_are_rejected_and_only_first_is_sent(protocol):
    rig = protocol
    steps = [draft("first immutable draft"), draft("replacement must not send"), skip(), call("finish_turn", decision="reply")]
    rig.adapter.script = lambda step, messages: steps[step - 1]
    state = tick(rig)
    assert rig.adapter.result(3).is_error and rig.adapter.result(4).is_error
    assert state["runs"][0]["status"] == "sent"
    messages = rig.sessions.list_messages("role:role")
    assert len(messages) == 1 and messages[0].content == "first immutable draft"
    assert messages[0].metadata["internalEvent"] == "active_decision"
    for _ in range(3):
        asyncio.run(rig.app.recover())
    assert len(rig.sessions.list_messages("role:role")) == 1


@pytest.mark.parametrize("tool", ["get_content", "mark_interesting", "mark_not_interesting", "message_push"])
def test_unknown_item_never_creates_interest_ack_or_draft(protocol, tool):
    rig = protocol
    invalid = draft("untrusted evidence", ["not-in-this-tick"]) if tool == "message_push" else call(tool, item_ids=["not-in-this-tick"])
    rig.adapter.script = lambda step, messages: invalid if step == 1 else skip()
    state = tick(rig)
    assert rig.adapter.result(2).is_error
    assert state["runs"][0]["status"] == "skipped" and state["acknowledgements"] == []
    assert rig.sessions.list_messages("role:role") == []


def test_previous_tick_item_is_rejected_when_not_in_current_snapshot(protocol):
    rig, previous = protocol, []
    def first(step, messages):
        if step == 1:
            return call("get_content_events")
        previous.append(json.loads(messages[-1].content[0].text)[0]["id"])
        return skip()
    rig.adapter.script = first
    assert tick(rig)["runs"][0]["status"] == "skipped"
    (rig.root / "events.json").write_text(json.dumps([{"id": "new-event", "kind": "content", "title": "New candidate"}]), encoding="utf-8")
    offset = len(rig.adapter.requests)
    def second(step, messages):
        if step == offset + 1:
            return call("get_content_events")
        if step == offset + 2:
            assert json.loads(messages[-1].content[0].text)[0]["id"] != previous[0]
            return call("get_content", item_ids=previous)
        return skip()
    rig.adapter.script = second
    state = tick(rig)
    assert rig.adapter.result(offset + 3).is_error
    assert state["acknowledgements"] == [] and len(remote_calls(rig)) == 2


def test_ordinary_skip_acks_only_explicit_discard_not_interesting_or_body_error(protocol):
    rig = protocol
    (rig.root / "events.json").write_text(json.dumps([
        {"id": name, "kind": "content", "title": name} for name in ("interesting", "discard", "body-error")]), encoding="utf-8")
    identifiers = {}
    def script(step, messages):
        if step == 1:
            return call("get_content_events")
        if step == 2:
            identifiers.update({item["remoteId"]: item["id"] for item in json.loads(messages[-1].content[0].text)})
            return call("get_content", item_ids=list(identifiers.values()))
        if step == 3:
            return call("mark_interesting", item_ids=[identifiers["interesting"]])
        if step == 4:
            return call("mark_not_interesting", item_ids=[identifiers["discard"]])
        return skip()
    rig.adapter.script = script
    state = tick(rig)
    bodies = json.loads(rig.adapter.result(3).content[0].text)
    assert bodies[identifiers["body-error"]] == {"body": "", "error": "body is unavailable"}
    assert len(state["acknowledgements"]) == 1
    ack = state["acknowledgements"][0]
    assert ack["reason"] == "discarded" and ack["status"] == "acknowledged"
    assert ack["expiresAt"] - ack["decisionAt"] == 720 * 3600
    sent = [item for item in remote_calls(rig) if item["name"] == "ack"]
    assert len(sent) == 1 and sent[0]["arguments"]["event_ids"] == ["discard"]
    assert rig.sessions.list_messages("role:role") == []


def test_three_source_channels_and_repeated_content_reads_use_only_frozen_snapshot(protocol):
    rig = protocol
    (rig.root / "events.json").write_text(json.dumps([
        {"id": "alert", "kind": "alert", "title": "Explicit alert"},
        {"id": "content", "kind": "content", "title": "Article", "body": "frozen article body"},
        {"id": "context", "kind": "context", "data": {"context": "approved context"}}]), encoding="utf-8")
    config = AutonomyConfig.model_validate(rig.app.query("role")["config"])
    grant = config.sourceGrants[0]
    rig.app.configure("role", config.model_copy(update={"sourceGrants": [grant,
        grant.model_copy(update={"sourceId": "alerts", "channel": "alert"}),
        grant.model_copy(update={"sourceId": "context", "channel": "context"})]}), expected_revision=config.revision)
    content_id = []
    def script(step, messages):
        if step in {1, 6}:
            return call("get_alert_events")
        if step in {2, 7}:
            return call("get_context_data")
        if step == 3:
            return call("get_content_events")
        if step == 4:
            content_id.append(json.loads(messages[-1].content[0].text)[0]["id"])
            return call("get_content", item_ids=content_id)
        if step == 5:
            return call("get_content", item_ids=content_id)
        return skip()
    rig.adapter.script = script
    state = tick(rig)
    assert state["runs"][0]["status"] == "skipped" and state["acknowledgements"] == []
    assert json.loads(rig.adapter.result(2).content[0].text)[0]["remoteId"] == "alert"
    assert json.loads(rig.adapter.result(3).content[0].text)[0]["data"] == {"context": "approved context"}
    body = {content_id[0]: {"body": "frozen article body", "error": None}}
    assert json.loads(rig.adapter.result(5).content[0].text) == body
    assert json.loads(rig.adapter.result(6).content[0].text) == body
    assert len(remote_calls(rig)) == 3 and all(item["name"] == "get_events" for item in remote_calls(rig))


def test_recent_chat_includes_only_twenty_completed_ordinary_desktop_messages(protocol):
    rig = protocol
    rig.sessions.open_role_session("role")
    for index in range(22):
        rig.sessions.append_message("role:role", "user" if index % 2 == 0 else "assistant", f"ordinary-{index}")
    rig.sessions.append_message("role:role", "assistant", "autonomous text", metadata={"internalEvent": "active_decision"})
    rig.sessions.append_message("role:role", "assistant", "drift text", metadata={"internalEvent": "drift"})
    rig.sessions.append_message("role:role", "assistant", "task completion", message_type="internal_completion")
    rig.sessions.append_message("role:role", "assistant", "incomplete", status="failed")
    rig.sessions.append_message("role:role", "tool", "tool result")
    rig.sessions.append_message("channel:qq:unshared", "user", "other channel and listener")
    rig.sessions.open_role_session("another-role")
    rig.sessions.append_message("role:another-role", "user", "another role")
    rig.adapter.script = lambda step, messages: call("get_recent_chat", n=20) if step == 1 else skip()
    assert tick(rig)["runs"][0]["status"] == "skipped"
    recent = json.loads(rig.adapter.result(2).content[0].text)
    assert [item["content"] for item in recent] == [f"ordinary-{index}" for index in range(2, 22)]


def test_memory_caps_two_current_role_target_hits_and_web_uses_actual_admitted_origin(protocol):
    rig = protocol
    rig.role["agentConfig"] = {"memoryRecall": {"enabled": True}, "enabledTools": ["web_fetch", "web_search"]}
    steps = [call("recall_memory", query="owned query"), call("web_fetch", url="http://127.0.0.1/fixture"),
             call("web_search", query="local fixture"), skip()]
    rig.adapter.script = lambda step, messages: steps[step - 1]
    state = tick(rig)
    assert state["runs"][0]["status"] == "skipped"
    assert json.loads(rig.adapter.result(2).content[0].text) == ["first", "second"]
    assert [entry[0] for entry in rig.callbacks] == ["recall_memory", "web_fetch", "web_search"]
    assert rig.callbacks[0][2] == 2
    assert all(entry[1].tick_id == state["runs"][0]["id"] for entry in rig.callbacks)


@pytest.mark.parametrize("tool", ["recall_memory", "web_fetch", "web_search"])
def test_model_cannot_dispatch_scoped_callback_without_frozen_role_grant(protocol, tool):
    rig = protocol
    arguments = {"url": "http://127.0.0.1/fixture"} if tool == "web_fetch" else {"query": "owned query"}
    rig.adapter.script = lambda step, messages: call(tool, **arguments) if step == 1 else skip()
    assert tick(rig)["runs"][0]["status"] == "skipped"
    assert rig.adapter.result(2).is_error and rig.callbacks == []


@pytest.mark.parametrize("initially_granted", [True, False])
def test_changed_web_role_grant_cannot_revoke_then_dispatch_or_widen_current_tick(protocol, initially_granted):
    rig = protocol
    rig.role["agentConfig"]["enabledTools"] = ["web_fetch"] if initially_granted else []
    def script(step, messages):
        if step == 1:
            return call("get_content_events")
        rig.role["agentConfig"]["enabledTools"] = [] if initially_granted else ["web_fetch"]
        return call("web_fetch", url="http://127.0.0.1/fixture")
    rig.adapter.script = script
    state = tick(rig)
    assert state["runs"][0]["status"] == "aborted"
    assert rig.callbacks == [] and rig.sessions.list_messages("role:role") == []


def test_finish_cas_and_empty_snapshot_ids_use_real_host_admitted_origin(protocol):
    rig = protocol
    (rig.root / "events.json").write_text("[]", encoding="utf-8")
    state = tick(rig)
    run = state["runs"][0]
    assert run["status"] == "no_events" and rig.adapter.requests == []
    actual = run["origin"]
    origin = AutonomyOrigin(actual["roleId"], actual["tickId"], actual["grantRevision"], actual["generation"], tuple(actual["allowedModes"]))
    turn = DecisionTurn(rig.app, origin, AutonomyConfig.model_validate(state["config"]),
        {"alerts": [], "contents": [], "contexts": [], "bodies": {}, "bodyErrors": {}})
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(turn.dispatch("get_content", {"item_ids": ["forged-empty-id"]}))
    with pytest.raises(ValueError, match="requires a draft"):
        turn.finish({"decision": "reply"})
    with pytest.raises(ValueError, match="schema"):
        asyncio.run(turn.dispatch("finish_turn", {"decision": "skip", "reason": "invented-reason"}))
    turn.draft({"message": "first CAS draft", "evidence": [], "mood": "neutral", "thought": "fixture"})
    with pytest.raises(ValueError, match="already used"):
        turn.draft({"message": "replacement", "evidence": []})
    with pytest.raises(ValueError, match="skip requires no draft"):
        turn.finish({"decision": "skip", "reason": "other"})
    barrier = threading.Barrier(2)
    def finish_concurrently():
        barrier.wait(timeout=3)
        return turn.finish({"decision": "reply"})
    with ThreadPoolExecutor(max_workers=2) as workers:
        first, same = list(workers.map(lambda _: finish_concurrently(), range(2)))
    assert first == same
    assert turn.finish({"decision": "reply"}) == first
    with pytest.raises(ValueError, match="conflicting"):
        turn.finish({"decision": "skip", "reason": "other"})
    with pytest.raises(ValueError, match="already used"):
        turn.draft({"message": "after finish", "evidence": []})
    assert rig.sessions.list_messages("role:role") == []
