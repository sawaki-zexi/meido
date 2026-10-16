from fastapi.testclient import TestClient
import os
from pathlib import Path
import pytest

from backend.app import main
from backend.app.role_store import RoleStore
from backend.app.computer_use import COMPUTER_USE_TOOL_NAMES

pytestmark=pytest.mark.usefixtures("local_platform_only")


@pytest.mark.parametrize("native", [False, True], ids=["protocol-fixture", "owned-native-window"])
def test_http_owner_run_passes_window_image_to_current_chat_only(tmp_path, monkeypatch, native):
    if native and not os.environ.get("MEIDO_COMPUTER_USE_NATIVE_RESOURCES"):
        pytest.skip("explicit owned-window native resources required")
    import json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from backend.app.model_config import ModelConfigurationStore
    from backend.app.models import ModelConfigurationInput, RoleInput
    from backend.app.memory_config import MemorySettingsStore
    from backend.app.session_manager import SessionManager
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    from backend.app.agent_runtime import RuntimeManager
    from backend.app.ai.model_adapter_registry import RoutedModelAdapter, ModelAdapterRegistry
    from backend.app.computer_use import ComputerUseHost
    from test_computer_use import Desktop, Driver, Lease
    window = None
    fixture_pid, fixture_hwnd = 42, 81
    if native:
        from test_computer_use_native import fixture_module
        window = fixture_module.FixtureWindow(tmp_path / "http-owned-window")
        native_state = window.state()
        fixture_pid, fixture_hwnd = native_state["pid"], native_state["a"]["hwnd"]
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            phase = len(requests)
            calls = []
            if phase == 1:
                calls = [("computer_list_windows", {"pid":fixture_pid})]
            elif phase in range(2,19,2):
                if native: time.sleep(.15)
                calls = [("computer_get_window_state", {"pid":fixture_pid,"window_id":fixture_hwnd})]
            elif phase in range(3,18,2):
                candidates = []
                for message in reversed(payload["messages"]):
                    content = message.get("content") or ""
                    text = content if isinstance(content,str) else "\n".join(x.get("text", "") for x in content)
                    if '"observation_id"' in text:
                        candidates.append(json.loads(text[text.index("{"):]))
                assert candidates
                observed = candidates[0]
                action = {k:observed[k] for k in ("pid","window_id","snapshot_id","observation_id")}
                action["delivery_mode"] = "foreground"
                name={3:"click",5:"type_text",7:"press_key",9:"hotkey"}.get(phase,"scroll")
                if name!="scroll":
                    label="Fixture button" if name=="click" else "Fixture input"
                    element=next((e for e in observed["elements"] if e.get("label")==label),observed["elements"][0])
                    action["element_token"]=element["element_token"]
                if name=="type_text": action["text"]="你好 HTTP"
                if name=="press_key": action["key"]="end"
                if name=="hotkey": action["keys"]=["ctrl","a"]
                if name=="scroll": action.update(direction={11:"down",13:"up",15:"right",17:"left"}[phase],amount=1,x=80,y=160)
                calls = [("computer_"+name,action)]
            elif phase == 19:
                calls = [("computer_release", {})]
            delta = {"tool_calls":[{"index":i,"id":f"call-{phase}-{i}","type":"function","function":{"name":name,"arguments":json.dumps(args)}} for i,(name,args) in enumerate(calls)]} if calls else {"content":"Fixture checked and released"}
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.end_headers()
            self.wfile.write(("data: " + json.dumps({"choices":[{"delta":delta,"finish_reason":None}]}) + "\n\ndata: " + json.dumps({"choices":[{"delta":{},"finish_reason":"tool_calls" if calls else "stop"}]}) + "\n\ndata: [DONE]\n\n").encode())
    server = ThreadingHTTPServer(("127.0.0.1",0),Provider)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    initialize_databases(tmp_path / "data")
    roles = RoleStore(tmp_path / "roles")
    models = ModelConfigurationStore(tmp_path / "models.json")
    config = models.create(ModelConfigurationInput(providerId="custom",provider="fixture",baseUrl=f"http://127.0.0.1:{server.server_port}/v1",model="fixture-chat",apiKey="fixture"))
    role = roles.create(RoleInput(name="fixture",profile={"profile":"fixture"},agentConfig={"computerUse":{"enabled":True}}))
    roles.set_model_configuration(role.id,config.id)
    sessions = SessionStore(tmp_path / "data/sessions.db")
    driver = Driver()
    registry = ModelAdapterRegistry(timeout=10)
    ports = {} if native else {"desktop":Desktop(),"driver_factory":lambda _:driver,"lease_factory":Lease}
    resource_root = Path(os.environ["MEIDO_COMPUTER_USE_NATIVE_RESOURCES"]) if native else tmp_path
    host = ComputerUseHost(role_getter=roles.get,configuration_getter=models.get,sessions=sessions,model_registry=registry,
        resources=resource_root,data_root=tmp_path/"driver",**ports)
    memory = MemorySettingsStore(tmp_path/"memory.json")
    memory.update(enabled=False,embedding_enabled=False)
    for key,value in {"store":roles,"model_configuration_store":models,"session_store":sessions,
        "session_manager":SessionManager(roles,sessions),"runtime_manager":RuntimeManager(sessions),"role_locks":{},
        "model_adapter":RoutedModelAdapter(registry),"model_adapter_registry":registry,"computer_use":host,"memory_settings_store":memory}.items():
        monkeypatch.setattr(main,key,value)
    try:
        client=TestClient(main.app)
        response=client.post(f"/api/roles/{role.id}/messages",json={"content":"Use only the fixture then release"})
        assert response.status_code==200 and "assistant_completed" in response.text,response.text
        assert "Fixture checked and released" in response.text,response.text
        assert '"isError": true' not in response.text,response.text
        assert len(requests)==20
        assert "data:image/png;base64," in json.dumps(requests[2])
        assert "data:image" not in json.dumps(requests[3])
        assert "data:image/png;base64," in json.dumps(requests[4])
        assert "data:image" not in json.dumps(requests[-1])
        history=client.get(f"/api/roles/{role.id}/session").json()
        assert "data:image" not in json.dumps(history)+response.text
        assert "iVBOR" not in json.dumps(history)+response.text
        if native:
            from backend.app.browser_use.process import WindowsJob
            from backend.app.computer_use.lease import JOB_NAME
            assert not WindowsJob.named_job_active(JOB_NAME)
            assert window.state()["a"]["clicks"]==1
            actual=window.state()["a"]
            assert actual["text"]=="你好 HTTP" and actual["keys"]>0
            assert "End," in actual["key_events"] and "A," in actual["key_events"],actual
            assert all(d+"," in actual["directions"] for d in ("up","down","left","right")),actual
        else:
            assert driver.exit_confirmed
        user=next(m for m in sessions.list_messages(f"role:{role.id}") if m.role=="user")
        run=sessions.get_run(user.runId)
        assert run.modelSnapshot["computerUseSource"]==run.modelSnapshot["humanInputSource"]=="desktop_owner"
        assert "iVBOR" not in json.dumps(sessions.list_tool_audits(run.runId))
    finally:
        server.shutdown();server.server_close();thread.join()
        if window is not None:
            window.close()


