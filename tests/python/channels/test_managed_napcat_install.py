from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import threading
from types import SimpleNamespace
import zipfile

import pytest

from backend.app.channels import napcat_managed_install as install
from backend.app.channels import napcat_managed_qq as qq


@pytest.fixture(autouse=True)
def supported_platform(monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    monkeypatch.setattr(install.platform, "machine", lambda: "AMD64")


def test_download_does_not_publish_archive_with_escaping_member(tmp_path, monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escaped.txt", "outside")
    data = archive.getvalue()
    monkeypatch.setattr(install, "ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(data))
    installer = install.NapCatInstaller(tmp_path)
    with pytest.raises(ValueError, match="非法路径"):
        asyncio.run(installer.prepare())
    assert installer.preparation()["stage"] == "error"
    assert not (tmp_path / "escaped.txt").exists()
    assert not installer.install_dir.exists()


def test_download_must_match_pinned_digest_and_supports_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"untrusted"))
    installer = install.NapCatInstaller(tmp_path)
    with pytest.raises(ValueError, match="SHA-256"):
        asyncio.run(installer.prepare())
    assert not installer.install_dir.exists()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        for name in ("node.exe", "index.js", "wrapper.node", "napcat/napcat.mjs"):
            bundle.writestr(name, "fixture")
    data = archive.getvalue()
    monkeypatch.setattr(install, "ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(data))
    monkeypatch.setattr(install, "resolve_official_qq", lambda *a: tmp_path / "official-qq")
    assert asyncio.run(installer.prepare()) == tmp_path / "official-qq"
    assert installer.preparation()["stage"] == "ready"
    assert (installer.install_dir / "node.exe").read_text() == "fixture"


def test_prepare_caches_verified_dependencies_from_updated_desktop_qq(tmp_path, monkeypatch):
    """The installed desktop build can differ from the bundled QQ wrapper."""
    desktop = tmp_path / "desktop-qq"
    app = desktop / "versions/9.9.36-53644/resources/app"
    app.mkdir(parents=True)
    (desktop / "QQ.exe").touch()
    (desktop / "Uninstall.exe").touch()
    (desktop / "versions/config.json").write_text(json.dumps({"curVersion": "9.9.36-53644"}))
    dependencies = {"crypto.dll": b"compatible crypto", "ssl.dll": b"compatible ssl"}
    for name, data in dependencies.items():
        (app / name).write_bytes(data)
    # The desktop wrapper is deliberately different: only the pinned bundle's
    # wrapper is launched, and only its missing dependencies should be read.
    (app / "wrapper.node").write_bytes(b"new desktop wrapper")
    class RegistryKey:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2,
        OpenKey=lambda *a: RegistryKey(),
        QueryValueEx=lambda *a: (str(desktop / "Uninstall.exe"), 1),
    ))
    wrapper = b"pinned wrapper"
    monkeypatch.setattr(qq, "_EXPECTED_SHA256", {
        **{name: hashlib.sha256(data).hexdigest() for name, data in dependencies.items()},
        "wrapper.node": hashlib.sha256(wrapper).hexdigest(),
    })
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        for name in ("node.exe", "index.js", "napcat/napcat.mjs"):
            bundle.writestr(name, "fixture")
        bundle.writestr("wrapper.node", wrapper)
    package = archive.getvalue()
    monkeypatch.setattr(install, "ARCHIVE_SHA256", hashlib.sha256(package).hexdigest())
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(package))
    installer = install.NapCatInstaller(tmp_path / "meido")
    runtime = asyncio.run(installer.prepare())
    assert runtime.is_relative_to(installer.install_dir)
    assert {name: (runtime / name).read_bytes() for name in dependencies} == dependencies
    # A desktop update/removal must not break a previously prepared runtime.
    for name in dependencies:
        (app / name).unlink()
    assert asyncio.run(install.NapCatInstaller(tmp_path / "meido").prepare()) == runtime
    (runtime / "ssl.dll").write_bytes(b"modified dependency")
    restarted = install.NapCatInstaller(tmp_path / "meido")
    with pytest.raises(RuntimeError, match="运行组件"):
        asyncio.run(restarted.prepare())
    assert restarted.preparation()["stage"] == "error"


def test_cancelling_preparation_retains_lease_until_dependency_worker_stops(tmp_path, monkeypatch):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as bundle:
        for name in ("node.exe", "index.js", "wrapper.node", "napcat/napcat.mjs"):
            bundle.writestr(name, "fixture")
    data = package.getvalue()
    monkeypatch.setattr(install, "ARCHIVE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(data))
    entered, release = threading.Event(), threading.Event()

    def resolve(*args):
        entered.set()
        assert release.wait(5)
        return tmp_path / "runtime"

    monkeypatch.setattr(install, "resolve_official_qq", resolve)

    async def scenario():
        first = install.NapCatInstaller(tmp_path)
        task = asyncio.create_task(first.prepare())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done(), "repeated cancellation must not abandon a writing worker"
            second = install.NapCatInstaller(tmp_path)
            with pytest.raises(RuntimeError, match="另一运行实例"):
                await second.prepare()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert await install.NapCatInstaller(tmp_path).prepare() == tmp_path / "runtime"

    asyncio.run(scenario())
