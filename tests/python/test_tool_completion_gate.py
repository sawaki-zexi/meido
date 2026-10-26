"""Offline completion evidence: no live model/tool side effects."""
import asyncio
import json

import httpx
import pytest

from backend.app.agent_runtime import (
    AssistantDoneEvent, AssistantMessage, CancellationToken, MeidoProvider,
    ToolCall, ToolDefinition, ToolExecutionStartEvent, ToolResultMessage, UserMessage, run_agent_loop,
)
from backend.app.agent_runtime.events import AgentEndEvent, MessageEndEvent
from backend.app.agent_runtime.tools import AgentToolResult
from backend.app.ai.model_types import (
    ModelAssistantMessage, ModelResponseMetadata, ModelStreamDone, ModelTextBlock,
    ModelToolCallBlock,
)
from backend.app.ai.pi_openai_adapter import _ResponseAccumulator, OpenAICompletionsOptions, OpenAICompletionsRichAdapter
from backend.app.models import ModelConfiguration, RoleInput, RoleProfile


class CountingTool:
    definition = ToolDefinition('count', 'Count invocations', {'type': 'object', 'properties': {'value': {'type': 'string'}}})
    def __init__(self):
        self.calls = []
    def execute(self, arguments, context, on_update=None):
        self.calls.append(dict(arguments))
        return AgentToolResult('counted')


class RichAdapter:
    def __init__(self, message):
        self.message = message
        self.requests = 0
    async def stream_rich_messages(self, *args, **kwargs):
        self.requests += 1
        yield ModelStreamDone(self.message if self.requests == 1 else ModelAssistantMessage(
            (ModelTextBlock('finished'),), ModelResponseMetadata(stop_reason='stop')))


def provider_for(adapter):
    role = RoleInput(name='test', profile=RoleProfile(profile='test')).model_copy(update={'id': 'role-test'})
    return MeidoProvider(adapter, role, None)


async def collect(provider, tool, signal=None):
    return [event async for event in run_agent_loop(provider=provider, model='fake', system='',
        messages=[UserMessage('test')], tools=[tool], signal=signal, run_id='completion-test')]


def accumulated(arguments, reason, *, second_arguments=None):
    acc = _ResponseAccumulator({})
    calls = [{'index': 0, 'id': 'a', 'function': {'name': 'count', 'arguments': arguments}}]
    if second_arguments is not None:
        calls.append({'index': 1, 'id': 'b', 'function': {'name': 'count', 'arguments': second_arguments}})
    acc.consume({'choices': [{'delta': {'tool_calls': calls}, 'finish_reason': reason}]})
    _, message, error = acc.finish()
    assert message is not None, error
    return message


@pytest.mark.parametrize('case,arguments,reason,expected', [
    ('control_complete_tool_use', '{"value":"hello"}', 'tool_calls', 1),
    ('truncated_json_length', '{"value":"hel', 'length', 0),
    ('complete_json_but_length', '{"value":"hello"}', 'length', 0),
    ('canonical_aborted_with_tool_non_cancelled_token', None, 'aborted', 0),
    ('canonical_error_with_tool_non_cancelled_token', None, 'error', 0),
    ('control_cancelled_token', '{"value":"hello"}', 'tool_calls', 0),
])
def test_six_existing_probe_cases(case, arguments, reason, expected):
    message = accumulated(arguments, reason) if arguments is not None else ModelAssistantMessage(
        (ModelToolCallBlock('a', 'count', {'value': 'hello'}),), ModelResponseMetadata(stop_reason=reason))
    adapter = RichAdapter(message)
    tool = CountingTool()
    token = CancellationToken()
    if case == 'control_cancelled_token':
        token.cancel()
    events = asyncio.run(collect(provider_for(adapter), tool, token))
    assert len(tool.calls) == expected
    if not expected:
        assert not any(isinstance(event, ToolExecutionStartEvent) for event in events)
        assert not any(isinstance(event, MessageEndEvent) and isinstance(event.message, ToolResultMessage) for event in events)
        assert adapter.requests <= 1
        assert events[-1].reason in {'failed', 'cancelled'}


