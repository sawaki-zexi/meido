from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


BACKUP_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/local-runtime/backup-meido-data.py"


@pytest.fixture
def local_settings(tmp_path):
    settings = {
        "dataDir": str(tmp_path / "data"),
        "rolesDir": str(tmp_path / "roles"),
        "ownerKnowledgeDir": str(tmp_path / "owner-knowledge"),
        "browserUseDataDir": str(tmp_path / "data/browser-use"),
        "backupRoot": str(tmp_path / "backups"),
        "legacyModelConfigs": [str(tmp_path / "old-workspace/model-config.json")],
    }
    for name in ("dataDir", "rolesDir", "ownerKnowledgeDir", "browserUseDataDir"):
        Path(settings[name]).mkdir(parents=True, exist_ok=True)
    Path(settings["dataDir"], "model-config.json").write_text('{"apiKey":"current-secret"}', encoding="utf-8")
    Path(settings["rolesDir"], "roles.json").write_text("[]", encoding="utf-8")
    Path(settings["ownerKnowledgeDir"], "notes.txt").write_text("owner data", encoding="utf-8")
    legacy = Path(settings["legacyModelConfigs"][0])
    legacy.parent.mkdir()
    legacy.write_text('{"apiKey":"old-secret"}', encoding="utf-8")
    config = tmp_path / "runtime/custom-settings.json"
    config.parent.mkdir()
    config.write_text(json.dumps(settings), encoding="utf-8")
    return settings, config


