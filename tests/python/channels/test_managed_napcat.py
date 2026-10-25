from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import stat
from pathlib import Path
import zipfile

import httpx
import pytest

from backend.app.channels import napcat_managed_install as install
from backend.app.channels.napcat_managed import ManagedNapCat


@pytest.fixture(autouse=True)
def supported_platform(monkeypatch):
    # OS compatibility is an external boundary; no native QQ is launched.
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    monkeypatch.setattr(install.platform, "machine", lambda: "AMD64")


def test_login_reference_survives_restart_and_is_bound_to_its_role(tmp_path):
    async def scenario():
        first = ManagedNapCat(tmp_path)
        ref = first.begin("role-a")
        endpoint = first.endpoint(ref, role_id="role-a")
        other = first.begin("role-b")
        assert first.endpoint(other, role_id="role-b") != endpoint
        restored = ManagedNapCat(tmp_path)
        restored_endpoint = restored.endpoint(ref, role_id="role-a")
        assert restored_endpoint[0] == endpoint[0]
        assert restored_endpoint[1] != endpoint[1]
        assert all(endpoint[1] not in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
        status = await restored.status(ref, role_id="role-a")
        assert status["login"]["phase"] == "stopped"
        assert endpoint[1] not in json.dumps(status)
        assert str(tmp_path) not in json.dumps(status)
        with pytest.raises(PermissionError):
            restored.endpoint(ref, role_id="role-b")
        with pytest.raises(ValueError):
            restored.endpoint("../foreign", role_id="role-a")
        await restored.delete(ref, role_id="role-a")
        with pytest.raises(KeyError):
            first.endpoint(ref, role_id="role-a")
        assert first.endpoint(other, role_id="role-b")
        await first.delete(other, role_id="role-b")
        await first.close()
        await restored.close()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="QQ creates Windows read-only profile folders")
def test_cancel_removes_readonly_qq_profile_and_allows_another_login(tmp_path):
    async def scenario():
        manager = ManagedNapCat(tmp_path)
        ref = manager.begin("role-a")
        documents = tmp_path / "managed-napcat/accounts" / ref / "profile/Documents"
        documents.mkdir(parents=True)
        documents.chmod(stat.S_IREAD)
        try:
            await manager.delete(ref, role_id="role-a")
            assert manager.refs_for_role("role-a") == ()
            replacement = manager.begin("role-a")
            await manager.delete(replacement, role_id="role-a")
        finally:
            if documents.exists():
                documents.chmod(stat.S_IWRITE)
            await manager.close()
    asyncio.run(scenario())


def test_deleted_role_cleanup_preserves_surviving_role_profiles(tmp_path):
    async def scenario():
        manager = ManagedNapCat(tmp_path)
        retained = manager.begin("surviving")
        deleted = manager.begin("deleted")
        assert manager.refs_for_role("deleted") == (deleted,)
        await manager.cleanup_deleted_roles({"surviving"})
        assert manager.endpoint(retained, role_id="surviving")
        with pytest.raises(KeyError):
            manager.endpoint(deleted, role_id="deleted")
        await manager.delete_role("surviving")
        assert manager.refs_for_role("surviving") == ()
        await manager.close()

    asyncio.run(scenario())


def test_managed_login_presents_official_qr_and_rejects_account_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("NAPCAT_QUICK_PASSWORD", "must-not-be-inherited")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        for name in ("node.exe", "index.js", "wrapper.node", "napcat/napcat.mjs"):
            bundle.writestr(name, "official fixture")
    package = archive.getvalue()
    monkeypatch.setattr(install, "ARCHIVE_SHA256", hashlib.sha256(package).hexdigest())
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(package))
    monkeypatch.setattr(install, "resolve_official_qq", lambda *a: tmp_path / "official-qq")
    state = {"online": False, "uin": "10001", "url": "https://qq.test/scan?secret=hidden"}
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nm1kAAAAASUVORK5CYII=")
    spawned = []

    class Process:
        returncode = None
        def kill(self):
            self.returncode = 0
        async def wait(self):
            self.returncode = 0
            return 0

    class Owner:
        def __init__(self, process):
            self.process = process
        def close(self):
            self.process.kill()

    async def spawn(*command, **kwargs):
        process = Process()
        directory = Path(kwargs["env"]["NAPCAT_WORKDIR"]) / "cache"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "qrcode.png").write_bytes(image)
        spawned.append((process, kwargs["env"]))
        return process, Owner(process)

    def respond(request):
        route = request.url.path
        if route.endswith("/auth/login"):
            return httpx.Response(200, json={"code": 0, "data": {"Credential": "fixture-secret"}})
        assert request.headers["Authorization"] == "Bearer fixture-secret"
        if route.endswith("/CheckLoginStatus"):
            data = {"isLogin": state["online"], "qrcodeurl": state["url"]}
        elif route.endswith("/GetQQLoginInfo"):
            data = {"uin": state["uin"], "nick": "QQ name", "online": True}
        elif route.endswith("/RefreshQRcode"):
            state["url"] = "https://qq.test/scan?secret=new"
            # A complete new PNG is exposed by the external runtime on refresh.
            from PIL import Image
            directory = Path(spawned[-1][1]["NAPCAT_WORKDIR"]) / "cache"
            Image.new("RGB", (2, 2), "red").save(directory / "qrcode.png")
            data = {"qrcodeurl": state["url"]}
        else:
            raise AssertionError(route)
        return httpx.Response(200, json={"code": 0, "data": data})

    async def wait_phase(manager, ref, phase):
        for _ in range(200):
            status = await manager.status(ref, role_id="role-a")
            if status["login"]["phase"] == phase:
                return status
            await asyncio.sleep(.01)
        raise AssertionError(status)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            manager = ManagedNapCat(tmp_path, process_factory=spawn, http_client=client, poll_interval=.01)
            ref = manager.begin("role-a")
            await manager.connect(ref, role_id="role-a")
            status = await wait_phase(manager, ref, "login_required")
            assert base64.b64decode(status["login"]["qrcode"].split(",", 1)[1]) == image
            assert "hidden" not in json.dumps(status)
            assert "NAPCAT_QUICK_PASSWORD" not in spawned[0][1]
            refreshed = await manager.refresh_qrcode(ref, role_id="role-a")
            assert refreshed["login"]["qrcode"] != status["login"]["qrcode"]
            state["online"] = True
            status = await wait_phase(manager, ref, "online")
            assert status["login"]["uin"] == "10001"
            manager.remember_identity(ref, role_id="role-a", uin="10001")
            with pytest.raises(ValueError):
                manager.remember_identity(ref, role_id="role-a", uin="20002")
            await manager.stop(ref, role_id="role-a")
            assert spawned[0][0].returncode == 0
            state["uin"] = "20002"
            restored = ManagedNapCat(tmp_path, process_factory=spawn, http_client=client, poll_interval=.01)
            await restored.connect(ref, role_id="role-a")
            status = await wait_phase(restored, ref, "error")
            assert "不匹配" in status["login"]["error"]
            assert status["login"]["qrcode"] == ""
            await manager.close()
            await restored.close()
            assert all(process.returncode == 0 for process, _ in spawned)

    asyncio.run(scenario())
