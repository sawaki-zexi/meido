"""Managed Drift files share ordinary FileTools I/O but reserve real disk peaks."""
import asyncio
from contextlib import contextmanager

import pytest

from backend.app import main
from backend.app.agent_runtime.file_tools import build_file_tools
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_s06_cannot_write_over_an_externally_full_drift_tree_and_old_files_survive(api):
    client, roles, _, _ = api
    role = client.post('/api/roles', json={'name': '配额', 'profile': {'profile': 'local'}}).json()['role']['id']
    root = roles.workspace_path(role) / 'drift'
    root.mkdir()
    for index in range(256):
        (root / f'unknown-{index}.txt').write_text('owned externally', encoding='utf-8')
    owner = main._proactive_application()
    try:
        tool = owner.drift_skills.author_tools(build_file_tools(role, root.parent), role,
            authorized=lambda context: True)[2]
        result = asyncio.run(tool.execute({'path': 'drift/new.txt', 'content': 'new'},
            ToolContext(role, f'role:{role}', 'ordinary-owner', CancellationToken())))
        assert result.is_error, result
        assert not (root / 'new.txt').exists()
        assert len(list(root.iterdir())) == 256
        assert (root / 'unknown-0.txt').read_text() == 'owned externally'
    finally:
        asyncio.run(owner.close())


def test_managed_write_reserves_old_plus_temporary_bytes_and_preserves_previous_on_exhaustion(api):
    from backend.app.proactive_runtime.drift_workspace import DriftFiles, FileLimits
    client, roles, _, _ = api
    role = client.post('/api/roles', json={'name': '峰值', 'profile': {'profile': 'local'}}).json()['role']['id']
    workspace = roles.workspace_path(role)
    owner = main._proactive_application()
    managed = DriftFiles(owner, owner.drift_skills.workspace, limits=FileLimits(role_bytes=12, role_items=8, headroom_bytes=0, headroom_items=0))
    tool = build_file_tools(role, workspace, write_policy=managed.policy(role))[2]
    context = ToolContext(role, f'role:{role}', 'real-owner', CancellationToken())
    try:
        first = asyncio.run(tool.execute({'path': 'drift/notes.txt', 'content': '12345678'}, context))
        assert not first.is_error, first
        second = asyncio.run(tool.execute({'path': 'drift/notes.txt', 'content': 'abcdefgh'}, context))
        assert second.is_error, second
        assert (workspace / 'drift/notes.txt').read_text() == '12345678'
        with owner._transaction() as db:
            row = db.execute('SELECT * FROM autonomy_drift_allocations WHERE role_id=?', (role,)).fetchone()
            assert row['committed_bytes'] == 8 and row['reserved_bytes'] == 0
            assert db.execute("SELECT COUNT(*) FROM autonomy_drift_write_journal WHERE status='pending'").fetchone()[0] == 0
            from backend.app.proactive_runtime import retention
            assert retention.usage(db, role)['filesystemBytes'] == 8
    finally:
        asyncio.run(owner.close())


def test_replaced_temporary_is_never_deleted_as_an_owned_file_and_pending_journal_keeps_its_reservation(api):
    client, roles, _, _ = api
    role = client.post('/api/roles', json={'name': '身份', 'profile': {'profile': 'local'}}).json()['role']['id']
    workspace = roles.workspace_path(role)
    root = workspace / 'drift'
    root.mkdir()
    target = root / 'notes.txt'
    target.write_bytes(b'original')
    app = main._proactive_application()
    replaced = []
    class BeforePublish:
        def __init__(self, allocation):
            self.allocation = allocation
        def __getattr__(self, name):
            return getattr(self.allocation, name)
        def check(self):
            self.allocation.check()
            if self.temporary.exists():
                held = root / 'retained-real-owner-temp'
                self.temporary.rename(held)
                self.temporary.write_bytes(b'foreign replacement must survive')
                replaced.append((self.temporary, held))
                raise ValueError('controlled same-path temporary replacement before publish')
    @contextmanager
    def swapped(path, data, context):
        with app.drift_files.policy(role)(path, data, context) as allocation:
            yield BeforePublish(allocation)
    tool = build_file_tools(role, workspace, write_policy=swapped)[2]
    try:
        result = asyncio.run(tool.execute({'path': 'drift/notes.txt', 'content': 'new bytes'},
            ToolContext(role, f'role:{role}', 'original-real-write', CancellationToken())))
        assert result.is_error and target.read_bytes() == b'original'
        temporary, held = replaced[0]
        assert temporary.read_bytes() == b'foreign replacement must survive'
        with pytest.raises(PermissionError):
            app.drift_files.reconcile(role)
        with app._transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM autonomy_drift_write_journal WHERE status='pending'").fetchone()[0] == 1
            assert db.execute('SELECT reserved_bytes FROM autonomy_drift_allocations WHERE role_id=?', (role,)).fetchone()[0] > 0
        # The test owner restores its exact recorded inode, then reconciliation
        # may remove only that actual orphan. No model write is replayed.
        temporary.unlink()
        held.rename(temporary)
        app.drift_files.reconcile(role)
        assert not temporary.exists() and target.read_bytes() == b'original'
    finally:
        asyncio.run(app.close())


