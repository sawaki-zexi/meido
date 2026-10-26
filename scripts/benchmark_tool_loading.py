"""Offline #234 fixture: actual SQLite writes, no model/MCP/business execution.

Run: python scripts/benchmark_tool_loading.py --output /tmp/tool-loading.json
Full new-path matrix; old path is intentionally bounded to 50 tools / 1 KiB.
"""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.agent_runtime import CapabilityRegistry, CancellationToken, ToolContext, ToolDefinition, AgentToolResult
from backend.app.agent_runtime.types import ToolResultMessage
from backend.app.models import AgentRun
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


class SyntheticTool:
    def __init__(self, index, kib):
        self.definition = ToolDefinition(f'bench_{index:04d}', 'Synthetic unused catalog entry',
            {'type': 'object', 'description': 'x' * (1024 * kib)}, exposure='deferred')
    async def execute(self, *args):
        raise AssertionError('benchmark must never execute a business tool')


def measured(fn, count=5):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {'p50_ms': round(statistics.median(samples), 3), 'p95_ms': round(max(samples), 3), 'samples': count}


def written_bytes():
    path = Path('/proc/self/io')
    if not path.exists():
        return None
    return int(next(line.split(':')[1] for line in path.read_text().splitlines() if line.startswith('write_bytes:')))


async def scenario(n, kib, loads, calls, *, legacy=False):
    tools = [SyntheticTool(i, kib) for i in range(n)]
    registry = CapabilityRegistry(tools)
    def resolve(**extra):
        return registry.resolve(role_id='benchmark', session_key='role:benchmark', run_id='run', discovery_enabled=True, **extra)
    resolution_ms = measured(lambda: resolve(activated_tools={'bench_0000'}), count=3)
    cap = resolve(activated_tools={'bench_0000'})
    with tempfile.TemporaryDirectory(prefix='meido-loading-') as folder:
        root = Path(folder)
        initialize_databases(root)
        keeper = sqlite3.connect(root / 'sessions.db')
        keeper.execute('PRAGMA journal_mode=WAL')
        keeper.execute('PRAGMA wal_autocheckpoint=0')
        sessions = SessionStore(root / 'sessions.db')
        baseline = sessions
        if legacy:
            source = subprocess.check_output(['git', 'show', '8a2d27d:backend/app/session_store.py'], text=True)
            namespace = {'__name__': 'backend.app._benchmark_baseline', '__package__': 'backend.app'}
            exec(compile(source, '<8a2d27d SessionStore>', 'exec'), namespace)
            baseline = namespace['SessionStore'](root / 'sessions.db')
        run = AgentRun(runId='run', roleId='benchmark', sessionKey='role:benchmark', status='created',
            startedAt=datetime.now(timezone.utc), modelSnapshot={'capabilities': cap.snapshot.to_dict(include_prompt_sections=False) if legacy else cap.run_metadata()})
        before = written_bytes()
        baseline.create_run_with_messages(run, 'synthetic discovery benchmark')
        baseline.update_run('run', status='running')
        if legacy:
            baseline.append_capability_revision('run', cap.snapshot.to_dict(include_prompt_sections=False))
        for index in range(loads):
            view = cap.current()
            context = ToolContext('benchmark', 'role:benchmark', 'run', CancellationToken(), f'load-{index}', view.snapshot.snapshot_id, view)
            result = await view.get('tool_search').execute({'query': f'select:bench_{index + 1:04d}'}, context)
            baseline.append_agent_message('role:benchmark', ToolResultMessage(context.tool_call_id, 'tool_search', result.content), run_id='run')
            revision = cap.pending_revision(context.tool_call_id)
            if revision is not None:
                if legacy:
                    baseline.append_capability_revision('run', revision.snapshot.to_dict(include_prompt_sections=False))
                cap.publish_revision(revision)
        view = cap.current()
        preparation = measured(view.provider_schemas)
        cache = view.selection_cache()
        restore = measured(lambda: resolve(restored_activation=cache), count=3)
        proof = view.call_record('bench_0000')
        call_samples = []
        for index in range(calls):
            start = time.perf_counter()
            if legacy:
                baseline.record_capability_call('run', f'call-{index}', view.snapshot.snapshot_id)
            sessions.start_tool_audit(run_id='run', role_id='benchmark', tool_name='bench_0000', call_id=f'call-{index}',
                argument_summary={}, capability_binding=None if legacy else proof)
            sessions.finish_tool_audit(run_id='run', call_id=f'call-{index}', result_category='succeeded')
            call_samples.append((time.perf_counter() - start) * 1000)
        if not legacy:
            sessions.save_tool_selection('run', cache)
        sessions.update_run('run', status='completed')
        stored = keeper.execute("SELECT length(CAST(model_snapshot_json AS BLOB)) FROM agent_runs").fetchone()[0]
        audit = keeper.execute("SELECT SUM(COALESCE(length(CAST(capability_binding_json AS BLOB)),0)+length(argument_summary_json)) FROM tool_audits").fetchone()[0]
        wal = root / 'sessions.db-wal'
        after = written_bytes()
        result = {'path': 'legacy-8a2d27d-store' if legacy else 'lightweight', 'tools': n, 'schema_kib': kib,
            'requested_loads': loads, 'selected': len(cache['selected']), 'calls': calls,
            'run_metadata_bytes': stored, 'audit_payload_bytes': audit, 'one_call_binding_bytes': len(json.dumps(proof, ensure_ascii=False).encode()) if not legacy else None,
            'sqlite_page_bytes': keeper.execute('PRAGMA page_count').fetchone()[0] * keeper.execute('PRAGMA page_size').fetchone()[0],
            'wal_file_bytes': wal.stat().st_size if wal.exists() else 0,
            'process_write_bytes_delta': after - before if after is not None and before is not None else None,
            'resolve': resolution_ms, 'request_prepare': preparation, 'restore': restore,
            'audit_write': {'p50_ms': round(statistics.median(call_samples), 3), 'p95_ms': round(sorted(call_samples)[max(0, int(.95 * len(call_samples)) - 1)], 3), 'samples': calls}}
        keeper.close()
        return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    rows = []
    for n in (50, 500, 2000):
        for kib in (1, 10):
            for loads in (0, 1, 10, 30):
                for calls in (20, 100):
                    rows.append(await scenario(n, kib, loads, calls))
    for loads in (0, 1, 10):
        rows.append(await scenario(50, 1, loads, 20, legacy=True))
    data = {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version, 'platform': platform.platform(),
        'seed': 234, 'peak_process_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'limits': ['offline synthetic fixtures; no model, MCP, side effects, or provider token costs',
            'P95 is empirical order statistic (max for 3/5 preparation samples), not an SLA',
            'WAL file size is allocated WAL extent, not cumulative logical writes; process write bytes include local database writes',
            'legacy measures pinned SessionStore SQL only against current bounded synthetic selection; not a complete old-runtime speed comparison',
            'full legacy large matrix intentionally not written; schema budget can reject requested loads',
            'restore times warm registry revalidation of the selected cache, not cold process startup or database recovery'], 'rows': rows}
    Path(args.output).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'rows': len(rows), 'output': args.output, 'peak_process_rss_kib': data['peak_process_rss_kib']}))


if __name__ == '__main__':
    asyncio.run(main())
