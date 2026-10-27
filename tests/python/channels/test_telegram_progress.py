import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.app import main
from backend.app.agent_runtime import (
    AssistantDoneEvent, AssistantMessage, RuntimeManager, ToolCall, ToolDefinition,
    ToolExecutionStartEvent, ToolExecutionUpdateEvent, ToolExecutionEndEvent,
)
from backend.app.agent_runtime.capabilities import CapabilityRegistry
from backend.app.agent_runtime.tools import AgentToolResult
from backend.app.channels import ChannelHost
from backend.app.channels.delivery import OutboundScheduler
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.models import TargetKind, target_from_chat
from backend.app.channels.telegram import TelegramAccountAdapter
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.multichannel import MultichannelStore, TelegramBotApi
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from test_runtime_delivery import MemoryCredentials, owned_tasks


@pytest.mark.parametrize('stop', [False, True])
def test_real_runtime_shows_safe_progress_before_tool_returns_and_stops_with_run(tmp_path, monkeypatch, owned_tasks, stop):
    async def scenario():
        requests = []
        shown, completed, typing, release = (asyncio.Event() for _ in range(4))
        def handle(request):
            method, body = request.url.path.rsplit('/', 1)[-1], json.loads(request.content)
            requests.append((method, body))
            if method == 'sendChatAction':
                typing.set()
                return httpx.Response(200, json={'ok': True, 'result': True})
            if '连接服务' in body.get('text', ''):
                shown.set()
            if '已完成' in body.get('text', ''):
                completed.set()
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': body.get('message_id', len(requests))}})
        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
        roles = RoleStore(tmp_path / 'roles')
        role = roles.create(RoleInput(name='role', profile=RoleProfile(profile='profile')))
        sessions = SessionStore(tmp_path / 'sessions.db')
        channels = MultichannelStore(tmp_path / 'sessions.db', sessions, MemoryCredentials())
        account = channels.save_account(role.id, account_id='tg', token='synthetic', enabled=True)
        thread = channels.ensure_channel_thread(role.id, 'telegram', account.account_id, '99', 'private')
        _, user = channels.append_event(thread, direction='inbound', content='hello', external_message_id='1', metadata={'senderId': '99'})
        target = target_from_chat(account_id='tg', chat_id='99', kind=TargetKind.PRIVATE)
        channels.remember_targets(role.id, [target])
        adapter = TelegramAccountAdapter('tg', lambda: 'synthetic', TelegramBotApi(),
            scheduler=OutboundScheduler(edit_interval=.01, typing_interval=.05))
        host = ChannelHost(channels)
        host.register(adapter)
        class Tool:
            definition = ToolDefinition('long_work', 'Work', external_allowed=True)
            async def execute(self, arguments, context, on_update=None):
                on_update(AgentToolResult('secret result', details={'phase': 'connecting', 'token': 'secret token'}))
                await release.wait()
                return AgentToolResult('tool finished')
        tool = Tool()
        class Provider:
            def __init__(self, adapter, role, configuration, *args, **kwargs):
                self.adapter, self.role, self.configuration = adapter, role, configuration
                self.calls = 0
            async def stream_response(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('call-1', 'long_work', {}),), stop_reason='tool_use'))
                else:
                    await asyncio.wait_for(completed.wait(), 2)
                    yield AssistantDoneEvent(AssistantMessage('final answer'))
        async def resolve(_role, *, session_key, run_id, **kwargs):
            return CapabilityRegistry((tool,)).resolve(role_id=role.id, session_key=session_key, run_id=run_id)
        async def commit(*args):
            pass
        worker_lock = asyncio.Lock()
        for name, value in {
            'store': roles, 'session_store': sessions, 'multichannel_store': channels,
            'runtime_manager': RuntimeManager(sessions), 'role_locks': {},
            'model_configuration_store': ModelConfigurationStore(tmp_path / 'models.json'),
            'group_repository': MultichannelGroupRepository(channels), 'channel_host': host,
            'channel_adapters': {'tg': adapter}, 'MeidoProvider': Provider,
            '_runtime_tool_candidates': lambda *a, **kw: (tool,), '_runtime_capabilities': resolve,
            '_role_memory_context': lambda *a: '',
            'memory_worker': SimpleNamespace(role_deletion_blocked=lambda *a: False, role_lock_for=lambda *a: worker_lock),
            'channel_memory_maintenance': SimpleNamespace(commit_turn=commit),
        }.items():
            monkeypatch.setattr(main, name, value)
        task = asyncio.create_task(main._handle_channel_message(thread, 'hello',
            {'messageId': user.id, 'senderId': '99', 'platformMessageId': '1'}, False))
        try:
            await asyncio.wait_for(typing.wait(), 2)
            await asyncio.wait_for(shown.wait(), 2)
            assert not task.done() and not release.is_set()
            serialized = json.dumps(requests)
            assert 'long_work' in serialized and 'secret' not in serialized
            if stop:
                main.runtime_manager.cancel_run(sessions.active_run(role.id).runId)
            else:
                release.set()
            await asyncio.gather(task, return_exceptions=stop)
            before = len(requests)
            await asyncio.sleep(.08)
            assert len(requests) == before
            messages = sessions.list_messages(thread.session_key)
            assert not any('连接服务' in message.content or '已完成' in message.content for message in messages)
            if not stop:
                assert messages[-1].content == 'final answer'
                assert messages[-1].metadata['deliveredContent'] == 'final answer'
                assert sum(body.get('text') == 'final answer' for _, body in requests) == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['latest', 'revoke', 'replace'])
