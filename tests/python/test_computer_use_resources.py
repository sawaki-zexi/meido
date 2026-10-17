import hashlib
import json
from pathlib import Path

import pytest

from backend.app.computer_use import driver


def test_replacing_manifest_and_binary_together_cannot_bypass_the_fixed_resource_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(driver.sys, "platform", "win32")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    root=tmp_path/"native/computer-use"
    root.mkdir(parents=True)
    executable=root/"cua-driver.exe"
    executable.write_bytes(b"unapproved driver")
    manifest=json.loads(Path(driver.__file__).with_name("runtime.json").read_text(encoding="utf-8"))
    manifest["sha256"]=hashlib.sha256(executable.read_bytes()).hexdigest()
    (root/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    with pytest.raises(OSError,match="identity mismatch"):
        driver.resolve_driver(tmp_path)


def test_committed_native_patch_matches_runtime_lock():
    root=Path(__file__).resolve().parents[2]
    expected=json.loads(Path(driver.__file__).with_name("runtime.json").read_text(encoding="utf-8"))
    patch=root/"native/computer-use/patches/0001-meido-windows-profile.patch"
    assert hashlib.sha256(patch.read_bytes()).hexdigest()==expected["patchSha256"]
