"""Create a separate verified snapshot without changing Meido's source data."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid


def within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def absolute_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("All configured paths must be absolute")
    return path.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--settings", type=Path,
        default=Path(__file__).resolve().with_name("local-settings.json"),
    )
    args = parser.parse_args()
    snapshot: Path | None = None
    manifest: dict[str, object] = {
        "status": "incomplete", "fileCount": 0, "databaseCount": 0,
        "sources": [], "databases": [],
        "credentialStore": "System credentials are not included in this snapshot.",
    }

    def write_manifest() -> None:
        if snapshot is not None:
            (snapshot / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )

    def copy_file(source: Path, target: Path) -> None:
        if source.is_symlink():
            raise ValueError("Source symbolic links are not supported")
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as probe:
            sqlite_header = probe.read(16) == b"SQLite format 3\x00"
        if source.suffix.lower() == ".db" or sqlite_header:
            check = {"source": str(source), "destination": str(target.relative_to(snapshot)), "quickCheck": "incomplete"}
            manifest["databases"].append(check)
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as original:
                with closing(sqlite3.connect(target)) as copied:
                    original.backup(copied)
                    results = [row[0] for row in copied.execute("PRAGMA quick_check")]
                    if results != ["ok"]:
                        check["quickCheck"] = "failed"
                        raise RuntimeError("SQLite quick_check failed")
                    check["quickCheck"] = "ok"
            manifest["databaseCount"] += 1
        else:
            shutil.copy2(source, target)
        manifest["fileCount"] += 1

    def copy_tree(source: Path, target: Path, *, exclude_test_dirs: bool = False, top: bool = True) -> None:
        if source.is_symlink() or not source.is_dir():
            raise ValueError("Source directory is missing or is a symbolic link")
        target.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.iterdir()):
            if item.is_symlink():
                raise ValueError("Source symbolic links are not supported")
            if item.is_dir():
                if top and exclude_test_dirs and item.name.lower().startswith("pytest"):
                    continue
                copy_tree(item, target / item.name, top=False)
            elif item.is_file():
                if item.name.lower().endswith(("-wal", "-shm")):
                    continue
                copy_file(item, target / item.name)

    try:
        settings_path = absolute_path(args.settings)
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        data = absolute_path(settings["dataDir"])
        roles = absolute_path(settings["rolesDir"])
        owner = absolute_path(settings["ownerKnowledgeDir"])
        browser = absolute_path(settings["browserUseDataDir"])
        backup_root = absolute_path(settings["backupRoot"])
        legacy_values = settings.get("legacyModelConfigs", [])
        if not isinstance(legacy_values, list):
            raise ValueError("legacyModelConfigs must be a list of absolute paths")
        legacy = [absolute_path(value) for value in legacy_values]
        runtime = Path(__file__).resolve().parent
        runtime_files = [Path(__file__).resolve()]
        launcher = runtime / "Start-Meido.ps1"
        if launcher.is_file():
            runtime_files.append(launcher)
        source_directories = [data, roles, owner, browser]
        if any(within(backup_root, source) for source in source_directories):
            raise ValueError("Backup root must be outside every source directory")

        now = datetime.now(timezone(timedelta(hours=8)))
        snapshot = backup_root / (now.strftime("%Y-%m-%d_%H-%M-%S") + "_" + uuid.uuid4().hex[:8])
        snapshot.mkdir(parents=True, exist_ok=False)
        manifest.update({"createdAt": now.isoformat(), "settingsPath": str(settings_path)})
        sources = [
            {"kind": "data", "source": str(data), "destination": "data", "excludedTopLevelDirectories": "pytest*"},
            {"kind": "roles", "source": str(roles), "destination": "roles"},
            {"kind": "ownerKnowledge", "source": str(owner), "destination": "owner-knowledge"},
            {"kind": "browserUse", "source": str(browser), "destination": ("data/" + browser.relative_to(data).as_posix()) if within(browser, data) else "browser-use", "includedWithData": within(browser, data), "exists": browser.exists()},
        ]
        for index, source in enumerate(legacy, 1):
            sources.append({"kind": "legacyModelConfig", "source": str(source), "destination": f"legacy/{index:02d}/{source.name}"})
        sources.append({"kind": "runtime", "source": str(settings_path), "destination": "runtime/local-settings.json"})
        for source in runtime_files:
            sources.append({"kind": "runtime", "source": str(source), "destination": f"runtime/{source.name}"})
        manifest["sources"] = sources
        write_manifest()
        if not browser.is_dir() and (browser.exists() or not within(browser, data)):
            raise ValueError("Browser data source directory is missing")
        for source, destination, exclude in (
            (data, "data", True), (roles, "roles", False), (owner, "owner-knowledge", False),
        ):
            copy_tree(source, snapshot / destination, exclude_test_dirs=exclude)
        if not within(browser, data):
            copy_tree(browser, snapshot / "browser-use")
        for index, source in enumerate(legacy, 1):
            copy_file(source, snapshot / "legacy" / f"{index:02d}" / source.name)
        copy_file(settings_path, snapshot / "runtime/local-settings.json")
        for source in runtime_files:
            copy_file(source, snapshot / "runtime" / source.name)
        manifest["status"] = "complete"
        write_manifest()
        print(f"{snapshot}\nfiles={manifest['fileCount']} databases={manifest['databaseCount']} status=complete")
        return 0
    except Exception as error:
        manifest["status"] = "incomplete"
        manifest["errorType"] = type(error).__name__
        try:
            write_manifest()
        except Exception:
            pass
        print(f"{snapshot if snapshot is not None else '(snapshot not created)'}\nfiles={manifest['fileCount']} databases={manifest['databaseCount']} status=incomplete", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
