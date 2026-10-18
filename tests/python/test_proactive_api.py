"""Owner HTTP config drives real MCP and a real localhost OpenAI SSE adapter."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading

import pytest

from backend.app import main
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.mcp_runtime import McpHost
from test_background_shell_api import api as shell_api
from test_spawn_api import api
from test_proactive_mcp import source
from test_proactive_snapshot import grant_for


pytestmark = pytest.mark.usefixtures("local_platform_only")


@pytest.fixture
def provider():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if request["messages"][-1].get("content") == "主人正常输入":
                delta, reason = {"role": "assistant", "content": "主人回合正常完成"}, "stop"
            else:
                step = len(requests)
                if step == 1:
                    name, arguments = "get_content_events", {}
                elif step == 2:
                    identifier = json.loads(request["messages"][-1]["content"])[0]["id"]
                    name, arguments = "message_push", {"message": "HTTP自主消息证据", "mood": "calm",
                        "thought": "只消费本地来源", "evidence": [identifier]}
                else:
                    name, arguments = "finish_turn", {"decision": "reply"}
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"local-{step}",
                    "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}
                reason = "tool_calls"
            body = ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}, ensure_ascii=False)
                + "\n\ndata: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
                + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", requests
    server.shutdown()
    server.server_close()
    worker.join(5)
    assert not worker.is_alive()


def test_http_configuration_real_model_source_receipt_and_next_owner_history(api, source, provider, monkeypatch):
    client, roles, sessions, tasks = api
    from backend.app.plugin_configuration import PluginConfigurationStore
    monkeypatch.setattr(main, 'plugin_configuration', PluginConfigurationStore(roles.root.parent / 'plugins.json'))
    root, mcp_store, data, *_ = source
    now, host = [1780000000.0], McpHost(mcp_store)
    monkeypatch.setattr(main, "mcp_host", host)
    monkeypatch.setattr(main, "_proactive", None, raising=False)
    monkeypatch.setattr(main, "_proactive_clock", lambda: now[0], raising=False)
    monkeypatch.setattr(main, "model_adapter", RoutedModelAdapter(ModelAdapterRegistry()))
    configuration = client.post("/api/model/configurations", json={"providerId": "custom", "provider": "local",
        "baseUrl": provider[0], "model": "proactive-fixture", "apiKey": "fixture-not-a-real-key"}).json()["configuration"]
    role = client.post("/api/roles", json={"name": "主动角色", "profile": {"profile": "唯一角色设定177"}}).json()["role"]
    role_id = role["id"]
    config = mcp_store.save(data.model_copy(update={"roleIds": [role_id]}))
    grant = grant_for(source).model_copy(update={"configRevision": config.revision, "grantRevision": config.grant_id})
    path = f"/api/roles/{role_id}/autonomy"
    defaults = client.get(path)
    assert defaults.status_code == 200, defaults.text
    assert defaults.json()["config"]["activeDecisionEnabled"] is False
    saved = client.put(path, json={"expectedRevision": 0, "config": {"activeDecisionEnabled": True,
        "decisionModelConfigurationId": configuration["id"], "consentRevision": 1,
        "sourceGrants": [grant.model_dump(mode="json")]}})
    assert saved.status_code == 200, saved.text
    assert not (root / "pid").exists() and provider[1] == []
    assert client.put(path, json={"expectedRevision": 0, "config": saved.json()["config"]}).status_code == 409

    async def exercise():
        now[0] += 1800
        application = main._proactive_application()
        assert await application.on_clock() == 0
        assert provider[1] == [] and not (root / 'pid').exists()
        # Global suspension retains this role's exact strategy and grants.
        assert application.query(role_id)['config']['activeDecisionEnabled']
        state = main.plugin_configuration.prepare('autonomy', expected_revision=0, enabled=True)
        main.plugin_configuration.finish('autonomy', state.revision)
        await application.on_clock()
        async with asyncio.timeout(12):
            while True:
                await application.recover()
                rows = application.query(role_id)["runs"]
                if rows and rows[0]["runtimeAcknowledged"]:
                    break
                await asyncio.sleep(.01)
        assert rows[0]["status"] == "sent", rows
    try:
        asyncio.run(exercise())
        assert len(provider[1]) == 3
        assert "唯一角色设定177" in json.dumps(provider[1][0], ensure_ascii=False)
        assert all(request["max_tokens"] == 1024 for request in provider[1])
        history = client.get(f"/api/roles/{role_id}/session").json()["messages"]
        assert len(history) == 1 and history[0]["content"] == "HTTP自主消息证据"
        assert history[0]["metadata"]["deliveryStatus"] == "sent"
        owner = client.post(f"/api/roles/{role_id}/messages", json={"content": "主人正常输入"})
        assert "assistant_completed" in owner.text, owner.text
        assert "HTTP自主消息证据" in json.dumps(provider[1][-1], ensure_ascii=False)
        assert client.get(path).json()["humanBusy"] is False
        assert len([message for message in sessions.list_messages(f"role:{role_id}") if message.role == "user"]) == 1
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())
        asyncio.run(host.close())


def test_owner_reservation_database_failure_ends_human_priority_without_fake_history(api, monkeypatch):
    client, _, sessions, _ = api
    monkeypatch.setattr(main, "_proactive", None)
    role = client.post("/api/roles", json={"name": "写入失败", "profile": {"profile": "本地"}}).json()["role"]
    path = f"/api/roles/{role['id']}/autonomy"
    assert client.put(path, json={"expectedRevision": 0, "config": {}}).status_code == 200
    with sqlite3.connect(sessions.database_path) as db:
        db.execute("CREATE TRIGGER fail_real_owner_run BEFORE INSERT ON agent_runs BEGIN SELECT RAISE(ABORT,'injected owner reservation failure'); END")
    try:
        response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "不能产生假的成功回合"})
        assert response.status_code == 409
        assert client.get(path).json()["humanBusy"] is False
        assert sessions.list_messages(f"role:{role['id']}") == []
    finally:
        asyncio.run(main._proactive.close())