def run_backup(config, cwd, script=BACKUP_SCRIPT):
    return subprocess.run(
        [sys.executable, str(script), "--settings", str(config)],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


@pytest.mark.parametrize("database_name", ["sessions.db", "History", "cache.sqlite"])
def test_backup_includes_committed_wal_and_keeps_current_and_legacy_models_separate(local_settings, tmp_path, database_name):
    settings, config = local_settings
    database = Path(settings["dataDir"]) / database_name
    current = Path(settings["dataDir"]) / "model-config.json"
    old = Path(settings["legacyModelConfigs"][0])
    before = (current.read_bytes(), old.read_bytes())
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE messages (content TEXT)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO messages VALUES ('committed in WAL')")
        writer.commit()
        with closing(sqlite3.connect(database.as_uri() + "?immutable=1", uri=True)) as main_file:
            assert main_file.execute("SELECT content FROM messages").fetchall() == []
        result = run_backup(config, tmp_path)
        assert result.returncode == 0, result.stderr
        snapshots = list(Path(settings["backupRoot"]).iterdir())
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        with closing(sqlite3.connect(snapshot / "data" / database_name)) as copied:
            assert copied.execute("SELECT content FROM messages").fetchall() == [("committed in WAL",)]
    assert (current.read_bytes(), old.read_bytes()) == before
    assert (snapshot / "data/model-config.json").read_bytes() == before[0]
    legacy_copies = list((snapshot / "legacy").rglob("model-config.json"))
    assert len(legacy_copies) == 1
    assert legacy_copies[0].read_bytes() == before[1]
    assert (snapshot / "runtime/local-settings.json").read_bytes() == config.read_bytes()
    manifest_text = (snapshot / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "complete"
    assert manifest["databaseCount"] == 1
    assert manifest["databases"][0]["quickCheck"] == "ok"
    assert datetime.fromisoformat(manifest["createdAt"]).utcoffset() == timedelta(hours=8)
    for secret in ("current-secret", "old-secret"):
        assert secret not in result.stdout + result.stderr + manifest_text
    assert (snapshot / "roles/roles.json").exists()
    assert (snapshot / "owner-knowledge/notes.txt").read_text(encoding="utf-8") == "owner data"


@pytest.mark.parametrize("field", [
    "dataDir", "rolesDir", "ownerKnowledgeDir", "browserUseDataDir", "backupRoot", "legacyModelConfigs", "settings",
])
def test_relative_paths_are_rejected_before_creating_a_snapshot(local_settings, tmp_path, field):
    settings, config = local_settings
    backup_root = Path(settings["backupRoot"])
    if field == "settings":
        config = config.relative_to(tmp_path)
    else:
        settings[field] = ["relative/model-config.json"] if field == "legacyModelConfigs" else "relative"
        config.write_text(json.dumps(settings), encoding="utf-8")
    result = run_backup(config, tmp_path)
    assert result.returncode == 1
    assert not backup_root.exists()
    assert not (tmp_path / "relative").exists()


def test_backup_excludes_top_level_test_directories_and_sqlite_sidecars(local_settings, tmp_path):
    settings, config = local_settings
    data = Path(settings["dataDir"])
    tests = data / "pytest-old-run"
    tests.mkdir()
    (tests / "test-only.txt").write_text("test data", encoding="utf-8")
    nested = data / "cache/pytest-document"
    nested.mkdir(parents=True)
    (nested / "keep.txt").write_text("real nested data", encoding="utf-8")
    for name in ("sessions.db-wal", "sessions.db-shm", "History-wal", "History-shm"):
        (data / name).write_text("transient sidecar", encoding="utf-8")
    result = run_backup(config, tmp_path)
    assert result.returncode == 0, result.stderr
    snapshot = next(Path(settings["backupRoot"]).iterdir())
    assert not (snapshot / "data/pytest-old-run").exists()
    assert (snapshot / "data/cache/pytest-document/keep.txt").read_text(encoding="utf-8") == "real nested data"
    assert not list(snapshot.rglob("*-wal"))
    assert not list(snapshot.rglob("*-shm"))


def test_backup_excludes_active_rollback_journal_and_preserves_committed_data(local_settings, tmp_path):
    settings, config = local_settings
    database = Path(settings["dataDir"]) / "sessions.db"
    journal = database.with_name("sessions.db-journal")
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("CREATE TABLE messages (content TEXT)")
        writer.execute("INSERT INTO messages VALUES ('committed message')")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE messages SET content = 'uncommitted change'")
        assert journal.is_file()
        original_journal = journal.read_bytes()
        result = run_backup(config, tmp_path)
        assert result.returncode == 0, result.stderr
        assert journal.read_bytes() == original_journal
        assert writer.execute("SELECT content FROM messages").fetchall() == [("uncommitted change",)]
        snapshot = next(Path(settings["backupRoot"]).iterdir())
        assert not list(snapshot.rglob("*-journal"))
        with closing(sqlite3.connect(snapshot / "data/sessions.db")) as copied:
            assert copied.execute("SELECT content FROM messages").fetchall() == [("committed message",)]


@pytest.mark.parametrize("field", ["dataDir", "rolesDir", "ownerKnowledgeDir", "browserUseDataDir"])
def test_missing_source_keeps_an_incomplete_snapshot(local_settings, tmp_path, field):
    settings, config = local_settings
    missing = tmp_path / "missing"
    settings[field] = str(missing)
    config.write_text(json.dumps(settings), encoding="utf-8")
    result = run_backup(config, tmp_path)
    assert result.returncode == 1
    snapshots = list(Path(settings["backupRoot"]).iterdir())
    assert len(snapshots) == 1
    manifest = json.loads((snapshots[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
    assert manifest["errorType"]
    assert not missing.exists()


def test_unused_internal_browser_directory_is_optional_and_recorded(local_settings, tmp_path):
    settings, config = local_settings
    browser = Path(settings["dataDir"]) / "unused-browser"
    settings["browserUseDataDir"] = str(browser)
    config.write_text(json.dumps(settings), encoding="utf-8")
    result = run_backup(config, tmp_path)
    assert result.returncode == 0, result.stderr
    snapshot = next(Path(settings["backupRoot"]).iterdir())
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    browser_source = next(source for source in manifest["sources"] if source["kind"] == "browserUse")
    assert browser_source["exists"] is False
    assert manifest["status"] == "complete"
    assert not browser.exists()


@pytest.mark.parametrize("source", ["dataDir", "rolesDir", "ownerKnowledgeDir", "browserUseDataDir"])
def test_backup_destination_inside_a_source_is_rejected_without_creating_files(local_settings, tmp_path, source):
    settings, config = local_settings
    backup_root = Path(settings[source]) / "must-not-be-created"
    settings["backupRoot"] = str(backup_root)
    config.write_text(json.dumps(settings), encoding="utf-8")
    result = run_backup(config, tmp_path)
    assert result.returncode == 1
    assert not backup_root.exists()


@pytest.mark.parametrize("source", ["settings", "runtime", "legacy"])
def test_backup_can_be_next_to_files_that_are_not_copied_recursively(local_settings, tmp_path, source):
    settings, config = local_settings
    script = BACKUP_SCRIPT
    if source == "runtime":
        runtime = tmp_path / "isolated-tools"
        runtime.mkdir()
        script = runtime / "backup-meido-data.py"
        shutil.copy2(BACKUP_SCRIPT, script)
        source_dir = runtime
    elif source == "settings":
        source_dir = config.parent
    elif source == "legacy":
        source_dir = Path(settings["legacyModelConfigs"][0]).parent
    backup_root = source_dir / "backups"
    settings["backupRoot"] = str(backup_root)
    config.write_text(json.dumps(settings), encoding="utf-8")
    result = run_backup(config, tmp_path, script)
    assert result.returncode == 0, result.stderr
    snapshot = next(backup_root.iterdir())
    assert json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))["status"] == "complete"


def test_repeated_backups_keep_separate_snapshots_and_include_external_browser_data(local_settings, tmp_path):
    settings, config = local_settings
    browser = tmp_path / "separate-browser/profiles"
    browser.mkdir(parents=True)
    (browser / "session.txt").write_text("browser state", encoding="utf-8")
    settings["browserUseDataDir"] = str(browser.parent)
    config.write_text(json.dumps(settings), encoding="utf-8")
    for _ in range(2):
        result = run_backup(config, tmp_path)
        assert result.returncode == 0, result.stderr
    snapshots = list(Path(settings["backupRoot"]).iterdir())
    assert len(snapshots) == 2
    for snapshot in snapshots:
        assert (snapshot / "browser-use/profiles/session.txt").read_text(encoding="utf-8") == "browser state"
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        browser_source = next(source for source in manifest["sources"] if source["kind"] == "browserUse")
        assert browser_source["includedWithData"] is False
        assert browser_source["exists"] is True
