"""A held ordinary spawn cleanup cannot reopen old Coding authority."""
import asyncio
import threading
import time

import pytest

from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingConfigStore
from backend.app.task_runtime import TaskBudget, TaskOrigin, TaskOutcome
from test_coding_spawn import Configuration, Executor
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_off_on_while_ordinary_spawn_cleanup_is_held_cannot_reuse_old_coding_authority(api, tmp_path, monkeypatch):
    from backend.app import main
    client, roles, sessions, tasks = api
    role = client.post('/api/roles', json={'name': '撤权门禁', 'profile': {'profile': 'fixture'}, 'agentConfig': {
        'spawn': {'enabled': True}, 'coding': {'enabled': True, 'repoGrantIds': ['repo-a'],
        'profileIds': ['codex-a'], 'defaultCodexProfileId': 'codex-a'}}}).json()['role']
    rid = role['id']
    class Config(Configuration):
        def __init__(self): self.store = CodingConfigStore(tmp_path / 'configuration.db')
        def public(self):
            grant, profile, build = self.resolve(rid, None, str(tmp_path), executor='codex_exec')
            return {'enabled': True, 'nativeBuild': build,
                'repositories': [grant | {'allowedRoleIds': [rid]}],
                'profiles': [profile | {'allowedRoleIds': [rid], 'verification': {'status': 'verified', 'revision': profile['revision']}}]}
        def delegation_generation(self, role_id): return self.store.delegation_generation(role_id)
        def revoke_delegation(self, role_id): self.store.revoke_delegation(role_id)
        def close(self): self.store.close()
    app = CodingApplication(tmp_path / 'coding', tasks, sessions, config=Config(),
        role_config=lambda _: roles.get(rid).agentConfig.coding, authorized=lambda _: True,
        delegation_authorized=lambda _: roles.get(rid).agentConfig.spawn.enabled, executor_factory=Executor)
    monkeypatch.setattr(main, '_coding', app)
    access = app.freeze_access(rid, roles.get(rid).agentConfig.coding)
    entered, release = threading.Event(), threading.Event()
    class OrdinaryWork:
        def run(self, execution):
            assert release.wait(15)
            return TaskOutcome('cancelled')
        def cancel(self):
            entered.set()
            assert release.wait(15)
    origin = TaskOrigin(rid, 'role:' + rid, 'parent', 'ordinary-call')
    tasks.submit(origin, OrdinaryWork(), kind='spawn', budget=TaskBudget(30, 1024, 1024))
    responses = []
    def update(enabled):
        responses.append(client.put('/api/roles/' + rid, json={'name': role['name'], 'profile': role['profile'],
            'agentConfig': {'spawn': {'enabled': enabled}}}))
    off, on = threading.Thread(target=update, args=(False,)), threading.Thread(target=update, args=(True,))
    try:
        off.start()
        assert entered.wait(5)
        on.start()
        deadline = time.monotonic() + 5
        while not roles.get(rid).agentConfig.spawn.enabled:
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert off.is_alive() and on.is_alive(), 'controlled cleanup was not held'
        with pytest.raises(PermissionError):
            app.start_task(TaskOrigin(rid, 'role:' + rid, 'old-parent', 'late-coding-call'),
                repository=str(tmp_path), requirement='must remain revoked', executor='codex_exec', frozen_access=access)
        assert not app.list_tasks(origin, executor='codex_exec')
    finally:
        release.set()
        for worker in (off, on):
            if worker.ident is not None:
                worker.join(timeout=8)
                assert not worker.is_alive()
        asyncio.run(app.close())
        tasks.close()
    assert len(responses) == 2 and all(item.status_code == 200 for item in responses)