@pytest.mark.parametrize('reason', ['tool_calls', 'length'])
def test_one_incomplete_argument_rejects_entire_batch_before_any_execution(reason):
    adapter = RichAdapter(accumulated('{"value":"complete"}', reason, second_arguments='{"value":"partial'))
    tool = CountingTool()
    events = asyncio.run(collect(provider_for(adapter), tool))
    assert tool.calls == []
    assert adapter.requests == 1
    assert events[-1].reason == 'failed'
    message = next(event.message for event in events if isinstance(event, MessageEndEvent))
    assert message.metadata['tool_batch_rejected'] is True
    assert len(message.metadata['rejected_tool_calls']) == 2


@pytest.mark.parametrize('reason', [None, 'unknown', 'length', 'aborted', 'error'])
def test_rich_non_success_reason_never_becomes_tool_use(reason):
    adapter = RichAdapter(ModelAssistantMessage((ModelToolCallBlock('a', 'count', {}),), ModelResponseMetadata(stop_reason=reason)))
    tool = CountingTool()
    events = asyncio.run(collect(provider_for(adapter), tool))
    assert not tool.calls
    assert adapter.requests == 1
    assert events[-1].reason != 'completed'


def test_legacy_iterator_eof_is_only_text_completion_not_tool_completion():
    from backend.app.ai.model_adapter import ModelTextDelta, ModelToolCall
    class Legacy:
        async def stream_structured_messages(self, *args):
            yield ModelTextDelta('partial')
            yield ModelToolCall('a', 'count', {})
    tool = CountingTool()
    events = asyncio.run(collect(provider_for(Legacy()), tool))
    assert not tool.calls
    assert events[-1].reason == 'failed'


def test_legacy_explicit_semantic_completion_allows_tools():
    from backend.app.ai.model_adapter import ModelResponseDone, ModelToolCall
    class Legacy:
        requests = 0
        async def stream_structured_messages(self, *args):
            self.requests += 1
            if self.requests == 1:
                yield ModelToolCall('a', 'count', {})
                yield ModelResponseDone('tool_use')
            else:
                yield ModelResponseDone('stop')
    tool = CountingTool()
    events = asyncio.run(collect(provider_for(Legacy()), tool))
    assert tool.calls == [{}]
    assert events[-1].reason == 'completed'


def test_legacy_text_only_eof_remains_compatible():
    from backend.app.ai.model_adapter import ModelTextDelta
    class Legacy:
        async def stream_structured_messages(self, *args):
            yield ModelTextDelta('unchanged')
    events = asyncio.run(collect(provider_for(Legacy()), CountingTool()))
    assert events[-1].reason == 'completed'
    assert events[-1].messages[-1].content == 'unchanged'


@pytest.mark.parametrize('complete_message,done,reason,expected', [
    (False, True, None, 0), (True, False, None, 0), (True, True, None, 1),
    (True, True, 'length', 0), (True, True, 'tool_calls', 1),
])
def test_no_finish_reason_compatibility_needs_complete_message_and_transport_done(monkeypatch, complete_message, done, reason, expected):
    options = OpenAICompletionsOptions(supports_finish_reason=False, tool_completion_contract='complete_message')
    choice = {'message' if complete_message else 'delta': {'tool_calls': [{'id': 'a', 'function': {'name': 'count', 'arguments': '{}'}}]}}
    if reason:
        choice['finish_reason'] = reason
    body = 'data: ' + json.dumps({'choices': [choice]}) + '\n\n' + ('data: [DONE]\n\n' if done else '')
    original = httpx.AsyncClient
    def handler(request):
        return httpx.Response(200, text=body, headers={'content-type': 'text/event-stream'}, request=request)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    configuration = ModelConfiguration(providerId='test', provider='test', baseUrl='https://example.test', model='test', apiKey='')
    class Adapter:
        requests = 0
        async def stream_rich_messages(self, messages, config, tools):
            self.requests += 1
            if self.requests > 1:
                yield ModelStreamDone(ModelAssistantMessage(metadata=ModelResponseMetadata(stop_reason='stop')))
            else:
                async for event in OpenAICompletionsRichAdapter(options=options).stream_messages(messages, configuration, tools):
                    yield event
    tool = CountingTool()
    asyncio.run(collect(provider_for(Adapter()), tool))
    assert len(tool.calls) == expected


