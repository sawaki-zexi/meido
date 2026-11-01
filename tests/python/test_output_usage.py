"""Provider wire counts retain their meaning before summary accounting."""
import json
import struct

import httpx
import pytest

from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.ai.model_types import ModelStreamDone, ModelThinkingBlock
from test_model_protocol_adapters import configuration, messages


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def wire(api, usage):
    def sse(events):
        return ''.join(f'data: {json.dumps(event)}\n\n' for event in events)
    if api in {'openai-completions', 'mistral-conversations'}:
        return sse([{'choices': [{'delta': {'reasoning_content': 'thought', 'content': 'answer'}, 'finish_reason': 'stop'}],
                     'usage': usage}]) + 'data: [DONE]\n\n'
    if api in {'openai-responses', 'azure-openai-responses', 'openai-codex-responses'}:
        return sse([
            {'type': 'response.reasoning_summary_text.delta', 'output_index': 0, 'delta': 'thought'},
            {'type': 'response.output_text.delta', 'output_index': 1, 'delta': 'answer'},
            {'type': 'response.completed', 'response': {'status': 'completed', 'usage': usage}},
        ])
    if api in {'google-generative', 'google-vertex'}:
        return sse([{'candidates': [{'content': {'parts': [{'text': 'thought', 'thought': True}, {'text': 'answer'}]},
                                     'finishReason': 'STOP'}], 'usageMetadata': usage}])
    if api == 'anthropic-messages':
        events = [
            ('message_start', {'message': {'id': 'm', 'usage': {'input_tokens': 17, 'output_tokens': 1}}}),
            ('content_block_start', {'index': 0, 'content_block': {'type': 'thinking', 'thinking': 'thought'}}),
            ('content_block_stop', {'index': 0}),
            ('content_block_start', {'index': 1, 'content_block': {'type': 'text', 'text': 'answer'}}),
            ('content_block_stop', {'index': 1}),
            ('message_delta', {'delta': {'stop_reason': 'end_turn'}, 'usage': usage}),
            ('message_stop', {}),
        ]
        return ''.join(f'event: {name}\ndata: {json.dumps(event)}\n\n' for name, event in events)
    if api == 'pi-messages':
        return sse([{'type': 'thinking_start', 'contentIndex': 0},
                    {'type': 'thinking_delta', 'contentIndex': 0, 'delta': 'thought'},
                    {'type': 'thinking_end', 'contentIndex': 0},
                    {'type': 'text_start', 'contentIndex': 1},
                    {'type': 'text_delta', 'contentIndex': 1, 'delta': 'answer'},
                    {'type': 'text_end', 'contentIndex': 1},
                    {'type': 'done', 'reason': 'stop', 'usage': usage}])
    if api == 'bedrock-converse':
        def frame(event):
            payload = json.dumps(event).encode()
            return struct.pack('>III', 16 + len(payload), 0, 0) + payload + struct.pack('>I', 0)
        return b''.join(map(frame, [
            {'messageStart': {'role': 'assistant'}},
            {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'reasoningText': {'text': 'thought'}}}}},
            {'contentBlockStart': {'contentBlockIndex': 1, 'start': {}}},
            {'contentBlockDelta': {'contentBlockIndex': 1, 'delta': {'text': 'answer'}}},
            {'messageStop': {'stopReason': 'end_turn'}}, {'metadata': {'usage': usage}},
        ]))
    raise AssertionError(api)


async def response(monkeypatch, api, usage):
    body = wire(api, usage)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))))
    config = configuration(api)
    if api == 'google-vertex':
        config.apiOptions = {'project': 'fixture', 'location': 'us-central1'}
    if api == 'openai-codex-responses':
        config.apiOptions = {'accountId': 'fixture-account'}
    events = [event async for event in RoutedModelAdapter(ModelAdapterRegistry()).stream_rich_messages(messages(), config)]
    assert isinstance(events[-1], ModelStreamDone), events
    return events[-1].message


@pytest.mark.anyio
@pytest.mark.parametrize('api,usage,visible', [
    ('openai-completions', {'completion_tokens': 2500, 'completion_tokens_details': {'reasoning_tokens': 1500}}, 1000),
    ('openai-responses', {'output_tokens': 2500, 'output_tokens_details': {'reasoning_tokens': 1500}}, 1000),
    ('azure-openai-responses', {'output_tokens': 2500, 'output_tokens_details': {'reasoning_tokens': 1500}}, 1000),
    ('openai-codex-responses', {'output_tokens': 2500, 'output_tokens_details': {'reasoning_tokens': 1500}}, 1000),
    ('anthropic-messages', {'output_tokens': 2500, 'output_tokens_details': {'thinking_tokens': 1500}}, 1000),
    ('google-generative', {'candidatesTokenCount': 1000, 'thoughtsTokenCount': 1500}, 1000),
    ('google-vertex', {'candidatesTokenCount': 1000, 'thoughtsTokenCount': 1500}, 1000),
    ('pi-messages', {'output': 2500, 'reasoning': 1500}, 1000),
    ('mistral-conversations', {'completion_tokens': 2500}, None),
    ('bedrock-converse', {'outputTokens': 2500}, None),
])
async def test_actual_protocols_preserve_output_and_reasoning_semantics(monkeypatch, api, usage, visible):
    message = await response(monkeypatch, api, usage)
    assert message.metadata.usage.output_tokens == 2500
    assert any(isinstance(block, ModelThinkingBlock) for block in message.content)
    counted = message.metadata.usage.output_usage.for_response(has_thinking=True)
    assert counted.visible_tokens == visible
    assert counted.unavailable_reason == ('' if visible is not None else 'missing_reasoning_usage')
    if api == 'anthropic-messages':
        assert message.metadata.usage.input_tokens == 17  # final delta must retain start usage