def test_pending_status_coalesces_and_rechecks_original_connection_before_http(tmp_path, monkeypatch, change):
    async def scenario():
        requests = []
        first, edited, waiting, release = (asyncio.Event() for _ in range(4))
        clock = [0.0]
        async def sleep(delay):
            waiting.set()
            await release.wait()
            clock[0] += delay
        def handle(request):
            body = json.loads(request.content)
            requests.append((request.url.path.rsplit('/', 1)[-1], body))
            if 'text' in body:
                (edited if 'message_id' in body else first).set()
                return httpx.Response(200, json={'ok': True, 'result': {'message_id': 7}})
            return httpx.Response(200, json={'ok': True, 'result': True})
        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
        sessions = SessionStore(tmp_path / 'sessions.db')
        channels = MultichannelStore(tmp_path / 'sessions.db', sessions, MemoryCredentials())
        channels.save_account('role', account_id='tg', token='synthetic', enabled=True)
        thread = channels.ensure_channel_thread('role', 'telegram', 'tg', '99', 'private')
        target = target_from_chat(account_id='tg', chat_id='99', kind=TargetKind.PRIVATE)
        channels.remember_targets('role', [target])
        scheduler = OutboundScheduler(send_interval=2, edit_interval=5, typing_interval=8, clock=lambda: clock[0], sleep=sleep)
        adapter = TelegramAccountAdapter('tg', lambda: 'synthetic', TelegramBotApi(), scheduler=scheduler)
        host = ChannelHost(channels)
        host.register(adapter)
        progress = host.start_private_progress(thread, run_id='run', authorize_source=lambda: True)
        try:
            progress.feed(ToolExecutionStartEvent('one', 'work', run_id='run', sequence=1))
            await asyncio.wait_for(first.wait(), 1)
            progress.feed(ToolExecutionEndEvent('one', 'work', AgentToolResult('secret'), run_id='run', sequence=2))
            await asyncio.wait_for(waiting.wait(), 1)
            progress.feed(ToolExecutionStartEvent('two', 'work', run_id='run', sequence=3))
            progress.feed(ToolExecutionUpdateEvent('two', 'work', AgentToolResult('secret', details={'phase': 'connecting'}), run_id='run', sequence=4))
            progress.feed(ToolExecutionEndEvent('two', 'work', AgentToolResult('secret', is_error=True), run_id='run', sequence=5))
            progress.feed(ToolExecutionUpdateEvent('one', 'work', AgentToolResult('late'), run_id='run', sequence=6))
            progress.feed(ToolExecutionStartEvent('other', 'wrong_run', run_id='other', sequence=99))
            progress.feed(ToolExecutionStartEvent('old', 'old_sequence', run_id='run', sequence=1))
            if change == 'revoke':
                channels.save_account('role', account_id='tg', enabled=False)
            elif change == 'replace':
                replacement = TelegramAccountAdapter('tg', lambda: 'replacement', TelegramBotApi())
                replacement.remember_target(target)
                host.register(replacement)
            release.set()
            if change == 'latest':
                await asyncio.wait_for(edited.wait(), 1)
                assert [body['text'] for _, body in requests if 'text' in body] == ['正在执行：work', '执行失败：work']
            else:
                await asyncio.sleep(.03)
                assert [body['text'] for _, body in requests if 'text' in body] == ['正在执行：work']
        finally:
            release.set()
            await progress.close()
    asyncio.run(scenario())


def test_progress_accepts_tool_updates_without_mapping_details():
    from backend.app.agent_runtime.tools import AgentToolResult
    from backend.app.channels.telegram_progress import TelegramProgress

    class Scheduler:
        clock = staticmethod(lambda: 0.0)
    message = SimpleNamespace(authorize_dispatch=lambda: True)
    progress = object.__new__(TelegramProgress)
    progress.run_id = 'run'
    progress.active = True
    progress.sequence = 0
    progress.call_id = 'call'
    progress.tool_name = 'work'
    progress.pending = None
    progress.wake = asyncio.Event()
    progress.feed(ToolExecutionUpdateEvent('call', 'work', AgentToolResult('phase', details=None), run_id='run', sequence=1))
    assert progress.pending[1] == '正在执行：work'