def test_spawn_child_uses_same_completion_gate(tmp_path):
    from test_spawn import delegate, completed
    class Provider:
        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('write', 'write_file', {'path': 'must-not-exist', 'content': 'unsafe'}),), stop_reason='length'))
    async def exercise():
        tasks, context, _, _, result = await delegate(tmp_path, Provider(), profile='scripting')
        try:
            snapshot = await completed(tasks, context, result.details['taskId'])
            assert snapshot.status == 'failed'
            assert not (tasks.root / 'workspaces' / snapshot.task_id / 'must-not-exist').exists()
        finally:
            tasks.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('api', [
    'openai-completions', 'openai-responses', 'azure-openai-responses', 'openai-codex-responses',
    'anthropic-messages', 'google-generative', 'google-vertex', 'mistral-conversations',
    'bedrock-converse', 'pi-messages',
])
@pytest.mark.parametrize('ending', ['complete', 'length', 'missing', 'unknown', 'partial', 'malformed'])
def test_registered_protocols_require_semantic_and_parameter_evidence(monkeypatch, api, ending):
    import struct
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry
    malformed = ending == 'malformed'
    if malformed:
        ending = 'complete'
    args = '{"value":"part' if ending == 'partial' else '{}'
    normal = ending in {'complete', 'partial'}
    reason = 'tool_use' if normal else 'length' if ending == 'length' else 'unknown'
    terminal = ending != 'missing'
    def sse(values):
        return ''.join('data: ' + (value if isinstance(value, str) else json.dumps(value)) + '\n\n' for value in values)
    if api in {'openai-completions', 'mistral-conversations'}:
        choice = {'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': 'count', 'arguments': args}}]}}
        if terminal:
            choice['finish_reason'] = 'tool_calls' if normal else reason
        body = sse([{'choices': [choice]}, '[DONE]'])
    elif api in {'openai-responses', 'azure-openai-responses', 'openai-codex-responses'}:
        body = sse([{'type': 'response.completed', 'response': {'status': 'completed' if normal else reason,
            'output': [{'type': 'function_call', 'call_id': 'a', 'name': 'count', 'arguments': args}]}}]) if terminal else sse([
            {'type': 'response.output_item.added', 'item': {'type': 'function_call', 'call_id': 'a', 'name': 'count', 'arguments': args}}])
    elif api == 'anthropic-messages':
        values = [
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'a', 'name': 'count', 'input': {}}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': args}},
            {'type': 'content_block_stop', 'index': 0},
        ]
        if terminal:
            values += [{'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens' if ending == 'length' else reason}}, {'type': 'message_stop'}]
        body = sse(values)
    elif api in {'google-generative', 'google-vertex'}:
        candidate = {'content': {'parts': [{'functionCall': {'id': 'a', 'name': 'count', 'args': args if ending == 'partial' else {}}}]}}
        if terminal:
            candidate['finishReason'] = 'STOP' if normal else 'MAX_TOKENS' if ending == 'length' else 'UNKNOWN'
        body = sse([{'candidates': [candidate]}])
    elif api == 'bedrock-converse':
        values = [{'contentBlockStart': {'contentBlockIndex': 0, 'start': {'toolUse': {'toolUseId': 'a', 'name': 'count'}}}},
            {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'toolUse': {'input': args}}}}]
        if terminal:
            values.append({'messageStop': {'stopReason': 'max_tokens' if ending == 'length' else reason}})
        def frame(value):
            encoded = json.dumps(value).encode()
            return struct.pack('>III', 16 + len(encoded), 0, 0) + encoded + struct.pack('>I', 0)
        body = b''.join(frame(value) for value in values)
    else:
        values = [{'type': 'toolcall_end', 'contentIndex': 0, 'toolCall': {'id': 'a', 'name': 'count', 'arguments': args if ending == 'partial' else {}}}]
        if terminal:
            values.append({'type': 'done', 'reason': reason})
        body = sse(values)
    if malformed:
        if isinstance(body, bytes):
            payload = b'{malformed'
            body += struct.pack('>III', 16 + len(payload), 0, 0) + payload + struct.pack('>I', 0)
        elif 'data: [DONE]' in body:
            body = body.replace('data: [DONE]', 'data: {malformed\n\ndata: [DONE]')
        else:
            body += 'data: {malformed\n\n'
    original = httpx.AsyncClient
    def handler(request):
        return httpx.Response(200, content=body.encode() if isinstance(body, str) else body,
                              headers={'content-type': 'text/event-stream'}, request=request)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    configuration = ModelConfiguration(providerId='test', provider='test', api=api, baseUrl='https://example.test', model='test', apiKey='test', apiOptions={'project':'offline', 'accountId':'offline'})
    adapter = ModelAdapterRegistry().get(configuration)
    class Routed:
        requests = 0
        async def stream_rich_messages(self, messages, config, tools):
            self.requests += 1
            if self.requests > 1:
                yield ModelStreamDone(ModelAssistantMessage(metadata=ModelResponseMetadata(stop_reason='stop')))
                return
            stream = getattr(adapter, "stream_rich_messages", adapter.stream_messages)
            async for event in stream(messages, configuration, tools):
                yield event
    tool = CountingTool()
    events = asyncio.run(collect(provider_for(Routed()), tool))
    assert len(tool.calls) == (1 if ending == 'complete' and not malformed else 0), (events[-1].error, events[-1].messages)


