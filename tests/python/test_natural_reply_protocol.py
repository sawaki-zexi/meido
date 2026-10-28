import asyncio
import pytest

from backend.app.agent_runtime.natural_reply import NaturalReplyProvider
from backend.app.agent_runtime.provider import AssistantDoneEvent, CancellationToken, TextDeltaEvent
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, UserMessage
from backend.app.agent_runtime.loop import run_agent_loop
from backend.app.agent_runtime.tools import AgentToolResult, ToolDefinition


def completed(*texts):
    return AssistantMessage(tool_calls=(ToolCall('finish', 'finish_reply', {'messages': list(texts)}),), stop_reason='tool_use')


def run(provider, tools=()):
    async def scenario():
        messages = [UserMessage('hello')]
        events = [event async for event in run_agent_loop(provider=NaturalReplyProvider(provider),
            model='', system='', messages=messages, tools=tools, max_turns=4)]
        return messages, events
    return asyncio.run(scenario())


def test_business_tools_finish_before_model_authored_messages():
    executions = []
    class Tool:
        definition = ToolDefinition('lookup', 'lookup', {'type': 'object', 'properties': {}})
        async def execute(self, *_args):
            executions.append('lookup')
            return AgentToolResult('actual result')
    class Provider:
        count = 0
        async def stream_response(self, **request):
            self.count += 1
            assert 'finish_reply' in {item['function']['name'] for item in request['tools']}
            if self.count == 1:
                yield TextDeltaEvent('premature draft')
                yield AssistantDoneEvent(AssistantMessage('premature draft', (
                    ToolCall('work', 'lookup'), *completed('premature reply').tool_calls), 'tool_use'))
            else:
                assert request['messages'][-1].content == 'actual result'
                yield AssistantDoneEvent(completed('first\nline', 'second'))
    messages, events = run(Provider(), [Tool()])
    assert executions == ['lookup']
    assert messages[-1].content == 'first\nline\n\nsecond'
    assert messages[-1].metadata['replySequence'] == ['first\nline', 'second']
    assert all(not getattr(event, 'delta', '') for event in events)
    assert all('premature' not in item.content for item in messages)


def test_invalid_plain_reply_corrects_once_with_no_business_tools():
    class Provider:
        count = 0
        async def stream_response(self, **request):
            self.count += 1
            if self.count == 1:
                yield TextDeltaEvent('not delivered')
                yield AssistantDoneEvent(AssistantMessage('not delivered'))
            else:
                assert [item['function']['name'] for item in request['tools']] == ['finish_reply']
                yield AssistantDoneEvent(completed('corrected'))
    provider = Provider()
    messages, events = run(provider)
    assert provider.count == 2
    assert messages[-1].content == 'corrected'
    assert messages[-1].metadata['naturalReplyCorrected'] is True


def test_second_invalid_response_fails_without_executing_correction_tools():
    class Provider:
        count = 0
        async def stream_response(self, **request):
            self.count += 1
            yield AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('bad', 'finish_reply', {'messages': ['']}),), stop_reason='tool_use'))
    provider = Provider()
    messages, events = run(provider)
    assert provider.count == 2
    assert events[-1].reason == 'failed'
    assert not messages[-1].content


def test_invalidated_plan_finishes_model_step_but_never_returns_draft():
    active = True
    finished = False
    class Provider:
        async def stream_response(self, **request):
            nonlocal active, finished
            active = False
            yield TextDeltaEvent('draft')
            finished = True
            yield AssistantDoneEvent(completed('not sent'))
    async def scenario():
        provider = NaturalReplyProvider(Provider(), authorized=lambda: active)
        with pytest.raises(asyncio.CancelledError):
            _ = [item async for item in provider.stream_response(model='', system='', messages=(), tools=(), signal=CancellationToken())]
    asyncio.run(scenario())
    assert finished


def test_business_call_replay_preserves_provider_signatures_without_draft_text():
    from backend.app.agent_runtime.meido_provider import MeidoProvider
    from backend.app.ai.model_types import ModelToolCallBlock, ModelThinkingBlock
    class Provider:
        async def stream_response(self, **request):
            yield AssistantDoneEvent(AssistantMessage('private draft', (ToolCall('signed', 'lookup'),),
                'tool_use', metadata={'content_blocks': [
                    {'type': 'text', 'text': 'private draft'},
                    {'type': 'thinking', 'text': '', 'signature': 'redacted-signature', 'redacted': True},
                    {'type': 'tool_call', 'id': 'signed', 'name': 'lookup', 'arguments': {},
                     'thought_signature': 'required-signature', 'namespace': 'functions'}]}))
    async def scenario():
        return [event async for event in NaturalReplyProvider(Provider()).stream_response(
            model='', system='', messages=(), tools=(), signal=CancellationToken())]
    message = asyncio.run(scenario())[0].message
    replay = MeidoProvider(None, None, None)._rich_messages('test system', [message])[-1]
    assert message.content == ''
    assert [type(block) for block in replay.content] == [ModelThinkingBlock, ModelToolCallBlock]
    assert replay.content[-1].thought_signature == 'required-signature'
    assert replay.content[-1].namespace == 'functions'
    assert replay.content[0].redacted is True
