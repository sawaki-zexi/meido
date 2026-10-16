import asyncio
import time
from dataclasses import replace

import pytest

from backend.app.agent_runtime import CapabilityRegistry, CancellationToken, ToolContext
from backend.app.agent_runtime.spawn_tool import SpawnManageTool, SpawnTool
from backend.app.coding_tasks.application import CodingApplication
from backend.app.coding_tasks.config import CodingRoleConfig
from backend.app.models import SpawnToolConfig
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases
from backend.app.task_runtime import TaskOrigin, TaskOutcome, TaskRuntime

pytestmark = pytest.mark.usefixtures('local_platform_only')


class Configuration:
    def resolve(self, role_id, role, repository, *, executor='herdr', **kwargs):
        return ({'id': 'repo-a', 'revision': 'repo-v1', 'root': repository},
                {'id': 'codex-a', 'revision': 'profile-v1', 'executor': executor, 'kind': 'codex'},
                {'executor': executor, 'sha256': 'fixture-only'})

    def close(self):
        pass


class Executor:
    def __init__(self, record, ledger):
        self.record, self.ledger = record, ledger

    def run(self, execution):
        self.ledger.update_attempt(self.record['attemptId'], status='validated_diff', summary='中文结果🙂',
            cleanupState='complete', nativeSession='07e35c16-cd89-40c9-aa4f-ac9257193252')
        return TaskOutcome('succeeded', 0, 'bounded')

    def cancel(self):
        pass


def test_coding_spawn_uses_owned_task_before_chat_provider_and_continues_same_job(tmp_path):
    async def run():
        initialize_databases(tmp_path / 'data')
        sessions = SessionStore(tmp_path / 'data/sessions.db')
        sessions.open_role_session('r')
        tasks = TaskRuntime(tmp_path / 'tasks')
        application = CodingApplication(tmp_path / 'coding', tasks, sessions,
            role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
            config=Configuration(), executor_factory=Executor)
        context = ToolContext('r', 'role:r', 'parent', CancellationToken(), 'start')
        origin = lambda ctx: TaskOrigin(ctx.role_id, ctx.session_key, ctx.run_id, ctx.tool_call_id)
        calls = []
        def unused_provider():
            calls.append('created')
            raise AssertionError('coding must not create a chat provider')
        tool = SpawnTool(tasks, SpawnToolConfig(enabled=True), origin_resolver=origin, authorized=lambda _: True,
            coding_application=lambda: application, coding_access={})
        tool.bind(CapabilityRegistry([]).resolve(role_id='r', session_key='role:r', run_id='parent'), unused_provider)
        manager = SpawnManageTool(tasks, origin_resolver=origin, authorized=lambda _: True,
            coding_application=lambda: application, coding_access={})
        try:
            arguments = {'task': '完整需求🙂', 'profile': 'coding', 'repository': str(tmp_path),
                         'run_in_background': True, 'retry_count': 0}
            result = await tool.execute(arguments, context)
            assert not result.is_error, result
            job = result.details['job_id']
            assert result.details['executor'] == 'codex_exec'
            assert not calls
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                await application.tick()
                found = await manager.execute({'action': 'get', 'job_id': job}, context)
                if found.details.get('deliveryStatus') == 'sent':
                    break
                await asyncio.sleep(.01)
            assert found.details['summary'] == '中文结果🙂'
            assert not found.details['runtimeAckDirty']
            assert tasks.list(origin(context), kind='coding_codex')[0].kind == 'coding_codex'
            assert not tasks.pending_completions(), 'ordinary spawn summary worker must not own coding'
            assert len(sessions.list_messages('role:r')) == 1
            context = replace(context, tool_call_id='continue')
            continued = await manager.execute({'action': 'continue', 'job_id': job, 'task': '下一步'}, context)
            assert not continued.is_error, continued
            assert continued.details['job_id'] == job
            assert continued.details['attemptId'] != result.details['attemptId']
            assert not calls
        finally:
            await application.close()
            tasks.close()
    asyncio.run(run())


@pytest.mark.parametrize('patch', [
    {'run_in_background': False}, {'run_in_background': None}, {'retry_count': 1}, {'retry_count': None},
    {'repository': None}, {'argv': ['unapproved']}, {'profile': 'scripting'},
], ids=['foreground', 'background-omitted', 'retry', 'retry-omitted', 'repository-omitted', 'argv', 'ordinary-repository'])
def test_coding_requires_explicit_background_zero_retry_and_exclusive_schema(tmp_path, patch):
    async def run():
        tasks = TaskRuntime(tmp_path / 'tasks')
        context = ToolContext('r', 'role:r', 'parent', CancellationToken(), 'call')
        def unavailable(): raise AssertionError('invalid input reached a provider or coding application')
        tool = SpawnTool(tasks, SpawnToolConfig(enabled=True), origin_resolver=lambda _: TaskOrigin('r', 'role:r'),
            authorized=lambda _: True, coding_application=unavailable, coding_access={})
        tool.bind(CapabilityRegistry([]).resolve(role_id='r', session_key='role:r', run_id='parent'), unavailable)
        arguments = {'task': 'work', 'profile': 'coding', 'repository': str(tmp_path), 'run_in_background': True, 'retry_count': 0}
        arguments.update(patch)
        arguments = {key: value for key, value in arguments.items() if value is not None}
        try:
            assert (await tool.execute(arguments, context)).is_error
            assert not tasks.list(TaskOrigin('r', 'role:r'), kind='coding_codex')
        finally:
            tasks.close()
    asyncio.run(run())


def test_spawn_revoke_and_reenable_does_not_revive_frozen_delegate_admission(tmp_path):
    from backend.app.coding_tasks.config import CodingConfigStore
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('r')
    tasks = TaskRuntime(tmp_path / 'tasks')
    class Config(Configuration):
        def __init__(self): self.store = CodingConfigStore(tmp_path / 'config.db')
        def delegation_generation(self, role_id): return self.store.delegation_generation(role_id)
        def revoke_delegation(self, role_id): self.store.revoke_delegation(role_id)
        def close(self): self.store.close()
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True,
        config=Config(), executor_factory=Executor)
    origin = TaskOrigin('r', 'role:r', 'old-parent', 'later-call')
    access = {'roleId': 'r', 'enabled': True, 'generation': 0, 'delegationGeneration': 0,
              'repositories': {'repo-a': 'repo-v1'}, 'profiles': {'codex-a': 'profile-v1'}}
    try:
        asyncio.run(application.invalidate_delegation('r'))
        with pytest.raises(PermissionError):
            application.start_task(origin, repository=str(tmp_path), requirement='old frozen delegate',
                                   executor='codex_exec', frozen_access=access)
        assert not application.list_tasks(origin, executor='codex_exec')
    finally:
        asyncio.run(application.close())
        tasks.close()
