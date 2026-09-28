from fastapi.testclient import TestClient

from backend.app import main
from backend.app.role_store import RoleStore


def test_role_api_crud_and_duplicate_names(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    client = TestClient(main.app)
    payload = {"name": "测试角色", "profile": {"profile": "核心设定"}}

    first_response = client.post("/api/roles", json=payload)
    second_response = client.post("/api/roles", json=payload)
    assert first_response.status_code == 201
    assert second_response.status_code == 201
    first = first_response.json()["role"]
    second = second_response.json()["role"]
    assert first["id"] != second["id"]

    updated_response = client.put(
        f"/api/roles/{first['id']}",
        json={"name": "改名", "profile": {"profile": "更新设定"}},
    )
    assert updated_response.status_code == 200
    assert updated_response.json()["role"]["id"] == first["id"]
    assert client.get(f"/api/roles/{first['id']}").json()["role"]["name"] == "改名"
    assert len(client.get("/api/roles").json()["roles"]) == 2


def test_role_api_rejects_blank_required_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "store", RoleStore(tmp_path / "roles"))
    client = TestClient(main.app)
    assert client.post("/api/roles", json={"name": " ", "profile": {"profile": "设定"}}).status_code == 422
    assert client.post("/api/roles", json={"name": "角色", "profile": {"profile": " "}}).status_code == 422
