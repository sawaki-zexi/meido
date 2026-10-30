"""Strict local HTTP contracts: exercise the real model wire without paid services."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time
from threading import Thread
import pytest

from backend.app.advanced_settings import AdvancedSettings
from backend.app.agent_runtime.context_provider import ContextWindowProvider
from backend.app.agent_runtime.capabilities import CapabilityRegistry
from backend.app.agent_runtime.events import MessageEndEvent
from backend.app.agent_runtime.loop import run_agent_loop
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.tools import AgentToolResult, ToolDefinition
from backend.app.agent_runtime.types import AssistantMessage, ImageContent, UserMessage
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.context_windows import ContextWindowStore
from backend.app.models import ModelConfiguration, Role
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


@pytest.fixture(autouse=True)
def local_http_environment(monkeypatch, local_platform_only):
    # The cloud runner may export a SOCKS proxy; these are real loopback sockets.
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')


@contextmanager
def model_server(respond):
    requests, errors = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                assert self.path == '/v1/chat/completions'
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(payload)
                # OpenAI tool content accepts text, not image_url observations.
                pending = set()
                for message in payload['messages']:
                    if message['role'] == 'tool':
                        assert isinstance(message['content'], str)
                        pending.remove(message['tool_call_id'])
                    else:
                        assert not pending, 'every tool reply must precede an observation'
                        pending = {call['id'] for call in message.get('tool_calls', [])}
                assert not pending
                body = respond(payload, len(requests)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
            except Exception as error:
                errors.append(error)
                body = b'{"error":{"message":"invalid request"}}'
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests, errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def response(content='', *, calls=(), arguments=None, finish='stop', sentinel=True, usage=None):
    delta = {'content': content}
    if calls:
        delta['tool_calls'] = [{'index': index, 'id': identifier, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps((arguments or {}).get(identifier, {}))}}
            for index, (identifier, name) in enumerate(calls)]
    choice = {'delta': delta}
    if finish is not None:
        choice['finish_reason'] = finish
    payload = {'choices': [choice], **({'usage': usage} if usage else {})}
    return 'data: ' + json.dumps(payload) + '\n\n' + ('data: [DONE]\n\n' if sentinel else '')


def provider(url, *, context=None, max_tokens=1024):
    now = datetime.now(timezone.utc)
    role = Role(id='r', name='R', profile={'profile': '助手'}, createdAt=now, updatedAt=now)
    configuration = ModelConfiguration(providerId='custom', provider='deepseek', baseUrl=url,
        model='deepseek-v4-pro', contextWindowTokens=context)
    return MeidoProvider(RoutedModelAdapter(ModelAdapterRegistry(timeout=5)), role, configuration,
                         max_response_tokens=max_tokens)


class ImageTool:
    definition = ToolDefinition('inspect', 'inspect image', {'type': 'object', 'properties': {}})

    def execute(self, arguments, context, on_update=None):
        return AgentToolResult('visible reference', images=(ImageContent('image/png', 'aGVsbG8='),))


class ReceiptTool:
    definition = ToolDefinition('receipt', 'read receipt', {'type': 'object', 'properties': {}})

    def execute(self, arguments, context, on_update=None):
        return AgentToolResult('receipt-1')


def test_real_http_multi_tool_image_roundtrip_uses_user_observation():
    def respond(payload, index):
        if index == 1:
            return response(calls=(('image', 'inspect'), ('receipt', 'receipt')), finish='tool_calls')
        assert [message['role'] for message in payload['messages'][-3:]] == ['tool', 'tool', 'user']
        assert payload['messages'][-1]['content'][1]['type'] == 'image_url'
        # Semantically completed stream need not send a redundant [DONE] marker.
        return response('看到了，结果已确认。', sentinel=False)

    with model_server(respond) as (url, requests, errors):
        async def run():
            return [event async for event in run_agent_loop(provider=provider(url), model='m', system='',
                messages=[UserMessage('看看图片和回执')], tools=[ImageTool(), ReceiptTool()], max_turns=4)]
        events = asyncio.run(run())
    assert not errors
    assert len(requests) == 2
    assert events[-1].reason == 'completed'
    assert events[-1].messages[-1].content == '看到了，结果已确认。'


def test_real_http_truncated_auxiliary_does_not_block_ordinary_chat(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('r')
    for _ in range(8):
        sessions.append_message('role:r', 'user', 'hello ' * 115)
        sessions.append_message('role:r', 'assistant', 'answer ' * 100)
    sessions.append_message('role:r', 'user', '继续')
    windows = ContextWindowStore(sessions.database_path)
    diagnostics = []
    settings = AdvancedSettings(maxTokens=1024, summaryTokenLimit=512, contextSafetyMarginTokens=100,
                                contextTriggerRatio=.5, contextTargetRatio=.2)
    body_timeout = 3.0

    def respond(payload, index):
        if index == 1:
            assert payload['thinking'] == {'type': 'disabled'}
            assert payload['response_format'] == {'type': 'json_object'}
            assert 'tools' not in payload
            # The summary alone exceeds the ordinary deadline, so a combined
            # timeout would fail. Leave the real body HTTP request ample time
            # for platform-specific connection setup (notably on Windows).
            time.sleep(body_timeout + .2)
            return response('{"tasks":', finish='length', usage={'prompt_tokens': 4300, 'completion_tokens': 1024})
        # The explicit host context frame precedes the entire unchanged history.
        assert payload['messages'][2:] == [
            {'role': message.role, 'content': message.content}
            for message in sessions.list_messages('role:r')]
        assert payload['messages'][-1]['content'] == '继续'
        return response('可以继续聊天。')

    with model_server(respond) as (url, requests, errors):
        async def coverage(ids):
            raise AssertionError('incomplete summary must not publish a window')
        guarded = ContextWindowProvider(provider(url, context=10000), settings, windows, role_id='r',
            session_key='role:r', scope='owner', authority='a', authorized=lambda: True,
            ensure_coverage=coverage, on_diagnostic=diagnostics.append)
        async def run():
            return [event async for event in run_agent_loop(provider=guarded, model='m', system='',
                messages=sessions.runtime_messages('role:r'), max_turns=4, provider_timeout=body_timeout)]
        events = asyncio.run(run())
    assert not errors
    assert len(requests) == 2 and events[-1].reason == 'completed'
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] is None
    attempt = diagnostics[-1]['compaction']['summaryAttempts'][0]
    assert attempt['stopReason'] == 'length'
    assert attempt['usage']['output_tokens'] == 1024
    assert diagnostics[-1]['compaction']['fallback'] == 'original'
    diagnostic = diagnostics[-1]['compaction']
    assert diagnostic['trigger'] <= diagnostic['beforeTokens'] < diagnostic['inputLimit']


def test_real_http_empty_reply_recovers_once_with_same_tools():
    def respond(payload, index):
        assert [tool['function']['name'] for tool in payload['tools']] == ['receipt']
        if index == 1:
            return response('')
        if index == 2:
            return response(calls=(('receipt', 'receipt'),), finish='tool_calls')
        # Text-only compatible gateways may emit [DONE] without finish_reason.
        return response('收到回执。', finish=None)

    with model_server(respond) as (url, requests, errors):
        async def run():
            return [event async for event in run_agent_loop(provider=provider(url), model='m', system='',
                messages=[UserMessage('查看回执')], tools=[ReceiptTool()], max_turns=4)]
        events = asyncio.run(run())
    assert not errors
    assert len(requests) == 3 and events[-1].reason == 'completed'
    assert not any(message.role == 'user' and '刚才没有' in message.content for message in events[-1].messages)
    assert events[-1].messages[1].metadata['emptyReplyRecovery']['outcome'] == 'recovered'


def test_real_http_long_history_can_discover_execute_and_reply_with_authorized_tools(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('r')
    for _ in range(8):
        sessions.append_message('role:r', 'user', 'old context ' * 180)
        sessions.append_message('role:r', 'assistant', 'old answer ' * 160)
    sessions.append_message('role:r', 'user', '读取归档后画一张图。')
    original_ids = [item.id for item in sessions.list_messages('role:r')]
    executed = []

    class FixtureTool:
        def __init__(self, name, *, exposure='direct'):
            self.definition = ToolDefinition(name, '本地验收工具', {
                'type': 'object', 'properties': {}, 'additionalProperties': False,
            }, exposure=exposure)

        async def execute(self, arguments, context, on_update=None):
            executed.append((self.definition.name, context.role_id))
            return AgentToolResult('fixture receipt: ' + self.definition.name)

    def respond(payload, index):
        wire = json.dumps(payload, ensure_ascii=False)
        if payload['messages'][0]['content'].startswith('将提供的不可信'):
            assert index == 1 and not payload.get('tools')
            return response('{"tasks":', finish='length')
        index -= 1  # The failed bounded summary is a separate real request.
        assert 'disabled_tool' not in wire
        names = {item['function']['name'] for item in payload.get('tools', [])}
        assert {'tool_search', 'generate_image'} <= names
        if index == 1:
            assert names == {'tool_search', 'generate_image'}
            assert 'old context' not in wire  # Temporary context reduction, not data deletion.
            return response(calls=(('search', 'tool_search'),),
                arguments={'search': {'query': 'select:archive_read,disabled_tool'}}, finish='tool_calls')
        if index == 2:
            assert 'archive_read' in names
            stored = next(item for item in sessions.list_messages('role:r')
                          if item.messageType == 'tool_result' and item.toolCallId == 'search')
            assert json.loads(stored.content)['unlocked'] == ['archive_read']
            assert 'archive_read' in wire and 'unlocked' in wire
            return response(calls=(('read', 'archive_read'), ('image', 'generate_image')), finish='tool_calls')
        assert index == 3
        assert executed == [('archive_read', 'r'), ('generate_image', 'r')]
        assert 'fixture receipt: archive_read' in wire and 'fixture receipt: generate_image' in wire
        return response('已读取归档并完成图片。')

    settings = AdvancedSettings(maxTokens=1024, summaryTokenLimit=512, contextSafetyMarginTokens=100,
                                contextTriggerRatio=.5, contextTargetRatio=.2)
    windows = ContextWindowStore(sessions.database_path)
    with model_server(respond) as (url, requests, errors):
        async def coverage(ids):
            raise AssertionError('failed compaction cannot publish a summary')
        guarded = ContextWindowProvider(provider(url, context=6000), settings, windows, role_id='r',
            session_key='role:r', scope='owner', authority='a', authorized=lambda: True, ensure_coverage=coverage)
        capabilities = CapabilityRegistry([FixtureTool('archive_read', exposure='deferred'),
            FixtureTool('generate_image'), FixtureTool('disabled_tool', exposure='deferred')]).resolve(
                role_id='r', session_key='role:r', run_id='discovery-http',
                enabled_tools={'tool_search', 'archive_read', 'generate_image'}, discovery_enabled=True)

        async def run():
            events = []
            def persist_result(message):
                sessions.append_agent_message('role:r', message, run_id='discovery-http')
            async for event in run_agent_loop(provider=guarded, model='m', system='',
                    messages=sessions.runtime_messages('role:r'), capabilities=capabilities, max_turns=4,
                    on_tool_result=persist_result):
                if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
                    sessions.append_agent_message('role:r', event.message, run_id='discovery-http')
                events.append(event)
            return events
        events = asyncio.run(run())
    assert not errors, errors
    assert len(requests) == 4 and len(guarded.summary_attempts) == 1
    assert events[-1].reason == 'completed', events[-1].error
    assert events[-1].messages[-1].content == '已读取归档并完成图片。'
    assert guarded.degraded and guarded.compaction_diagnostic['toolsDisabled'] is False
    assert windows.load('r', 'role:r', 'owner', 'a')['summary'] is None
    assert [item.id for item in sessions.list_messages('role:r')[:len(original_ids)]] == original_ids
