"""Exercise server exit while an actual HTTP SSE client remains connected."""
import asyncio
import socket

import httpx
import uvicorn
from backend.app.server import Server

from backend.app import main
from test_api import configure_session_api


def test_server_exit_cancels_owned_reply_without_waiting_for_sse_disconnect(tmp_path, monkeypatch, local_platform_only):
    from backend.app.ai.model_types import ModelTextDelta, ModelThinkingDelta
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Model:
        async def stream_rich_messages(self, messages, configuration, tools):
            try:
                yield ModelThinkingDelta('退出前的真实思考')
                yield ModelTextDelta('部分内容')
                entered.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    role, sessions, _ = configure_session_api(tmp_path, monkeypatch, Model())
    async def close_chat():
        await main._chat_runs().close()
    monkeypatch.setattr(main.app.router, 'on_startup', [])
    monkeypatch.setattr(main.app.router, 'on_shutdown', [close_chat])

    async def scenario():
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        server = Server(uvicorn.Config(main.app, log_level='critical', lifespan='on'))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                await asyncio.sleep(.01)
            path = f'http://127.0.0.1:{sock.getsockname()[1]}/api/roles/{role.id}'
            async with httpx.AsyncClient(timeout=5) as client:
                accepted = await client.post(path + '/runs', json={'content': '退出', 'requestId': 'open-sse-shutdown'})
                assert accepted.status_code in {200, 202}
                await asyncio.wait_for(entered.wait(), 3)
                async with client.stream('GET', path + '/runs/run-open-sse-shutdown/events?after=0') as response:
                    async for line in response.aiter_lines():
                        if line.startswith('data: '):
                            break
                    server.should_exit = True
                    await asyncio.sleep(.5)
                    stopped_while_open = cancelled.is_set()
                    status_while_open = sessions.get_run('run-open-sse-shutdown').status
                await asyncio.wait_for(serving, 5)
                assert stopped_while_open, 'server waited for the SSE client before stopping the owner'
                assert status_while_open == 'cancelled'
                partial = sessions.list_messages(f'role:{role.id}')[-1]
                assert partial.content == '部分内容'
                assert partial.metadata['streamThinking'] == '退出前的真实思考'
        finally:
            server.should_exit = True
            await main._chat_runs().close()
            await asyncio.wait_for(serving, 5)
            sock.close()

    asyncio.run(scenario())


def test_server_exit_waits_for_tool_receipt_and_host_cleanup_before_terminal(tmp_path, monkeypatch, local_platform_only):
    from backend.app.agent_runtime import AgentToolResult, ToolDefinition
    from backend.app.model_adapter import ModelToolCall, ModelResponseDone
    model_waiting, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    class Tool:
        definition = ToolDefinition('list_dir', 'fixture', {'type': 'object', 'properties': {}})
        async def execute(self, arguments, context, on_update=None):
            return AgentToolResult('真实已完成回执')

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append(messages)
            if len(calls) == 1:
                yield ModelToolCall('finished-tool', 'list_dir', {})
                yield ModelResponseDone('tool_use')
            else:
                model_waiting.set()
                await asyncio.Event().wait()

    role, sessions, _ = configure_session_api(tmp_path, monkeypatch, Model())
    monkeypatch.setattr(main, '_runtime_tool_candidates', lambda *args, **kwargs: [Tool()])
    original = main.browser_use_sessions.close_run

    async def cleanup(role_id, run_id):
        if run_id == 'run-server-cleanup':
            cleaning.set()
            await release.wait()
        await original(role_id, run_id)

    async def close_chat():
        await main._chat_runs().close()

    monkeypatch.setattr(main.browser_use_sessions, 'close_run', cleanup)
    monkeypatch.setattr(main.app.router, 'on_startup', [])
    monkeypatch.setattr(main.app.router, 'on_shutdown', [close_chat])

    async def scenario():
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        server = Server(uvicorn.Config(main.app, log_level='critical', lifespan='on'))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                await asyncio.sleep(.01)
            path = f'http://127.0.0.1:{sock.getsockname()[1]}/api/roles/{role.id}'
            async with httpx.AsyncClient(timeout=5) as client:
                accepted = await client.post(path + '/runs', json={'content': '退出', 'requestId': 'server-cleanup'})
                assert accepted.status_code in {200, 202}
                await asyncio.wait_for(model_waiting.wait(), 3)
                async with client.stream('GET', path + '/runs/run-server-cleanup/events?after=0') as response:
                    observed = []
                    async def consume():
                        async for line in response.aiter_lines():
                            if line.startswith('event: '):
                                observed.append(line[7:])
                    reader = asyncio.create_task(consume())
                    server.should_exit = True
                    await asyncio.wait_for(cleaning.wait(), 3)
                    await asyncio.sleep(.05)
                    assert not serving.done()
                    assert 'assistant_failed' not in observed and 'assistant_completed' not in observed
                    assert not main._chat_runs().runs['run-server-cleanup'].finished
                    release.set()
                    await asyncio.wait_for(serving, 5)
                    await asyncio.wait_for(reader, 3)
                    assert 'assistant_failed' in observed
                assert sessions.get_run('run-server-cleanup').status == 'cancelled'
                assert [row.content for row in sessions.list_messages(f'role:{role.id}') if row.messageType == 'tool_result'] == ['真实已完成回执']
                assert [row['resultCategory'] for row in sessions.list_tool_audits('run-server-cleanup')] == ['succeeded']
        finally:
            release.set()
            server.should_exit = True
            await main._chat_runs().close()
            await asyncio.wait_for(serving, 5)
            sock.close()

    asyncio.run(scenario())
