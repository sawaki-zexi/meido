import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .models import RoleInput, RoleList, RoleResponse
from .role_store import RoleStore
from .storage import initialize_databases

roles_root = Path(os.getenv("MEIDO_ROLES_DIR", "roles"))
data_root = Path(os.getenv("MEIDO_DATA_DIR", ".data"))
initialize_databases(data_root)
store = RoleStore(roles_root)

app = FastAPI(title="Meido API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/roles", response_model=RoleList)
def list_roles() -> RoleList:
    return RoleList(roles=store.list())


@app.post("/api/roles", response_model=RoleResponse, status_code=201)
def create_role(data: RoleInput) -> RoleResponse:
    return RoleResponse(role=store.create(data))


@app.get("/api/roles/{role_id}", response_model=RoleResponse)
def get_role(role_id: str) -> RoleResponse:
    role = store.get(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return RoleResponse(role=role)


@app.put("/api/roles/{role_id}", response_model=RoleResponse)
def update_role(role_id: str, data: RoleInput) -> RoleResponse:
    try:
        return RoleResponse(role=store.update(role_id, data))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="角色不存在") from error