def test_role_computer_setting_survives_old_client_and_catalog_hard_denies(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    client = TestClient(main.app)
    profile = {"name": "fixture role", "profile": {"profile": "fixture"}}
    role = client.post("/api/roles", json=profile).json()["role"]
    assert role["agentConfig"]["computerUse"] == {"enabled": False}
    catalog = client.get(f"/api/roles/{role['id']}/tools").json()
    entries = {x["name"]: x for x in catalog["tools"]}
    assert all(name in entries and not entries[name]["authorized"] for name in COMPUTER_USE_TOOL_NAMES)
    enabled = client.put(f"/api/roles/{role['id']}", json={**profile, "agentConfig": {"computerUse": {"enabled": True}}})
    assert enabled.status_code == 200, enabled.text
    omitted = client.put(f"/api/roles/{role['id']}", json={**profile, "agentConfig": {"enabledTools": ["web_fetch"]}})
    assert omitted.json()["role"]["agentConfig"]["computerUse"] == {"enabled": True}
    off = client.put(f"/api/roles/{role['id']}", json={**profile, "agentConfig": {"computerUse": {"enabled": False}, "enabledTools": list(COMPUTER_USE_TOOL_NAMES)}})
    assert off.status_code == 200
    catalog = client.get(f"/api/roles/{role['id']}/tools").json()
    assert all(not x["authorized"] for x in catalog["tools"] if x["name"] in COMPUTER_USE_TOOL_NAMES)
