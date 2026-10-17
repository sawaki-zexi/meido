"""Live SQLite journals may disappear without cancelling an owner verification."""
import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from backend.app.proactive_runtime.source_validation import SourceContractRegistry
from test_proactive_authoring_api import (  # noqa: F401
    api, authored, refresh_artifact, shell_api, verify_source,
)


pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_real_journal_commit_during_collection_check_keeps_contract_failure_and_releases_runtime(authored, monkeypatch):
    value = authored
    script = value.root / 'source.py'
    text = script.read_text(encoding='utf-8')
    broken = text.replace('raise ValueError("synthetic read failure")', 'return []')
    assert broken != text
    script.write_text(broken, encoding='utf-8', newline='\n')
    refresh_artifact(value, monkeypatch)
    ready, release = threading.Event(), threading.Event()
    observed, worker_errors = {}, []
    workers = []
    original_check, original_scan = SourceContractRegistry._check_staging, os.scandir

    def commit_journal(database):
        try:
            with closing(sqlite3.connect(database)) as db:
                assert db.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
                db.execute('CREATE TABLE data(value INTEGER)')
                db.commit()
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO data VALUES(1)')
                assert Path(str(database) + '-journal').is_file()
                ready.set()
                assert release.wait(5), 'owned SQLite commit was not released'
                db.commit()
        except BaseException as error:
            worker_errors.append(error)
            ready.set()

    class JournalEntry:
        def __init__(self, entry):
            self.entry, self.name, self.path = entry, entry.name, entry.path

        def is_symlink(self):
            return self.entry.is_symlink()

        def is_dir(self, **options):
            return self.entry.is_dir(**options)

        def is_file(self, **options):
            result = self.entry.is_file(**options)
            assert result
            observed['was_file'] = True
            release.set()
            workers[0].join(5)
            assert not workers[0].is_alive() and not worker_errors
            assert not Path(self.path).exists()
            observed['committed'] = True
            return result

        def stat(self, **options):
            # Keep native DirEntry behavior: POSIX stats the disappeared file;
            # Windows may return its already cached directory information.
            return self.entry.stat(**options)

    class JournalScan:
        def __init__(self, scan):
            self.scan = scan

        def __enter__(self):
            self.scan.__enter__()
            return self

        def __exit__(self, *arguments):
            return self.scan.__exit__(*arguments)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.scan)
            return JournalEntry(entry) if Path(entry.path) == observed['journal'] else entry

    def scan(directory):
        result = original_scan(directory)
        if (not observed.get('committed') and isinstance(directory, (str, os.PathLike))
                and Path(directory) == observed.get('state')):
            return JournalScan(result)
        return result

    def check(registry):
        task = asyncio.current_task()
        if (not workers and registry._directory is not None and task is not None
                and task.get_coro().__qualname__.endswith('.watch')):
            state = registry._directory / 'state'
            observed.update(state=state, journal=state / 'live.sqlite-journal')
            worker = threading.Thread(target=commit_journal, args=(state / 'live.sqlite',))
            workers.append(worker)
            worker.start()
            assert ready.wait(5) and not worker_errors
        return original_check(registry)

    monkeypatch.setattr(os, 'scandir', scan)
    monkeypatch.setattr(SourceContractRegistry, '_check_staging', check)
    try:
        final = verify_source(value, 'content')
        assert observed.get('was_file') and observed.get('committed')
        assert final['status'] == 'failed' and final['proof'] is None, final
        assert final['runtimeAcknowledged'] and not final['cleanupPending']
        assert not (value.root / 'state').exists()
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert not worker_errors
