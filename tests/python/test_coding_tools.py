import asyncio

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.coding_tasks.tools import build_coding_tools
from backend.app.task_runtime import TaskOrigin

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_five_herdr_tools_bind_host_origin_and_reject_identity_or_argv_fields():
    class Application:
        def start_task(self, origin, **arguments):
            assert origin.role_id == 'role-a'
            assert origin.session_key == 'role:role-a'
            return {'id': 'coding_fixture', 'attemptId': 'attempt_fixture', 'status': 'accepted'}
    tools = build_coding_tools(lambda: Application(), role_id='role-a', authorized=lambda _: True, access={'enabled': True},
        origin_resolver=lambda context: TaskOrigin(context.role_id, context.session_key, context.run_id, context.tool_call_id))
    assert {tool.definition.name for tool in tools} == {'herdr_start_task', 'herdr_list_tasks', 'herdr_get_task', 'herdr_continue_task', 'herdr_cancel_task'}
    assert all(tool.definition.source == 'builtin:coding' and not tool.definition.external_allowed for tool in tools)
    start = next(tool for tool in tools if tool.definition.name == 'herdr_start_task')
    context = ToolContext('role-a', 'role:role-a', 'run-a', CancellationToken(), 'call-a')
    result = asyncio.run(start.execute({'repository': 'repo-a', 'requirement': '完整需求'}, context))
    assert result.details['status'] == 'accepted'
    assert not result.is_error
    for extra in ({'role_id': 'role-b'}, {'argv': ['anything']}, {'auth': 'secret'}, {'env': {}}):
        rejected = asyncio.run(start.execute({'repository': 'repo-a', 'requirement': '完整需求', **extra}, context))
        assert rejected.is_error