def test_prior_completed_tool_receipt_survives_later_rejected_batch():
    class Provider:
        requests = 0
        async def stream_response(self, **kwargs):
            self.requests += 1
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall(str(self.requests), 'count', {}),),
                stop_reason='tool_use' if self.requests == 1 else 'length'))
    tool = CountingTool()
    events = asyncio.run(collect(Provider(), tool))
    assert tool.calls == [{}]
    assert events[-1].reason == 'failed'
    results = [message for message in events[-1].messages if isinstance(message, ToolResultMessage)]
    assert len(results) == 1 and not results[0].is_error
    assert results[0].tool_call_id == '1'


def test_http_rejected_batch_is_failed_and_persists_reason_without_execution(tmp_path, monkeypatch, local_platform_only):
    from fastapi.testclient import TestClient
    from backend.app import main
    from backend.app.memory_config import MemorySettingsStore
    from backend.app.model_config import ModelConfigurationStore
    from backend.app.role_store import RoleStore
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    roles = RoleStore(tmp_path / 'roles')
    role = roles.create(RoleInput(name='test', profile=RoleProfile(profile='test')))
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data' / 'sessions.db')
    adapter = RichAdapter(accumulated('{"value":"partial', 'length'))
    monkeypatch.setattr(main, 'store', roles)
    monkeypatch.setattr(main, 'session_store', sessions)
    monkeypatch.setattr(main, 'session_manager', main.SessionManager(roles, sessions))
    monkeypatch.setattr(main, 'model_adapter', adapter)
    monkeypatch.setattr(main, 'model_configuration_store', ModelConfigurationStore(tmp_path / 'model.json'))
    monkeypatch.setattr(main, 'role_locks', {})
    memory = MemorySettingsStore(tmp_path / 'memory.json')
    memory.update(enabled=False)
    monkeypatch.setattr(main, 'memory_settings_store', memory)
    response = TestClient(main.app).post(f'/api/roles/{role.id}/messages', json={'content': 'test'})
    assert response.status_code == 200
    assert 'event: assistant_failed' in response.text
    assert '回复未完成，本批工具未执行' in response.text
    assert 'tool_execution_start' not in response.text
    assert 'assistant_completed' not in response.text
    stored = sessions.list_messages(f'role:{role.id}')
    assert stored[-1].status == 'failed'
    assert stored[-1].metadata['runtimeStopReason'] == 'length'
    assert stored[-1].metadata['provider_stop_reason'] == 'length'
    assert stored[-1].metadata['tool_batch_rejected'] is True
    assert not any(item.messageType in {'tool_call', 'tool_result'} for item in stored)


