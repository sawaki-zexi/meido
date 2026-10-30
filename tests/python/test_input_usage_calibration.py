"""Request usage drives the next ordinary request, using controlled HTTP only."""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.provider import AssistantDoneEvent, CancellationToken
from backend.app.agent_runtime.types import AssistantMessage, UserMessage
from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.models import ModelConfiguration, Role
from backend.app.agent_runtime.usage_anchor import UsageAnchors
from test_model_image_request_budget import local_model  # noqa: F401


def make_provider(**kwargs):
    now = datetime.now(timezone.utc)
    role = Role(id='r', name='R', profile={'profile': '助手'}, createdAt=now, updatedAt=now)
    config = ModelConfiguration(id='connection', providerId='custom', provider='fixture',
        baseUrl='https://provider.test', model='fixture', contextWindowTokens=32000)
    return MeidoProvider(RoutedModelAdapter(ModelAdapterRegistry()), role, config,
        max_response_tokens=1024, **kwargs)


def patch_openai(monkeypatch, *, usage=123):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        chunk = {'choices': [{'delta': {'content': 'reply'}, 'finish_reason': 'stop'}]}
        if usage is not None:
            chunk['usage'] = {'prompt_tokens': usage, 'completion_tokens': 7}
        return httpx.Response(200, text=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    return requests


async def respond(provider, messages, tools=(), *, signal=None):
    events = [event async for event in provider.stream_response(model='fixture', system='',
        messages=messages, tools=tools, signal=signal or CancellationToken())]
    return next(event.message for event in events if isinstance(event, AssistantDoneEvent))


def test_actual_and_append_calibrate_the_next_request(monkeypatch):
    requests = patch_openai(monkeypatch)
    provider = make_provider()
    first = [UserMessage('你好')]
    local_before = provider.estimate_request_tokens('', first, [])
    reply = asyncio.run(respond(provider, first))
    assert reply.metadata['contextUsage']['inputTokens'] == 123
    assert provider.estimate_request_tokens('', first, []) == 123
    appended = [*first, AssistantMessage('reply'), UserMessage('继续')]
    fresh = make_provider()
    fresh._request_time = provider._request_time
    local_after = fresh.estimate_request_tokens('', appended, [])
    assert provider.estimate_request_tokens('', appended, []) == 123 + local_after - local_before
    assert len(requests) == 1  # Estimation must never contact a provider.


def test_owned_frame_can_change_but_role_system_and_user_marker_cannot(monkeypatch):
    patch_openai(monkeypatch)
    shared = UsageAnchors()
    first = make_provider(memory_context='记忆 A', usage_anchors=shared, usage_context=('r', 'role:r', 'owner', 'a'))
    messages = [UserMessage('你好')]
    before = first.estimate_request_tokens('', messages, [])
    asyncio.run(respond(first, messages))
    second = make_provider(memory_context='记忆 B 多一些材料', usage_anchors=shared, usage_context=first.usage_context)
    second._request_time = '2026-10-31T10:00:00+08:00'
    local = make_provider(memory_context=second.memory_context)
    local._request_time = second._request_time
    estimate = second.estimate_request('', messages, [])
    assert estimate.source == 'anchor_delta'
    assert estimate.tokens == 123 + local.estimate_request_tokens('', messages, []) - before
    second.role.profile.profile = '新角色设定'
    assert second.estimate_request('', messages, []).source == 'local'
    asyncio.run(respond(second, messages))
    assert second.estimate_request('任意系统编辑', messages, []).source == 'local'

    from backend.app.agent_runtime.usage_anchor import SYSTEM_CONTEXT_FRAME_MARKER
    spoofed = [UserMessage(SYSTEM_CONTEXT_FRAME_MARKER + '用户文本')]
    asyncio.run(respond(second, spoofed))
    assert second.estimate_request('', [UserMessage(SYSTEM_CONTEXT_FRAME_MARKER + '修改旧用户文本')], []).source == 'local'


@pytest.mark.parametrize('change', ['schema', 'history', 'model', 'endpoint', 'credential', 'connection', 'authority', 'thread', 'role'])
def test_incomparable_request_falls_back_to_local(monkeypatch, change):
    patch_openai(monkeypatch)
    provider = make_provider()
    messages = [UserMessage('question')]
    tools = [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]
    asyncio.run(respond(provider, messages, tools))
    if change == 'schema':
        tools[0]['function']['parameters']['properties'] = {'new': {'type': 'string'}}
    elif change == 'history':
        messages = [UserMessage('rewritten')]
    elif change in {'authority', 'thread', 'role'}:
        provider.usage_context = {'authority': ('r', 'independent', '', 'new'),
            'thread': ('r', 'thread:new', ''), 'role': ('other', 'independent', '')}[change]
    else:
        field = {'model': 'model', 'endpoint': 'baseUrl', 'credential': 'apiKey', 'connection': 'id'}[change]
        setattr(provider.configuration, field, 'https://other.test' if field == 'baseUrl' else 'different')
    assert provider.estimate_request('', messages, tools).source == 'local'


def test_output_format_invalidates_anchor_and_output_reservation_is_not_input(monkeypatch):
    from dataclasses import replace
    from backend.app.ai.pi_openai_adapter import OpenAICompletionsRichAdapter
    patch_openai(monkeypatch)
    provider = make_provider()
    provider.adapter = OpenAICompletionsRichAdapter()
    messages = [UserMessage('question')]
    asyncio.run(respond(provider, messages))
    provider.max_response_tokens = 2000
    assert provider.estimate_request('', messages, []).source == 'actual'
    provider.adapter.options = replace(provider.adapter.options, json_mode=True)
    assert provider.estimate_request('', messages, []).source == 'local'


def test_missing_usage_keeps_append_estimate_and_unknown_capacity(monkeypatch):
    requests = patch_openai(monkeypatch)
    provider = make_provider()
    first = [UserMessage('question')]
    asyncio.run(respond(provider, first))
    # Replace the responder while retaining the same real adapter/connection.
    # The original patched factory already owns its transport, so restore it.
    monkeypatch.undo()
    patch_openai(monkeypatch, usage=None)
    provider.configuration.contextWindowTokens = None
    appended = [*first, AssistantMessage('reply'), UserMessage('continue')]
    expected = provider.estimate_request('', appended, [])
    reply = asyncio.run(respond(provider, appended))
    assert reply.metadata['contextUsage']['source'] == 'anchor_delta'
    assert reply.metadata['contextUsage']['inputTokens'] == expected.tokens
    assert reply.metadata['contextUsage']['capacity'] is None
    assert len(requests) == 1


@pytest.mark.parametrize('latest_usage', [101, None])
def test_latest_matching_response_wins_even_when_it_has_no_usage(monkeypatch, latest_usage):
    original = httpx.AsyncClient
    async def scenario():
        started = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0
        async def handler(request):
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            await release[index].wait()
            payload = {'choices': [{'delta': {'content': 'reply'}, 'finish_reason': 'stop'}]}
            if index == 0 or latest_usage is not None:
                payload['usage'] = {'prompt_tokens': 100 if index == 0 else latest_usage, 'completion_tokens': 1}
            return httpx.Response(200, text=f'data: {json.dumps(payload)}\n\ndata: [DONE]\n\n')
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
        shared = UsageAnchors()
        first, second = make_provider(usage_anchors=shared), make_provider(usage_anchors=shared)
        first._request_time = second._request_time
        messages = [UserMessage('same input')]
        older = asyncio.create_task(respond(first, messages))
        await started[0].wait()
        newer = asyncio.create_task(respond(second, messages))
        await started[1].wait()
        release[1].set()
        await newer
        release[0].set()
        await older
        if latest_usage is not None:
            assert first.estimate_request('', messages, []).tokens == latest_usage
            assert first.estimate_request('', messages, []).source == 'actual'
        else:
            assert first.estimate_request('', messages, []).source == 'local'

        # A request invalidated by a new source view cannot revive its anchor.
        ticket = shared.begin({'messages': []}, ('r', 'role:r', 'owner', 'old'))
        shared.estimate({'messages': []}, ('r', 'role:r', 'owner', 'new'))
        shared.finish(ticket, 999)
        assert shared.estimate({'messages': []}, ('r', 'role:r', 'owner', 'new')).source == 'local'
    asyncio.run(scenario())


def test_anchor_store_evicts_old_context_and_its_pending_response():
    store = UsageAnchors()
    request = {'messages': [{'role': 'user', 'content': 'x'}]}
    pending = store.begin(request, ('r', 'old', 'owner'))
    for index in range(64):
        key = ('r', str(index), 'owner')
        store.finish(store.begin(request, key), index + 100)
    store.finish(pending, 999)
    assert store.estimate(request, ('r', 'old', 'owner')).source == 'local'
    assert store.estimate(request, ('r', '63', 'owner')).tokens == 163


@pytest.mark.parametrize('api', ['openai-completions', 'openai-responses', 'azure-openai-responses',
    'openai-codex-responses', 'anthropic-messages', 'google-generative', 'google-vertex',
    'bedrock-converse', 'mistral-conversations', 'pi-messages'])
def test_every_protocol_prepares_real_images_schema_and_repaired_history_without_io(monkeypatch, api):
    from backend.app.agent_runtime.types import AttachmentContent, ImageContent, ToolCall
    def forbidden(*args, **kwargs):
        raise AssertionError('Estimation performed IO')
    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    provider = make_provider()
    provider.configuration = provider.configuration.model_copy(update={'api': api, 'apiKey': 'test',
        'apiOptions': {'accountId': 'account', 'project': 'project'}, 'model': 'claude-vision' if api == 'bedrock-converse' else 'pixtral-large'})
    def image(data):
        return AttachmentContent('img', 'image/png', image=ImageContent('image/png', data, 'high'))
    messages = [UserMessage('picture', attachments=(image('cGljdHVyZQ=='),)),
        AssistantMessage(tool_calls=(ToolCall('read', 'archive.read', {}),)),
        UserMessage('after missing result')]
    tools = [{'type': 'function', 'function': {'name': 'archive.read', 'description': 'lookup',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}}]
    estimate = provider.estimate_request('', messages, tools)
    assert estimate.tokens >= 4096
    messages[0] = UserMessage('picture', attachments=(image('eA==' * 10000),))
    assert provider.estimate_request_tokens('', messages, tools) == estimate.tokens
    prepared = provider.prepare_request('', messages, tools)
    request = provider._input_request(prepared.messages, prepared.tools,
        configuration=prepared.configuration, max_tokens=prepared.max_tokens)
    assert 'archive' in str(request) and 'No result provided' in str(request)
    assert 'apiKey' not in request and 'test' not in request['_connection']
    assert request.get('_context_frames') == (1,)


@pytest.mark.parametrize('api', ['openai-completions', 'openai-responses', 'azure-openai-responses', 'openai-codex-responses'])
def test_provider_image_estimate_honors_native_low_detail(api):
    from backend.app.agent_runtime.types import AttachmentContent, ImageContent
    provider = make_provider()
    provider.configuration = provider.configuration.model_copy(update={'api': api, 'apiOptions': {'accountId': 'fixture'}})
    estimates = {}
    for detail in ('low', 'high', 'auto'):
        messages = [UserMessage('picture', attachments=(AttachmentContent('img', 'image/png',
            image=ImageContent('image/png', 'eA==', detail)),))]
        estimates[detail] = provider.estimate_request_tokens('', messages, [])
    assert estimates['high'] - estimates['low'] == 4096 - 85
    assert estimates['auto'] == estimates['high']


@pytest.mark.parametrize('image', [
    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,eA=='}},
    {'type': 'input_image', 'image_url': 'data:image/png;base64,eA=='},
])
def test_images_without_explicit_detail_keep_conservative_allowance(image):
    from backend.app.agent_runtime.usage_anchor import estimate_tokens
    assert estimate_tokens(image) == 4096


def test_http_wire_uses_same_aliased_tools_and_multimodal_projection(monkeypatch):
    from backend.app.agent_runtime.types import AttachmentContent, ImageContent, ToolCall
    requests = patch_openai(monkeypatch)
    provider = make_provider()
    messages = [UserMessage('read image', attachments=(AttachmentContent('img', 'image/png', image=ImageContent('image/png', 'eA==')),)),
        AssistantMessage(tool_calls=(ToolCall('call', 'archive.read', {}),)), UserMessage('continue')]
    tools = [{'type': 'function', 'function': {'name': 'archive.read', 'parameters': {'type': 'object'}}}]
    prepared = provider.prepare_request('', messages, tools)
    provider.estimate_prepared(prepared)
    projected = provider._input_request(prepared.messages, prepared.tools,
        configuration=prepared.configuration, max_tokens=prepared.max_tokens)
    asyncio.run(respond(provider, messages, tools))
    assert projected['messages'] == requests[0]['messages']
    assert projected['tools'] == requests[0]['tools']
    assert requests[0]['tools'][0]['function']['name'] != 'archive.read'


def make_window(provider, tmp_path, *, settings=None):
    from backend.app.advanced_settings import AdvancedSettings
    from backend.app.agent_runtime.context_provider import ContextWindowProvider
    from backend.app.context_windows import ContextWindowStore
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('r')
    async def coverage(ids):
        return True
    return ContextWindowProvider(provider, settings or AdvancedSettings(maxTokens=1024, summaryTokenLimit=512,
        contextSafetyMarginTokens=100, contextTriggerRatio=.5, contextTargetRatio=.2),
        ContextWindowStore(sessions.database_path), role_id='r', session_key='role:r', scope='owner',
        authority='a', authorized=lambda: True, ensure_coverage=coverage)


@pytest.mark.parametrize('api', ['openai-responses', 'azure-openai-responses', 'openai-codex-responses'])
def test_responses_low_detail_images_fit_initial_and_appended_request_budget(monkeypatch, tmp_path, api):
    from backend.app.agent_runtime.types import AttachmentContent, ImageContent
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        payload = {'response': {'id': 'response', 'status': 'completed',
            'usage': {'input_tokens': 200, 'output_tokens': 3},
            'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'reply'}]}]}}
        return httpx.Response(200, text=f'event: response.completed\ndata: {json.dumps(payload)}\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    provider = make_provider(context_safety_margin=100)
    provider.configuration = provider.configuration.model_copy(update={'api': api, 'contextWindowTokens': 4000,
        'apiOptions': {'accountId': 'fixture'}})
    window = make_window(provider, tmp_path)
    image = AttachmentContent('img', 'image/png', image=ImageContent('image/png', 'eA==', 'low'))
    messages = [UserMessage('picture', attachments=(image,))]
    asyncio.run(respond(window, messages))
    assert window.last_input_source == 'local'
    messages += [AssistantMessage('reply'), UserMessage('second picture', attachments=(image,))]
    asyncio.run(respond(window, messages))
    assert window.last_input_source == 'anchor_delta'
    assert window.last_input_estimate < 1000
    assert not window.compaction_failed and not window.degraded and window.compactions == 0
    assert len(requests) == 2
    images = [part for message in requests[-1]['input'] for part in message.get('content', [])
        if isinstance(part, dict) and part.get('type') == 'input_image']
    assert len(images) == 2
    assert all(part['detail'] == 'low' and isinstance(part['image_url'], str) for part in images)


def test_actual_usage_avoids_unnecessary_compaction_and_counts_optional_context(monkeypatch, tmp_path):
    requests = patch_openai(monkeypatch, usage=123)
    provider = make_provider(memory_context='长期记忆 ' * 800)
    window = make_window(provider, tmp_path)
    messages = [UserMessage('继续')]
    local = provider.estimate_request_tokens('', messages, [])
    asyncio.run(respond(provider, messages))
    provider.configuration.contextWindowTokens = 4000
    provider.context_blocks = lambda: ['可选活动 ' * 80]
    asyncio.run(respond(window, messages))
    assert local > provider.configuration.contextWindowTokens
    assert window.compactions == 0 and not window.compaction_failed and not window.degraded
    assert window.last_input_source == 'anchor_delta'
    assert window.last_input_estimate < 2000
    assert '可选活动' in str(requests[-1]['messages'])
    assert requests[-1]['max_tokens'] == 1024


def test_actual_usage_crosses_hard_limit_with_real_output_reserve_and_safety(monkeypatch, tmp_path):
    requests = patch_openai(monkeypatch, usage=10000)
    provider = make_provider()
    window = make_window(provider, tmp_path)
    messages = [UserMessage('当前输入')]
    local = provider.estimate_request_tokens('', messages, [])
    asyncio.run(respond(provider, messages))
    # Local alone fits. Actual input does not; even minimal rendering must fit
    # capacity minus the output cap and safety margin before any HTTP request.
    provider.configuration.contextWindowTokens = local + 1024 + 100 + 50
    with pytest.raises(ValueError, match='输入容量'):
        asyncio.run(respond(window, messages))
    assert window.compaction_failed and window.degraded
    assert len(requests) == 1


def test_summary_and_one_off_child_do_not_replace_chat_anchor(monkeypatch, tmp_path):
    patch_openai(monkeypatch, usage=123)
    parent = make_provider()
    window = make_window(parent, tmp_path)
    messages = [UserMessage('parent context')]
    asyncio.run(respond(parent, messages))
    asyncio.run(window._summarize({'messages': ['unrelated prefix']}, CancellationToken()))
    assert parent.estimate_request('', messages, []).source == 'actual'
    assert parent.estimate_request_tokens('', messages, []) == 123
    child = make_provider()
    asyncio.run(respond(child, [UserMessage('independent task')]))
    assert parent.estimate_request('', messages, []).source == 'actual'
    assert child.usage_anchors is not parent.usage_anchors


@pytest.mark.parametrize('api', ['openai-completions', 'openai-responses', 'anthropic-messages',
    'google-generative', 'google-vertex', 'bedrock-converse', 'mistral-conversations', 'pi-messages'])
def test_native_http_usage_includes_cached_input_exactly_once(monkeypatch, api):
    import struct
    def frame(payload):
        encoded = json.dumps(payload).encode()
        return struct.pack('>III', 16 + len(encoded), 0, 0) + encoded + struct.pack('>I', 0)
    def handler(request):
        if api == 'anthropic-messages':
            body = ''.join([
                'event: message_start\ndata: {"message":{"id":"msg","usage":{"input_tokens":40,"cache_read_input_tokens":50,"cache_creation_input_tokens":10}}}\n\n',
                'event: content_block_start\ndata: {"index":0,"content_block":{"type":"text","text":"reply"}}\n\n',
                'event: content_block_stop\ndata: {"index":0}\n\n',
                'event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n',
                'event: message_stop\ndata: {}\n\n'])
        elif api.startswith('google'):
            body = 'data: {"candidates":[{"content":{"parts":[{"text":"reply"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":100,"cachedContentTokenCount":60,"candidatesTokenCount":3}}\n\n'
        elif api == 'bedrock-converse':
            body = b''.join([frame({'messageStart': {'role': 'assistant'}}),
                frame({'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'text': 'reply'}}}),
                frame({'messageStop': {'stopReason': 'end_turn'}}),
                frame({'metadata': {'usage': {'inputTokens': 40, 'outputTokens': 3, 'cacheReadInputTokens': 50, 'cacheWriteInputTokens': 10}}})])
            return httpx.Response(200, content=body)
        elif api == 'pi-messages':
            body = 'data: {"type":"text_delta","contentIndex":0,"delta":"reply"}\n\ndata: {"type":"done","reason":"stop","usage":{"input":40,"output":3,"cacheRead":50,"cacheWrite":10}}\n\n'
        elif api == 'openai-responses':
            payload = {'response': {'id': 'response', 'status': 'completed', 'usage': {'input_tokens': 100,
                'input_tokens_details': {'cached_tokens': 60}, 'output_tokens': 3},
                'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'reply'}]}]}}
            body = f'event: response.completed\ndata: {json.dumps(payload)}\n\n'
        else:
            payload = {'choices': [{'delta': {'content': 'reply'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 60}, 'completion_tokens': 3}}
            body = f'data: {json.dumps(payload)}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    provider = make_provider()
    provider.configuration = provider.configuration.model_copy(update={'api': api, 'apiKey': 'test', 'apiOptions': {'project': 'fixture'}})
    messages = [UserMessage('cached request')]
    reply = asyncio.run(respond(provider, messages))
    assert reply.metadata['contextUsage']['inputTokens'] == 100
    assert reply.metadata['contextUsage']['source'] == 'actual'
    assert provider.estimate_request_tokens('', messages, []) == 100
    # Billing fields retain the adapter's own contract, not a synthetic total.
    if api.startswith('google'):
        assert reply.metadata['usage']['input_tokens'] == 40


@pytest.mark.parametrize('outcome', ['absent', 'error', 'cancelled'])
def test_missing_failed_and_cancelled_http_responses_do_not_create_usage_anchor(monkeypatch, outcome):
    from backend.app.agent_runtime.provider import ProviderErrorEvent
    signal = CancellationToken()
    def handler(request):
        if outcome == 'error':
            return httpx.Response(500, json={'error': {'message': 'fixture failure'}})
        if outcome == 'cancelled':
            signal.cancel()
        body = {'choices': [{'delta': {'content': 'reply'}, 'finish_reason': 'stop'}]}
        if outcome == 'cancelled':
            body['usage'] = {'prompt_tokens': 999}
        return httpx.Response(200, text=f'data: {json.dumps(body)}\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    provider = make_provider()
    messages = [UserMessage('request')]
    async def collect():
        return [event async for event in provider.stream_response(model='fixture', system='', messages=messages, tools=[], signal=signal)]
    if outcome == 'cancelled':
        with pytest.raises(RuntimeError, match='cancelled'):
            asyncio.run(collect())
    else:
        events = asyncio.run(collect())
        assert any(isinstance(event, ProviderErrorEvent if outcome == 'error' else AssistantDoneEvent) for event in events)
    assert provider.estimate_request('', messages, []).source == 'local'


@pytest.mark.parametrize('natural', [False, True])
def test_chat_api_reuses_actual_usage_across_turns_and_reports_each_source(tmp_path, monkeypatch, local_model, natural):  # noqa: F811
    from backend.app import main
    from backend.app.advanced_settings import AdvancedSettingsStore
    from backend.app.models import ModelConfigurationInput, RoleUpdateInput
    from test_api import configure_session_api
    adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=3))
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, adapter)
    monkeypatch.setattr(main, 'advanced_settings', AdvancedSettingsStore(tmp_path / 'advanced.db'))
    monkeypatch.setattr(main, '_ordinary_usage_anchors', UsageAnchors())
    activity = ['本轮外部活动 A']
    monkeypatch.setattr(main, '_external_activity_blocks', lambda role_id: activity)
    main.model_configuration_store.save(ModelConfigurationInput(providerId='custom', provider='fixture',
        baseUrl=local_model.url, model='fixture', apiKey='synthetic', contextWindowTokens=131072))
    main.store.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
        agentConfig=role.agentConfig.model_copy(update={'naturalChatEnabled': natural})))
    def response(payload):
        first = len(local_model.bodies) == 1
        if natural:
            delta = {'tool_calls': [{'index': 0, 'id': 'finish', 'type': 'function', 'function': {
                'name': 'finish_reply', 'arguments': json.dumps({'messages': ['reply']})}}]}
        else:
            delta = {'content': 'reply'}
        chunk = {'choices': [{'delta': delta, 'finish_reason': 'tool_calls' if natural else 'stop'}]}
        if first:
            chunk['usage'] = {'prompt_tokens': 200, 'completion_tokens': 5}
        return f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n'.encode()
    local_model.response = response
    for text, expected in [('first question', 'actual'), ('next question', 'anchor_delta'), ('changed system', 'local')]:
        if expected == 'anchor_delta':
            activity[:] = ['本轮外部活动 B，新的检索资料']
        elif expected == 'local':
            current = main.store.get(role.id)
            main.store.update(role.id, RoleUpdateInput(name=current.name,
                profile=current.profile.model_copy(update={'profile': '编辑后的角色设定'})))
        result = client.post(f'/api/roles/{role.id}/messages', json={'content': text})
        assert 'event: assistant_completed' in result.text, result.text
        rows = sessions.list_messages(f'role:{role.id}')
        reply = next(row for row in reversed(rows) if row.role == 'assistant')
        assert reply.metadata['contextUsage']['source'] == expected
        run = sessions.get_run(reply.runId)
        assert run.modelSnapshot['budgetDiagnostic']['inputSource'] == ('local' if expected == 'actual' else expected)
        if expected == 'anchor_delta':
            assert reply.metadata['contextUsage']['inputTokens'] < 2000
    assert len(local_model.bodies) == 3
    schemas = [{tool['function']['name'] for tool in json.loads(body)['tools']} for body in local_model.bodies]
    assert all(('finish_reply' in names) is natural for names in schemas)
    assert '本轮外部活动 B' in local_model.bodies[1].decode()
    assert not any('本轮外部活动' in row.content for row in sessions.list_messages(f'role:{role.id}'))


def test_natural_correction_counts_only_actual_finish_schema(monkeypatch, tmp_path):
    from backend.app.agent_runtime.natural_reply import NaturalReplyProvider
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        delta = {'content': 'unformatted'} if len(requests) == 1 else {'tool_calls': [{'index': 0,
            'id': 'finish', 'type': 'function', 'function': {'name': 'finish_reply', 'arguments': '{"messages":["done"]}'}}]}
        chunk = {'choices': [{'delta': delta, 'finish_reason': 'stop' if len(requests) == 1 else 'tool_calls'}]}
        if len(requests) == 1:
            chunk['usage'] = {'prompt_tokens': 100, 'completion_tokens': 4}
        return httpx.Response(200, text=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    base = make_provider()
    window = make_window(base, tmp_path)
    provider = NaturalReplyProvider(window)
    business = [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]
    message = asyncio.run(respond(provider, [UserMessage('reply')], business))
    assert message.metadata['replySequence'] == ['done']
    assert [[tool['function']['name'] for tool in request['tools']] for request in requests] == [['read', 'finish_reply'], ['finish_reply']]
    assert message.metadata['contextUsage']['source'] == window.last_input_source == 'local'
    assert message.metadata['contextUsage']['inputTokens'] == window.last_input_estimate


def test_legacy_local_fallback_counts_image_once_and_keeps_current_time():
    from backend.app.agent_runtime.types import AttachmentContent, ImageContent
    provider = make_provider()
    provider.adapter = object()  # No pure projection available.
    def messages(detail):
        return [UserMessage('picture', attachments=(AttachmentContent('img', 'image/png',
            image=ImageContent('image/png', 'eA==', detail)),))]
    low = provider.estimate_request_tokens('', messages('low'), [])
    high = provider.estimate_request_tokens('', messages('high'), [])
    assert high - low == 4096 - 85
    assert provider._request_time in provider._combined_memory_context()


def test_started_request_freezes_connection_and_schema_before_callbacks(monkeypatch):
    requests = patch_openai(monkeypatch)
    provider = make_provider()
    tools = [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]
    messages = [UserMessage('question')]
    async def scenario():
        stream = provider.stream_response(model='fixture', system='', messages=messages, tools=tools, signal=CancellationToken())
        await anext(stream)
        provider.configuration.model = 'new-model'
        tools[0]['function']['name'] = 'changed'
        return [event async for event in stream]
    events = asyncio.run(scenario())
    reply = next(event.message for event in events if isinstance(event, AssistantDoneEvent))
    assert requests[0]['model'] == reply.metadata['contextUsage']['model'] == 'fixture'
    assert requests[0]['tools'][0]['function']['name'] == 'read'
    assert provider.estimate_request('', messages, tools).source == 'local'


def test_budget_reserves_the_actual_request_output_cap(monkeypatch, tmp_path):
    requests = patch_openai(monkeypatch, usage=123)
    provider = make_provider()
    window = make_window(provider, tmp_path)
    messages = [UserMessage('question')]
    asyncio.run(respond(provider, messages))
    provider.configuration.contextWindowTokens = 2000
    provider.max_response_tokens = 1900  # Settings still say 1024.
    with pytest.raises(ValueError, match='输入空间'):
        asyncio.run(respond(window, messages))
    assert len(requests) == 1