def test_concurrent_writes_cannot_share_last_allocation_and_custom_eight_mib_schema_matches_actual_utf8_limit(api):
    from backend.app.proactive_runtime.drift_workspace import DriftFiles, FileLimits
    client, roles, _, _ = api
    role = client.post('/api/roles', json={'name': '并发', 'profile': {'profile': 'local'}}).json()['role']['id']
    workspace = roles.workspace_path(role)
    app = main._proactive_application()
    managed = DriftFiles(app, app.drift_skills.workspace, limits=FileLimits(role_bytes=12, role_items=8, headroom_bytes=0, headroom_items=0))
    context = ToolContext(role, f'role:{role}', 'real-owner', CancellationToken())
    tools = build_file_tools(role, workspace, write_policy=managed.policy(role))
    async def concurrent():
        return await asyncio.gather(*(tools[2].execute({'path': 'drift/' + name, 'content': '12345678'}, context)
            for name in ('a.txt', 'b.txt')))
    try:
        results = asyncio.run(concurrent())
        assert sum(not result.is_error for result in results) == 1
        assert sum(path.stat().st_size for path in (workspace / 'drift').iterdir()) == 8
        large = build_file_tools(role, workspace, max_file_bytes=8 * 1024 * 1024)[2]
        assert large.definition.input_schema['properties']['content']['maxLength'] == 8 * 1024 * 1024
        exact = 'x' * (8 * 1024 * 1024)
        assert not asyncio.run(large.execute({'path': 'exact-eight.txt', 'content': exact}, context)).is_error
        too_large = asyncio.run(large.execute({'path': 'too-large.txt', 'content': exact[:-1] + '你'}, context))
        assert too_large.is_error and not (workspace / 'too-large.txt').exists()
        (workspace / 'external-large.txt').write_bytes(b'x' * (8 * 1024 * 1024 + 1))
        reader = build_file_tools(role, workspace, max_file_bytes=8 * 1024 * 1024)[0]
        assert asyncio.run(reader.execute({'path': 'external-large.txt'}, context)).is_error
    finally:
        asyncio.run(app.close())


def test_drift_cannot_write_case_alias_of_host_state(api, monkeypatch):
    import json
    from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
    from test_proactive_drift_delivery import prepare, settled
    client, roles, _, _ = api
    role, _, now = prepare(client, monkeypatch)
    class Writer:
        requests = 0
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, args = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests == 2:
                name, args = 'write_file', {'path': 'STATE/reading-notes.json', 'content': 'forged host progress'}
            else:
                assert self.requests == 3 and messages[-1].is_error
                name, args = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '拒绝修改宿主状态',
                    'next_action': '继续合法工作', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'state-{self.requests}', name, args),)))
    model = Writer()
    monkeypatch.setattr(main, 'model_adapter', model)
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert state['runs'][0]['status'] == 'finished_silent' and model.requests == 3, state
        projection = json.loads((roles.workspace_path(role) / 'drift/state/reading-notes.json').read_text(encoding='utf-8'))
        assert projection['runCount'] == 1
    finally:
        asyncio.run(main._proactive.close())