def test_incomplete_stop_reason_cannot_be_overwritten_by_later_normal_finish():
    accumulator = _ResponseAccumulator({})
    accumulator.consume({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': 'count', 'arguments': '{}'}}]}, 'finish_reason': 'length'}]})
    accumulator.consume({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]})
    _, message, error = accumulator.finish()
    assert message is not None, error
    tool = CountingTool()
    asyncio.run(collect(provider_for(RichAdapter(message)), tool))
    assert tool.calls == []
    assert message.metadata.stop_reason == 'length'


@pytest.mark.parametrize("invalid_tail", ["unfinished", "duplicate_end"])
def test_pi_unfinished_second_call_rejects_even_completed_first_call(monkeypatch, invalid_tail):
    from backend.app.ai.pi_messages_adapter import PiMessagesAdapter
    events = [
        {'type': 'toolcall_end', 'contentIndex': 0, 'toolCall': {'id': 'a', 'name': 'count', 'arguments': {}}},
        {'type': 'toolcall_start', 'contentIndex': 1, 'id': 'b', 'toolName': 'count'},
        {'type': 'toolcall_delta', 'contentIndex': 1, 'delta': '{"value":"incomplete'},
        {'type': 'done', 'reason': 'toolUse'},
    ]
    if invalid_tail == 'duplicate_end':
        events = [events[0], events[0], events[-1]]
    body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=body, request=request))))
    configuration = ModelConfiguration(providerId='test', provider='test', api='pi-messages', baseUrl='https://example.test', model='test', apiKey='')
    class Adapter:
        requests = 0
        async def stream_rich_messages(self, messages, config, tools):
            self.requests += 1
            if self.requests > 1:
                yield ModelStreamDone(ModelAssistantMessage(metadata=ModelResponseMetadata(stop_reason="stop")))
                return
            async for event in PiMessagesAdapter().stream_messages(messages, configuration, tools):
                yield event
    tool = CountingTool()
    result = asyncio.run(collect(provider_for(Adapter()), tool))
    assert not tool.calls
    assert result[-1].reason == 'failed'


@pytest.mark.parametrize('api,body', [
    ('anthropic-messages', [
        {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'a','name':'count','input':{}}},
        {'type':'content_block_stop','index':0}, {'type':'message_delta','delta':{'stop_reason':'tool_use'}},
    ]),
    ('openai-responses', [
        {'type':'response.output_item.done','output_index':0,'item':{'type':'function_call','call_id':'a','name':'count','arguments':'{}'}},
    ]),
])
def test_transport_done_does_not_replace_native_semantic_terminal(monkeypatch, api, body):
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry
    wire = ''.join('data: '+json.dumps(event)+'\n\n' for event in body) + 'data: [DONE]\n\n'
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=wire, request=request))))
    configuration = ModelConfiguration(providerId='test', provider='test', api=api, baseUrl='https://example.test', model='test', apiKey='')
    class Adapter:
        async def stream_rich_messages(self, messages, config, tools):
            async for event in ModelAdapterRegistry().get(configuration).stream_messages(messages, configuration, tools):
                yield event
    tool = CountingTool()
    events = asyncio.run(collect(provider_for(Adapter()), tool))
    assert not tool.calls
    assert events[-1].reason == 'failed'


def test_rich_text_error_preserves_provider_error_detail():
    adapter = RichAdapter(ModelAssistantMessage((ModelTextBlock('partial'),),
        ModelResponseMetadata(stop_reason='error', error_message='sanitized provider failure')))
    events = asyncio.run(collect(provider_for(adapter), CountingTool()))
    assert events[-1].reason == 'failed'
    assert events[-1].error == 'sanitized provider failure'
