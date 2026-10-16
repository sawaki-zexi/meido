"""Exercise the local launcher through its public PowerShell CLI only."""
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


START_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/local-runtime/Start-Meido.ps1"


@pytest.fixture
def powershell():
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("PowerShell is not installed")
    return executable


@pytest.fixture
def local_settings(tmp_path):
    code = tmp_path / "code A"
    (code / "backend/app").mkdir(parents=True)
    (code / "backend/app/main.py").write_text("", encoding="utf-8")
    (code / "node_modules/vite/bin").mkdir(parents=True)
    (code / "node_modules/vite/bin/vite.js").write_text("", encoding="utf-8")
    persistent = tmp_path / "persistent data"
    values = {
        "codeRoot": str(code),
        "projectRoot": str(persistent),
        "dataDir": str(persistent / "data"),
        "rolesDir": str(persistent / "roles"),
        "ownerKnowledgeDir": str(persistent / "owner-knowledge"),
        "browserUseDataDir": str(persistent / "browser-use"),
        "python": sys.executable,
        "node": sys.executable,
        "apiHost": "127.0.0.1",
        "apiPort": 18341,
        "webPort": 18342,
        "backupRoot": str(tmp_path / "backups"),
        "legacyModelConfigs": [],
    }
    for key in ("dataDir", "rolesDir", "ownerKnowledgeDir"):
        Path(values[key]).mkdir(parents=True)
    path = tmp_path / "local settings.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path, values


def run_start(powershell, settings, *arguments):
    return subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(START_SCRIPT), "-SettingsPath", str(settings), *arguments],
        cwd=settings.parent, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def tree(root):
    return sorted(str(item.relative_to(root)) for item in root.rglob("*"))


@pytest.mark.parametrize("service", ["Backend", "Frontend", "Backup"])
def test_check_only_reports_explicit_paths_without_creating_runtime_data(powershell, local_settings, service):
    path, values = local_settings
    before = tree(path.parent)
    result = run_start(powershell, path, "-Service", service, "-CheckOnly")
    assert result.returncode == 0, result.stdout + result.stderr
    info = json.loads(result.stdout)
    assert info["Service"] == service
    for field in ("CodeRoot", "DataDir", "RolesDir", "OwnerKnowledgeDir"):
        assert Path(info[field]) == Path(values[field[0].lower() + field[1:]])
    assert info["BackendUrl"] == "http://127.0.0.1:18341"
    assert info["FrontendUrl"] == "http://127.0.0.1:18342/app"
    assert tree(path.parent) == before


def test_default_backend_and_code_override_keep_the_same_persistent_directories(powershell, local_settings):
    path, values = local_settings
    other_code = path.parent / "code B"
    (other_code / "backend/app").mkdir(parents=True)
    (other_code / "backend/app/main.py").write_text("", encoding="utf-8")
    before = tree(path.parent)
    original = run_start(powershell, path, "-CheckOnly")
    overridden = run_start(powershell, path, "-CodeRoot", str(other_code), "-CheckOnly")
    assert original.returncode == overridden.returncode == 0, original.stderr + overridden.stderr
    original_info, overridden_info = json.loads(original.stdout), json.loads(overridden.stdout)
    assert original_info["Service"] == overridden_info["Service"] == "Backend"
    assert Path(original_info["CodeRoot"]) == Path(values["codeRoot"])
    assert Path(overridden_info["CodeRoot"]) == other_code
    for field in ("DataDir", "RolesDir", "OwnerKnowledgeDir"):
        assert original_info[field] == overridden_info[field]
    assert tree(path.parent) == before


@pytest.mark.parametrize("invalid", ["relative-data", "missing-data", "missing-code", "incomplete-code"])
def test_invalid_paths_fail_without_creating_empty_runtime_data(powershell, local_settings, invalid):
    path, values = local_settings
    if invalid == "relative-data":
        values["dataDir"] = "relative-data"
    elif invalid == "missing-data":
        values["dataDir"] = str(path.parent / "missing data")
    else:
        bad_code = path.parent / "invalid source"
        if invalid == "incomplete-code":
            bad_code.mkdir()
        values["codeRoot"] = str(bad_code)
    path.write_text(json.dumps(values), encoding="utf-8")
    before = tree(path.parent)
    result = run_start(powershell, path, "-CheckOnly")
    assert result.returncode != 0, result.stdout
    assert tree(path.parent) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows drive and root-relative path semantics")
@pytest.mark.parametrize("kind", ["root-relative", "drive-relative"])
def test_existing_but_not_fully_absolute_data_directory_is_rejected(powershell, local_settings, kind):
    path, values = local_settings
    drive = path.drive
    if kind == "root-relative":
        values["dataDir"] = values["dataDir"][len(drive):]
    else:
        (path.parent / "folder").mkdir()
        values["dataDir"] = f"{drive}folder"
    path.write_text(json.dumps(values), encoding="utf-8")
    # Both misleading forms resolve to real directories from this working
    # directory; a missing-directory check alone must not satisfy this test.
    env = dict(os.environ, MEIDO_TEST_PATH=values["dataDir"])
    probe = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command",
         "if (Test-Path -LiteralPath $env:MEIDO_TEST_PATH -PathType Container) { exit 0 } else { exit 1 }"],
        cwd=path.parent, env=env, capture_output=True, text=True, timeout=15,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    before = tree(path.parent)
    result = run_start(powershell, path, "-CheckOnly")
    assert result.returncode != 0, result.stdout
    assert "absolute" in (result.stdout + result.stderr).lower()
    assert tree(path.parent) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows listener inspection uses Get-NetTCPConnection")
def test_existing_healthy_backend_is_reused_without_starting_or_creating_data(powershell, local_settings):
    path, values = local_settings
    requests = []

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        values["apiPort"] = server.server_port
        path.write_text(json.dumps(values), encoding="utf-8")
        before = tree(path.parent)
        result = run_start(powershell, path, "-Service", "Backend")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "/api/health" in requests
        assert tree(path.parent) == before
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
