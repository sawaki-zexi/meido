"""Real child-process protocol; never invokes the user's Herdr executable."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from backend.app.herdr_control.cli import HerdrCli, CommandError
from backend.app.herdr_control.config import HerdrConfig
from backend.app.herdr_control.service import HerdrControl
from backend.app.herdr_control.commands import ReportTarget
from backend.app.task_runtime import TaskOrigin


@pytest.fixture(scope='session')
def native_cli(tmp_path_factory):
    compiler = shutil.which('gcc')
    if compiler is None:
        pytest.skip('gcc is required for the native Herdr argv fixture')
    output = tmp_path_factory.mktemp('herdr-native') / ('fixture.exe' if os.name == 'nt' else 'fixture')
    subprocess.run([compiler, str(Path(__file__).parent / 'herdr_fixtures/cli.c'), '-o', str(output),
                    *(['-lshell32'] if os.name == 'nt' else [])], check=True,
                   **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    return output


@pytest.fixture
def cli_files(tmp_path, monkeypatch, native_cli):
    root = tmp_path / 'cli'
    root.mkdir()
    monkeypatch.setenv('MEIDO_HERDR_FIXTURE', str(root))
    agent = {'pane_id': 'pane-manual', 'workspace_id': 'work-1', 'agent': 'codex',
             'agent_status': 'idle', 'terminal_title_stripped': 'Manual agent', 'cwd': 'D:/scratch', 'state_change_seq': 7}
    values = {'version': 'herdr 0.9.4', 'server': {'running': True, 'version': '0.9.4', 'compatible': True, 'endpoint_compatible': True},
              'agents': {'agents': [agent]}, 'workspaces': {'workspaces': [{'workspace_id': 'work-1', 'label': 'Manual', 'number': 1}]},
              'get': {'agent': agent}, 'prompt': {'agent': {**agent, 'agent_status': 'working'}}, 'keys': {}, 'read': 'x' * 5000 + 'tail'}
    for name, value in values.items():
        if isinstance(value, dict):
            value = json.dumps(value if name == 'server' else {'ok': True, 'result': value})
        (root / (name + '.txt')).write_text(value, encoding='utf-8')
    return root, HerdrConfig(executable=str(native_cli)), agent


def calls(root):
    return [json.loads(line) for line in (root / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]


def test_native_manual_agents_inputs_and_finished_report(cli_files):
    root, config, _ = cli_files
    reports = []
    async def report(identifier, text):
        reports.append((identifier, json.loads(text)))
        return True
    target = ReportTarget(TaskOrigin('role-a', 'role:role-a', 'run-a'), report)
    async def run():
        service = HerdrControl(config, root)
        service.watcher.poll_seconds = .01
        try:
            listed = await service.list_agents()
            assert listed['workspaces'][0]['agents'][0]['pane_id'] == 'pane-manual'
            assert listed['workspaces'][0]['agents'][0]['handler'] is None
            command = await service.send('prompt', 'pane-manual', 'hello " & %\n世界', 'Alice', target)
            assert command.status == 'sent'
            for _ in range(200):
                if reports:
                    break
                await asyncio.sleep(.01)
            assert reports[0][1]['outcome'] == 'finished'
            assert reports[0][0] == 'herdr-agent:' + command.id
            detail = await service.read_agent('pane-manual')
            assert detail['handler']['role_id'] == 'role-a'
            assert len(detail['output']) == 4000
            assert detail['commands'][-1]['report'] == 'reported'
            await service.send('keys', 'pane-manual', ['ctrl+c', 'enter'], 'Alice', target)
        finally:
            await service.close()
    asyncio.run(run())
    prompt = [args for args in calls(root) if args[2:4] == ['agent', 'prompt']]
    keys = [args for args in calls(root) if args[2:4] == ['agent', 'send-keys']]
    assert prompt == [['--session', 'default', 'agent', 'prompt', 'pane-manual', 'hello " & %\n世界', '--wait', '--until', 'working', '--until', 'blocked', '--timeout', '10000']]
    assert keys == [['--session', 'default', 'agent', 'send-keys', 'pane-manual', 'ctrl+c', 'enter']]


def test_cli_invalid_response_and_timeout_are_not_replayed(cli_files):
    root, config, _ = cli_files
    (root / 'keys.txt').write_text('not-json')
    async def run():
        cli = HerdrCli(config, root)
        with pytest.raises(CommandError, match='响应') as error:
            await cli.call('agent', 'send-keys', 'pane-manual', 'enter')
        assert error.value.code == 'invalid_response'
        (root / 'get.delay').write_text('5000')
        with pytest.raises(CommandError) as error:
            await cli.call('agent', 'get', 'pane-manual', timeout=.1)
        assert error.value.code == 'timeout'
    asyncio.run(run())
    assert len(calls(root)) == 2


def test_selector_loop_cancellation_joins_only_cli_child(cli_files, monkeypatch):
    from backend.app.herdr_control import cli as cli_module
    root, config, _ = cli_files
    (root / 'keys.delay').write_text('10000')
    children = []
    popen = cli_module.subprocess.Popen
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(cli_module.subprocess, 'Popen', spawn)
    async def report(*_):
        return True
    target = ReportTarget(TaskOrigin('r', 'role:r', 'run'), report)
    async def run():
        service = HerdrControl(config, root)
        operation = asyncio.create_task(service.send('keys', 'pane-manual', ['enter'], 'R', target))
        try:
            for _ in range(300):
                if (root / 'calls.jsonl').exists() and any(args[2:4] == ['agent', 'send-keys'] for args in calls(root)):
                    break
                await asyncio.sleep(.01)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert children and all(child.poll() is not None for child in children)
            assert service.log.records('default', 'pane-manual')[-1]['status'] == 'unconfirmed'
            assert sum(args[2:4] == ['agent', 'send-keys'] for args in calls(root)) == 1
        finally:
            await service.close()
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(run())


@pytest.mark.skipif(os.name != 'nt', reason='Windows batch parsing boundary')
def test_windows_batch_rejects_reinterpreted_arguments(cli_files):
    root, config, _ = cli_files
    batch = root / 'fixture.cmd'
    batch.write_text('@echo should-never-run')
    config.executable = str(batch)
    async def run():
        cli = HerdrCli(config, root)
        for text in ['hello & echo other', '%PATH%', '"quoted"', '(group)', 'line\nbreak']:
            with pytest.raises(CommandError) as failure:
                await cli.call('agent', 'prompt', 'pane-manual', text)
            assert failure.value.code == 'unsafe_windows_batch_argument'
    asyncio.run(run())
    assert not (root / 'calls.jsonl').exists()


def test_background_input_requires_actual_handler_but_reads_still_work(cli_files):
    from backend.app.herdr_control.tools import build_agent_tools
    from backend.app.herdr_control.commands import Handover
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.agent_runtime.provider import CancellationToken
    root, config, _ = cli_files
    async def report(*_):
        return True
    target = ReportTarget(TaskOrigin('r', 'role:r', 'run-user'), report)
    def denied(_):
        raise PermissionError('not a user-initiated turn')
    async def run():
        service = HerdrControl(config, root)
        try:
            service.watcher.take_over('default', 'pane-manual', Handover('R', target))
            tools = build_agent_tools(lambda: service, role_id='r', role_name='R', enabled=lambda: True, capture=denied)
            context = ToolContext('r', 'role:r', 'background', CancellationToken())
            assert not (await tools[0].execute({}, context)).is_error
            assert (await tools[2].execute({'pane_id': 'pane-manual', 'text': 'forbidden'}, context)).is_error
            other = build_agent_tools(lambda: service, role_id='other', role_name='Other', enabled=lambda: True,
                capture=denied, report_target=ReportTarget(TaskOrigin('other', 'role:other', 'run-other'), report))
            result = await other[3].execute({'pane_id': 'pane-manual', 'keys': ['enter']},
                ToolContext('other', 'role:other', 'report-other', CancellationToken()))
            assert result.details['error']['code'] == 'not_permitted'
        finally:
            await service.close()
    asyncio.run(run())
    assert not any(args[2:4] in (['agent', 'prompt'], ['agent', 'send-keys']) for args in calls(root))


def test_reports_group_by_original_target_and_commands_have_bounded_history(cli_files):
    from backend.app.herdr_control.commands import AgentCommand, CommandSource
    root, config, _ = cli_files
    deliveries = []
    async def report_a(identifier, text):
        deliveries.append(('a', identifier, json.loads(text)))
        return True
    async def report_b(identifier, text):
        deliveries.append(('b', identifier, json.loads(text)))
        return False  # Unknown delivery is attempted once, not retried.
    a = ReportTarget(TaskOrigin('a', 'role:a', 'run-a'), report_a)
    b = ReportTarget(TaskOrigin('b', 'thread:b', 'run-b'), report_b)
    async def run():
        service = HerdrControl(config, root)
        service.watcher.poll_seconds = .01
        commands = []
        for target in (a, b, a):
            command = AgentCommand('prompt', target.role_id, CommandSource('role', target.role_id))
            command.settle('sent')
            commands.append(command)
            service.watcher.follow(config, 'pane-manual', command, target, working=True)
        for _ in range(200):
            if len(deliveries) == 2:
                break
            await asyncio.sleep(.01)
        assert [(row[0], row[1]) for row in deliveries] == [
            ('a', 'herdr-agent:' + commands[2].id), ('b', 'herdr-agent:' + commands[1].id)]
        assert len(deliveries[0][2]['commands']) == 2
        assert commands[1].report == 'failed' and commands[2].report == 'reported'
        await asyncio.sleep(.03)
        assert len(deliveries) == 2
        for index in range(105):
            service.log.add('default', 'pane-manual', AgentCommand('prompt', str(index), CommandSource('role', 'a')))
        assert len(service.log.records('default', 'pane-manual')) == 100
        detail = await service.read_agent('pane-manual')
        assert len(detail['commands']) == 20
        assert detail['commands'][0]['content'] == '85'
        await service.close()
    asyncio.run(run())


def test_new_command_discards_a_read_started_before_it(cli_files):
    root, config, agent = cli_files
    (root / 'get.txt').write_text(json.dumps({'ok': True, 'result': {'agent': {**agent, 'agent_status': 'blocked'}}}))
    (root / 'get.delay').write_text('600')
    reports = []
    async def report(identifier, text):
        reports.append(json.loads(text))
        return True
    target = ReportTarget(TaskOrigin('r', 'role:r', 'run'), report)
    async def run():
        service = HerdrControl(config, root)
        service.watcher.poll_seconds = .01
        try:
            await service.send('prompt', 'pane-manual', 'first', 'R', target)
            for _ in range(100):
                if any(args[2:4] == ['agent', 'get'] for args in calls(root)):
                    break
                await asyncio.sleep(.01)
            # This working observation cannot rewrite the native process's
            # already captured old blocked response.
            (root / 'get.txt').write_text(json.dumps({'ok': True, 'result': {'agent': {**agent, 'agent_status': 'working'}}}))
            await service.send('prompt', 'pane-manual', 'second', 'R', target)
            await asyncio.sleep(.65)
            assert not reports
            (root / 'get.txt').write_text(json.dumps({'ok': True, 'result': {'agent': agent}}))
            (root / 'get.delay').unlink()
            for _ in range(250):
                if reports:
                    break
                await asyncio.sleep(.01)
            assert len(reports) == 1 and reports[0]['outcome'] == 'finished'
            assert [item['content'] for item in reports[0]['commands']] == ['first', 'second']
        finally:
            await service.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode,code', [('missing', 'executable_not_found'), ('old', 'unsupported_version'),
    ('server-old', 'unsupported_version'), ('stopped', 'server_unavailable'), ('malformed', 'invalid_response')])
def test_real_dependency_errors(cli_files, mode, code):
    root, config, _ = cli_files
    if mode == 'missing':
        config.executable = str(root / 'not-installed')
    elif mode == 'old':
        (root / 'version.txt').write_text('herdr 0.9.3')
    elif mode == 'malformed':
        (root / 'server.txt').write_text('[]')
    else:
        (root / 'server.txt').write_text(json.dumps({'running': mode != 'stopped', 'version': '0.9.3',
            'compatible': True, 'endpoint_compatible': True}))
    async def run():
        from backend.app.herdr_control.tools import build_agent_tools
        from backend.app.agent_runtime.tools import ToolContext
        from backend.app.agent_runtime.provider import CancellationToken
        service = HerdrControl(config, root)
        tools = build_agent_tools(lambda: service, role_id='r', role_name='R', enabled=lambda: True, capture=lambda _: None)
        result = await tools[0].execute({}, ToolContext('r', 'role:r', 'run', CancellationToken()))
        assert result.is_error
        assert result.details['error']['code'] == code
        await service.close()
    asyncio.run(run())


@pytest.mark.parametrize('response,expected', [('garbage', 'unconfirmed'),
    (json.dumps({'ok': False, 'error': {'code': 'agent_prompt_stalled', 'message': 'stalled'}}), 'unconfirmed'),
    (json.dumps({'ok': False, 'error': {'code': 'agent_blocked', 'message': 'blocked'}}), 'failed')])
def test_send_outcomes_and_no_automatic_repeat(cli_files, response, expected):
    root, config, _ = cli_files
    (root / 'prompt.txt').write_text(response)
    async def report(*_):
        return True
    target = ReportTarget(TaskOrigin('r', 'role:r', 'run'), report)
    async def run():
        service = HerdrControl(config, root)
        try:
            with pytest.raises(CommandError):
                await service.send('prompt', 'pane-manual', 'once', 'R', target)
            records = service.log.records('default', 'pane-manual')
            assert records[-1]['status'] == expected
            assert records[-1]['report'] == ('pending' if expected == 'unconfirmed' else 'none')
        finally:
            await service.close()
    asyncio.run(run())
    assert sum(args[2:4] == ['agent', 'prompt'] for args in calls(root)) == 1


def test_owned_agent_refuses_prompt_and_keys_without_cli_input(cli_files):
    root, config, _ = cli_files
    async def report(*_):
        return True
    target = ReportTarget(TaskOrigin('r', 'role:r', 'run'), report)
    async def run():
        service = HerdrControl(config, root, task_for=lambda session, pane: ('coding-task-1', True))
        for kind, content in [('prompt', 'once'), ('keys', ['ctrl+c'])]:
            with pytest.raises(ValueError, match='herdr_cancel_task'):
                await service.send(kind, 'pane-manual', content, 'R', target)
        detail = await service.read_agent('pane-manual')
        assert detail['agent']['task_id'] == 'coding-task-1' and detail['can_prompt'] is False
        await service.close()
    asyncio.run(run())
    assert not any(args[2:4] in (['agent', 'prompt'], ['agent', 'send-keys']) for args in calls(root))


def test_owned_ledger_binding_blocks_input_until_task_cancel(cli_files, tmp_path, monkeypatch):
    from backend.app.coding_tasks.application import CodingApplication
    from backend.app.coding_tasks.config import CodingRoleConfig
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    from backend.app.task_runtime import TaskRuntime
    from test_coding_application import GrantedConfiguration
    root, config, _ = cli_files
    initialize_databases(tmp_path / 'owned')
    sessions = SessionStore(tmp_path / 'owned/sessions.db')
    sessions.open_role_session('r')
    tasks = TaskRuntime(tmp_path / 'tasks')
    application = CodingApplication(tmp_path / 'coding', tasks, sessions,
        role_config=lambda _: CodingRoleConfig(enabled=True), authorized=lambda _: True, config=GrantedConfiguration())
    # Pause at admission: the real task ledger is populated without launching
    # an owned server. Native bindings are the executor's existing ledger seam.
    monkeypatch.setattr(application, '_submit', lambda record: None)
    origin = TaskOrigin('r', 'role:r', 'run', 'task-call')
    record = application.start_task(origin, repository=str(tmp_path), requirement='fixture task')
    application.ledger.update_attempt(record['attemptId'], binding={'pane_id': 'pane-manual'})
    config.session = 'meido-' + record['id'][7:39]
    async def report(*_):
        return True
    async def run():
        service = HerdrControl(config, root, task_for=application.session_agent_task)
        try:
            assert application.inspect(record['id'], origin)['status'] == 'accepted'
            with pytest.raises(ValueError, match='herdr_cancel_task'):
                await service.send('keys', 'pane-manual', ['ctrl+c'], 'R', ReportTarget(origin, report))
            assert not (root / 'calls.jsonl').exists()
            assert application.cancel_task(record['id'], origin)['status'] == 'cancelled'
            sent = await service.send('keys', 'pane-manual', ['enter'], 'R', ReportTarget(origin, report))
            assert sent.status == 'sent'
        finally:
            await service.close()
            await application.close()
    try:
        asyncio.run(run())
    finally:
        tasks.close()


@pytest.mark.parametrize('outcome', ['finished', 'idle', 'blocked', 'gone', 'unreadable', 'timeout'])
def test_native_watch_outcomes_and_reconfiguration(cli_files, outcome):
    root, config, agent = cli_files
    if outcome == 'gone':
        (root / 'get.txt').write_text(json.dumps({'ok': False, 'error': {'code': 'agent_not_found', 'message': 'gone'}}))
    elif outcome == 'unreadable':
        (root / 'get.txt').write_text('broken')
    else:
        (root / 'get.txt').write_text(json.dumps({'ok': True, 'result': {'agent': {
            **agent, 'agent_status': 'blocked' if outcome == 'blocked' else 'working' if outcome == 'timeout' else 'idle'}}}))
    reports = []
    async def report(identifier, text):
        reports.append((identifier, json.loads(text)))
        return True
    target = ReportTarget(TaskOrigin('r', 'role:r', 'run'), report)
    async def run():
        service = HerdrControl(config, root)
        service.watcher.poll_seconds = .01
        service.watcher.start_grace_seconds = .03
        try:
            command = await service.send('prompt', 'pane-manual', 'once', 'R', target)
            watch = service.watcher._watches[('default', 'pane-manual')]
            if outcome == 'idle':
                watch.working = False
            if outcome == 'timeout':
                watch.config = watch.config.model_copy(update={'task_timeout_seconds': .03})
            service.reconfigure(config.model_copy(update={'cli_timeout_seconds': 20}))
            assert service.watcher.handler('default', 'pane-manual')['role_id'] == 'r'
            for _ in range(300):
                if reports:
                    break
                await asyncio.sleep(.01)
            assert len(reports) == 1
            assert reports[0][1]['outcome'] == outcome
            assert command.report == 'reported'
            if outcome == 'unreadable':
                assert sum(args[2:4] == ['agent', 'get'] for args in calls(root)) == 5
        finally:
            await service.close()
        fresh = HerdrControl(config, root)
        assert fresh.watcher.handler('default', 'pane-manual') is None
        assert fresh.log.records('default', 'pane-manual') == []
        await fresh.close()
    asyncio.run(run())


def test_blocked_key_baseline_and_new_command_invalidates_read(cli_files):
    root, config, agent = cli_files
    blocked = {**agent, 'agent_status': 'blocked'}
    (root / 'get.txt').write_text(json.dumps({'ok': True, 'result': {'agent': blocked}}))
    reports = []
    async def report(identifier, text):
        reports.append((identifier, json.loads(text)))
        return True
    target = ReportTarget(TaskOrigin('r', 'role:r', 'run'), report)
    async def run():
        service = HerdrControl(config, root)
        service.watcher.poll_seconds = .01
        service.watcher.start_grace_seconds = 1
        try:
            first = await service.send('keys', 'pane-manual', ['enter'], 'R', target)
            await asyncio.sleep(.12)
            assert not reports  # Same blocked state is not a new question.
            (root / 'get.delay').write_text('250')
            await asyncio.sleep(.08)  # One observation is now in flight.
            second = await service.send('prompt', 'pane-manual', 'new instruction', 'R', target)
            (root / 'get.txt').write_text(json.dumps({'ok': True, 'result': {'agent': {**blocked, 'state_change_seq': 8}}}))
            for _ in range(200):
                if reports:
                    break
                await asyncio.sleep(.01)
            assert reports[0][0] == 'herdr-agent:' + second.id
            assert len(reports[0][1]['commands']) == 2
            assert first.report == second.report == 'reported'
        finally:
            await service.close()
    asyncio.run(run())


def test_public_chat_config_and_handler_report_keep_actual_target(cli_files, tmp_path, monkeypatch, local_platform_only):
    import httpx
    from test_api import configure_session_api
    from backend.app import main
    from backend.app.agent_runtime import RuntimeManager
    from backend.app.model_adapter import ModelToolCall, ModelResponseDone, ModelTextDelta
    from backend.app.plugin_configuration import PluginConfigurationStore
    from backend.app.task_runtime import TaskRuntime
    root, config, _ = cli_files
    seen = []
    class Adapter:
        async def stream_structured_messages(self, messages, configuration, tools):
            names = {item['function']['name'] for item in tools}
            report = any(item['role'] == 'user' and 'Herdr agent 回报' in str(item.get('content')) for item in messages)
            seen.append((report, messages, names))
            current_calls = [item for item in seen if item[0] == report]
            step = len(current_calls)
            if not report and step == 1:
                assert 'herdr_prompt_agent' not in names
                yield ModelToolCall('find-agents', 'tool_search', {'query': 'select:herdr_list_agents,herdr_prompt_agent'})
            elif not report and step == 2:
                yield ModelToolCall('list-manual', 'herdr_list_agents', {})
                yield ModelToolCall('send-manual', 'herdr_prompt_agent', {'pane_id': 'pane-manual', 'text': 'public entry'})
            elif report and step == 1:
                assert 'read_file' in names  # The ordinary role directory remains available.
                yield ModelToolCall('find-keys', 'tool_search', {'query': 'select:herdr_send_keys'})
            elif report and step == 2:
                yield ModelToolCall('respond-handler', 'herdr_send_keys', {'pane_id': 'pane-manual', 'keys': ['enter']})
                yield ModelToolCall('ordinary-read', 'read_file', {'path': 'note.txt'})
            else:
                yield ModelTextDelta('代理回报已处理' if report else '已接手')
                yield ModelResponseDone('stop')
                return
            yield ModelResponseDone('tool_use')
    role, sessions, _ = configure_session_api(tmp_path, monkeypatch, Adapter())
    tasks = TaskRuntime(tmp_path / 'tasks')
    monkeypatch.setattr(main, 'task_runtime', tasks)
    monkeypatch.setattr(main, 'runtime_manager', RuntimeManager(sessions))
    monkeypatch.setattr(main, 'plugin_configuration', PluginConfigurationStore(tmp_path / 'plugins.json'))
    monkeypatch.setattr(main, 'data_root', tmp_path / 'data')
    monkeypatch.setattr(main, '_herdr', None)
    monkeypatch.setattr(main, '_coding', None)
    (main.store.workspace_path(role.id) / 'note.txt').write_text('ordinary read allowed')
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://local') as client:
            defaults = (await client.get('/api/plugin-management/herdr')).json()
            assert defaults['values']['session'] == 'default' and defaults['enabled'] is False
            saved = await client.put('/api/plugin-management/herdr/configuration', json={'values': config.model_dump(), 'expectedRevision': 0})
            assert saved.status_code == 200, saved.text
            on = await client.put('/api/plugin-management/herdr/enabled', json={'enabled': True, 'expectedRevision': 1})
            assert on.status_code == 200, on.text
            status = (await client.get('/api/plugin-management/herdr/status')).json()
            assert status['workspaces'][0]['agents'][0]['title'] == 'Manual agent'
            service = main._herdr_control()
            service.watcher.poll_seconds = .01
            response = await client.post(f'/api/roles/{role.id}/messages', json={'content': '接手手动代理'})
            assert response.status_code == 200 and '已接手' in response.text
            for _ in range(500):
                if any(row.role == 'assistant' and row.content == '代理回报已处理' for row in sessions.list_messages('role:' + role.id)):
                    break
                await asyncio.sleep(.01)
            rows = sessions.list_messages('role:' + role.id)
            assert any(row.content == '代理回报已处理' for row in rows), {
                'rows': [(row.messageType, row.content[:300]) for row in rows],
                'commands': service.log.records('default', 'pane-manual'),
                'runs': [sessions.get_run(row.runId).model_dump() for row in rows if row.runId]}
            assert any(row.toolName == 'herdr_send_keys' and not row.isError for row in rows)
            assert any(row.toolName == 'read_file' and 'ordinary read allowed' in row.content for row in rows)
            event = next(row for row in rows if row.metadata.get('internalEvent') == 'herdr_agent_report')
            assert sessions.get_run(event.runId).modelSnapshot['humanInputSource'] == 'herdr_report'
            public = (await client.get(f'/api/roles/{role.id}/session')).json()['messages']
            assert all(row['id'] != event.id for row in public)
            assert any(row['content'] == '代理回报已处理' for row in public)
            assert not any(row.id == event.id for row in main._owner_history(role.id).read())
            current = (await client.get('/api/plugin-management/herdr')).json()
            edited = await client.put('/api/plugin-management/herdr/configuration', json={
                'values': {'cli_timeout_seconds': 20}, 'expectedRevision': current['revision']})
            assert edited.status_code == 200
            assert service.watcher.handler('default', 'pane-manual')['role_id'] == role.id
            off = await client.put('/api/plugin-management/herdr/enabled', json={'enabled': False, 'expectedRevision': edited.json()['revision']})
            assert off.status_code == 200 and service.closed
        await service.close()
        if main._coding is not None:
            await main._coding.close()
    try:
        asyncio.run(run())
    finally:
        tasks.close()
    commands = calls(root)
    assert sum(args[2:4] == ['agent', 'prompt'] for args in commands) == 1
    assert sum(args[2:4] == ['agent', 'send-keys'] for args in commands) == 1
