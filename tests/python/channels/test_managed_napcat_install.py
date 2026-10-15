from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile

import pytest

from backend.app.channels import napcat_managed_install as install


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