@pytest.mark.anyio
@pytest.mark.parametrize('api,usage,reason', [
    ('openai-responses', {}, 'missing_completion_usage'),
    ('openai-responses', {'output_tokens': '2500'}, 'invalid_completion_usage'),
    ('openai-responses', {'output_tokens': 2500, 'output_tokens_details': {'reasoning_tokens': -1}}, 'invalid_reasoning_usage'),
    ('anthropic-messages', {'output_tokens': 2500}, 'missing_reasoning_usage'),
    ('anthropic-messages', {'output_tokens': 2500, 'output_tokens_details': {'thinking_tokens': True}}, 'invalid_reasoning_usage'),
    ('google-generative', {'thoughtsTokenCount': 1500}, 'missing_completion_usage'),
    ('google-generative', {'candidatesTokenCount': 1000, 'thoughtsTokenCount': '1500'}, 'invalid_reasoning_usage'),
    ('pi-messages', {'output': 2500, 'reasoning': 3000}, 'invalid_reasoning_usage'),
    ('mistral-conversations', {'completion_tokens': False}, 'invalid_completion_usage'),
    ('bedrock-converse', {'outputTokens': '2500'}, 'invalid_completion_usage'),
])
async def test_raw_missing_and_invalid_counts_are_not_confused_with_valid_zero(monkeypatch, api, usage, reason):
    message = await response(monkeypatch, api, usage)
    counted = message.metadata.usage.output_usage.for_response(has_thinking=True)
    assert counted.visible_tokens is None and counted.unavailable_reason == reason


@pytest.mark.anyio
async def test_google_visible_count_is_independent_of_missing_thoughts_count(monkeypatch):
    message = await response(monkeypatch, 'google-generative', {'candidatesTokenCount': 1000})
    counted = message.metadata.usage.output_usage.for_response(has_thinking=True)
    assert counted.visible_tokens == 1000 and not counted.unavailable_reason


def anthropic_without_thinking(text, terminal):
    """message_start usage is provisional, even when no thinking is emitted."""
    events = [
        ('message_start', {'message': {'id': 'm', 'usage': {
            'input_tokens': 17, 'output_tokens': 1, 'cache_read_input_tokens': 11, 'cache_creation_input_tokens': 7}}}),
        ('content_block_start', {'index': 0, 'content_block': {'type': 'text', 'text': text}}),
        ('content_block_stop', {'index': 0}),
    ]
    if terminal != 'no_delta':
        delta = {'delta': {'stop_reason': 'end_turn'}}
        if terminal == 'valid':
            delta['usage'] = {'output_tokens': 2500, 'output_tokens_details': {'thinking_tokens': 1500}}
        elif terminal != 'missing':
            delta['usage'] = {'null': None, 'not_object': ['invalid'], 'empty': {}}[terminal]
        events.append(('message_delta', delta))
    events.append(('message_stop', {}))
    return ''.join(f'event: {name}\ndata: {json.dumps(event)}\n\n' for name, event in events)


@pytest.mark.anyio
@pytest.mark.parametrize('terminal', ['missing', 'null', 'not_object', 'empty', 'no_delta', 'valid'])
async def test_anthropic_only_final_usage_can_bound_output_without_thinking(monkeypatch, terminal):
    from backend.app.ai.model_types import ModelStreamError
    body = anthropic_without_thinking('x' * 7000, terminal)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))))
    events = [event async for event in RoutedModelAdapter(ModelAdapterRegistry()).stream_rich_messages(
        messages(), configuration('anthropic-messages'))]
    if terminal == 'no_delta':
        assert isinstance(events[-1], ModelStreamError)
        assert not any(isinstance(event, ModelStreamDone) for event in events)
        return
    message = events[-1].message
    assert not any(isinstance(block, ModelThinkingBlock) for block in message.content)
    usage = message.metadata.usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (17, 11, 7)
    # Preserve the existing display counters; only strict summary evidence changes.
    assert usage.output_tokens == (2500 if terminal == 'valid' else 0 if terminal == 'empty' else 1)
    strict = usage.output_usage.for_response(has_thinking=False)
    assert strict.visible_tokens == (1000 if terminal == 'valid' else None)
    assert strict.unavailable_reason == ('' if terminal == 'valid' else 'missing_completion_usage')
