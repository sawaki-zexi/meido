"""External summaries reach real HTTP model requests through the public chat API."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.channels.consolidation import ChannelMemoryMaintenance
from backend.app.channels.group_tools import MultichannelGroupRepository
from test_background_shell_api import api as shell_api
from test_spawn_api import api
from test_proactive_mcp import source
from test_proactive_snapshot import grant_for


@pytest.fixture
def local_model():
    state = SimpleNamespace(requests=[], respond=lambda request: {"role": "assistant", "content": "完成"})
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(request)
            message = state.respond(request)
            finish = "tool_calls" if message.get("tool_calls") else "stop"
            if request.get("stream"):
                for index, call in enumerate(message.get("tool_calls", [])):
                    call["index"] = index
                body = ("data: " + json.dumps({"choices": [{"index": 0, "delta": message, "finish_reason": None}]}, ensure_ascii=False)
                    + "\n\ndata: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
                    + "\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps({"choices": [{"index": 0, "message": message, "finish_reason": finish}]}).encode()
                content_type = "application/json"
            self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever); worker.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1"
    yield state
    server.shutdown(); server.server_close(); worker.join(5)
    assert not worker.is_alive()


def configure(client, local_model, monkeypatch):
    monkeypatch.setattr(main, "model_adapter", RoutedModelAdapter(ModelAdapterRegistry()))
    configuration = client.post("/api/model/configurations", json={"providerId": "custom", "provider": "local",
        "baseUrl": local_model.url, "model": "memory-fixture", "apiKey": "fixture", "contextWindowTokens": 24000}).json()["configuration"]
    role = client.post("/api/roles", json={"name": "记忆角色", "profile": {"profile": "本地测试"},
        "agentConfig": {"enabledTools": ["list_dir"]}}).json()["role"]
    assert client.put(f"/api/roles/{role['id']}/model-configuration", json={"configurationId": configuration["id"]}).status_code == 200
    groups = MultichannelGroupRepository(main.multichannel_store)
    monkeypatch.setattr(main, "group_repository", groups)
    return role["id"], groups


def test_desktop_requests_refresh_activity_authority_after_tool_wait(api, local_model, monkeypatch):
    client, _, _, _ = api
    role_id, groups = configure(client, local_model, monkeypatch)
    channels = main.multichannel_store
    channels.register_channel_account(role_id, "qq", account_id="qq")
    group = channels.ensure_channel_thread(role_id, "qq", "qq", "group", "group")
    visitor = channels.ensure_channel_thread(role_id, "qq", "qq", "22", "private")
    groups.update_context(role_id, group.thread_id, summary="群聊最近动态-703")
    with channels._connect() as db:
        channels.external_memory.write_context_in(db, visitor, summary="配对前访客动态-704")
    channels.append_event(group, direction="inbound", content="不可共享的群原文-705", external_message_id="raw", metadata={"senderId": "23"})
    def respond(request):
        if len(local_model.requests) == 1:
            code = channels.create_pairing_code()
            assert channels.consume_pairing_code(code, role_id=role_id, channel="qq", scope="platform", platform_user_id="22", account_id="qq", chat_id="22")
            return {"role": "assistant", "tool_calls": [{"id": "lookup", "type": "function", "function": {"name": "list_dir", "arguments": '{"path":"."}'}}]}
        return {"role": "assistant", "content": "动态读取完成"}
    local_model.respond = respond
    result = client.post(f"/api/roles/{role_id}/messages", json={"content": "最近发生了什么"})
    assert "动态读取完成" in result.text, result.text
    assert len(local_model.requests) == 2
    first, second = [json.dumps(request["messages"], ensure_ascii=False) for request in local_model.requests]
    assert "配对前访客动态-704" in first and "配对前访客动态-704" not in second
    assert all("群聊最近动态-703" in text and "不可共享的群原文-705" not in text for text in (first, second))


def test_real_model_maintenance_separates_notes_from_member_facts(api, local_model, monkeypatch):
    client, _, _, _ = api
    role_id, groups = configure(client, local_model, monkeypatch)
    channels = main.multichannel_store
    channels.register_channel_account(role_id, "qq", account_id="qq", allow_group=True)
    thread = channels.ensure_channel_thread(role_id, "qq", "qq", "group", "group")
    for index in range(20):
        channels.append_event(thread, direction="inbound", content="主人参与书会" if index == 0 else "访客聊书",
            external_message_id=str(index), metadata={"senderId": "11" if index == 0 else "22", "senderIsOwner": index == 0})
    local_model.respond = lambda request: {"role": "assistant", "content": json.dumps({"groupNote": "书会笔记", "summary": "书会讨论",
        "members": [{"senderId": "22", "profile": "爱读书", "shorthand": "书友"}, {"senderId": "11", "profile": "应拒绝"}]}, ensure_ascii=False)}
    worker = ChannelMemoryMaintenance(lambda: channels, lambda: groups, main._summarize_channel_memory, lambda *a: None)
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=False))
    request = local_model.requests[0]
    prompt = json.loads(request["messages"][1]["content"])
    assert any(message["content"] == "主人参与书会" for message in prompt["messages"])
    assert set(prompt["profiles"]) == {"22"}
    assert all(message["metadata"]["senderId"] == "22" for message in prompt["memberFactsMessages"])
    saved = client.get(f"/api/roles/{role_id}/channels/threads/{thread.thread_id}/external-memory").json()
    assert saved["note"] == "书会笔记" and saved["members"][0]["profile"] == "爱读书"
    assert [item["senderId"] for item in saved["members"]] == ["22"]


def test_oversized_activity_is_dropped_without_losing_source_of_fitting_items(api, local_model, monkeypatch):
    client, _, _, _ = api
    role_id, groups = configure(client, local_model, monkeypatch)
    channels = main.multichannel_store
    channels.register_channel_account(role_id, "qq", account_id="qq")
    short = channels.ensure_channel_thread(role_id, "qq", "qq", "short", "group", title="书友群")
    long = channels.ensure_channel_thread(role_id, "qq", "qq", "long", "group", title="大摘要")
    groups.update_context(role_id, short.thread_id, summary="可容纳动态-709")
    groups.update_context(role_id, long.thread_id, summary="超长" * 6000)
    result = client.post(f"/api/roles/{role_id}/messages", json={"content": "看看最近动态"})
    assert "assistant_completed" in result.text, result.text
    prompt = json.dumps(local_model.requests[-1], ensure_ascii=False)
    assert "超长" not in prompt and "可容纳动态-709" in prompt and short.thread_id in prompt and "书友群" in prompt


def test_active_decision_requests_refresh_the_same_activity_scope(api, source, local_model, monkeypatch, tmp_path):
    from backend.app.mcp_runtime import McpHost
    from backend.app.plugin_configuration import PluginConfigurationStore
    from test_proactive_drift_delivery import settled
    client, _, _, _ = api
    role_id, groups = configure(client, local_model, monkeypatch)
    plugins = PluginConfigurationStore(tmp_path / "plugins.json")
    pending = plugins.prepare("autonomy", expected_revision=0, enabled=True); plugins.finish("autonomy", pending.revision)
    monkeypatch.setattr(main, "plugin_configuration", plugins)
    _, mcp_store, data, *_ = source
    host = McpHost(mcp_store)
    monkeypatch.setattr(main, "mcp_host", host)
    monkeypatch.setattr(main, "_proactive", None)
    now = [1780000000.0]
    monkeypatch.setattr(main, "_proactive_clock", lambda: now[0])
    server = mcp_store.save(data.model_copy(update={"roleIds": [role_id]}))
    grant = grant_for(source).model_copy(update={"configRevision": server.revision, "grantRevision": server.grant_id})
    model_id = main.store.get(role_id).modelConfigurationId
    response = client.put(f"/api/roles/{role_id}/autonomy", json={"expectedRevision": 0, "config": {
        "activeDecisionEnabled": True, "decisionModelConfigurationId": model_id, "consentRevision": 1,
        "sourceGrants": [grant.model_dump(mode="json")]}})
    assert response.status_code == 200, response.text
    channels = main.multichannel_store
    channels.register_channel_account(role_id, "qq", account_id="qq")
    private = channels.ensure_channel_thread(role_id, "qq", "qq", "22", "private")
    with channels._connect() as db:
        channels.external_memory.write_context_in(db, private, summary="主动回合访客动态-710")
    def respond(request):
        if len(local_model.requests) == 1:
            code = channels.create_pairing_code()
            assert channels.consume_pairing_code(code, role_id=role_id, channel="qq", scope="platform", platform_user_id="22", account_id="qq", chat_id="22")
            name, arguments = "get_content_events", {}
        else:
            name, arguments = "finish_turn", {"decision": "skip", "reason": "other", "note": "仅检查资料范围"}
        return {"role": "assistant", "tool_calls": [{"id": str(len(local_model.requests)), "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
    local_model.respond = respond
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive_application(), role_id))
        assert state["runs"][0]["status"] == "skipped", state
        assert len(local_model.requests) == 2
        assert "主动回合访客动态-710" in json.dumps(local_model.requests[0], ensure_ascii=False)
        assert "主动回合访客动态-710" not in json.dumps(local_model.requests[1], ensure_ascii=False)
    finally:
        asyncio.run(main._proactive.close()); asyncio.run(host.close())


def test_drift_requests_refresh_the_same_activity_scope(api, local_model, monkeypatch, tmp_path):
    from test_proactive_drift_delivery import prepare, settled
    from backend.app.plugin_configuration import PluginConfigurationStore
    plugins = PluginConfigurationStore(tmp_path / "plugins.json")
    pending = plugins.prepare("autonomy", expected_revision=0, enabled=True); plugins.finish("autonomy", pending.revision)
    monkeypatch.setattr(main, "plugin_configuration", plugins)
    client, _, _, _ = api
    role_id, path, now = prepare(client, monkeypatch)
    configuration = client.get(path).json()["config"]["driftModelConfigurationId"]
    response = client.put(f"/api/model/configurations/{configuration}", json={"providerId": "custom", "provider": "local",
        "baseUrl": local_model.url, "model": "local-drift-only", "apiKey": "fixture", "contextWindowTokens": 24000})
    assert response.status_code == 200, response.text
    monkeypatch.setattr(main, "model_adapter", RoutedModelAdapter(ModelAdapterRegistry()))
    channels = main.multichannel_store
    channels.register_channel_account(role_id, "qq", account_id="qq")
    private = channels.ensure_channel_thread(role_id, "qq", "qq", "22", "private")
    with channels._connect() as db:
        channels.external_memory.write_context_in(db, private, summary="Drift访客动态-711")
    def respond(request):
        if len(local_model.requests) == 1:
            code = channels.create_pairing_code()
            assert channels.consume_pairing_code(code, role_id=role_id, channel="qq", scope="platform", platform_user_id="22", account_id="qq", chat_id="22")
            name, arguments = "activate_skill", {"skill_id": "reading-notes"}
        else:
            name, arguments = "finish_drift", {"skill_used": "reading-notes", "one_line": "检查最近活动范围", "next_action": "继续记笔记", "message_result": "silent"}
        return {"role": "assistant", "tool_calls": [{"id": str(len(local_model.requests)), "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
    local_model.respond = respond
    try:
        now[0] += 1800
        try:
            state = asyncio.run(settled(main._proactive, role_id))
        except TimeoutError:
            pytest.fail(str({"state": main._proactive.query(role_id), "enabled": main.plugin_configuration.enabled("autonomy"), "errors": main._proactive.errors}))
        assert state["runs"][0]["status"] == "finished_silent", state
        assert len(local_model.requests) == 2
        assert "Drift访客动态-711" in json.dumps(local_model.requests[0], ensure_ascii=False)
        assert "Drift访客动态-711" not in json.dumps(local_model.requests[1], ensure_ascii=False)
    finally:
        asyncio.run(main._proactive.close())
