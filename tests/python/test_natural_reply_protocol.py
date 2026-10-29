import asyncio
import pytest

from backend.app.agent_runtime.natural_reply import NaturalReplyProvider
from backend.app.agent_runtime.provider import AssistantDoneEvent, CancellationToken, TextDeltaEvent
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, UserMessage
from backend.app.agent_runtime.loop import run_agent_loop
from backend.app.agent_runtime.tools import AgentToolResult, ToolDefinition
from backend.app.agent_runtime.provider import ThinkingDeltaEvent
from contextlib import aclosing


def test_natural_reply_streams_thinking_before_finish_without_publishing_draft():
    async def scenario():
        release = asyncio.Event()
        class Provider:
            async def stream_response(self, **kwargs):
                yield TextDeltaEvent('未提交的草稿')
                yield ThinkingDeltaEvent('查阅公开资料')
                await release.wait()
                yield AssistantDoneEvent(completed('最终回复'))
        wrapped = NaturalReplyProvider(Provider())
        async with aclosing(wrapped.stream_response(model='fake', system='', messages=(UserMessage('你好'),),
                tools=(), signal=CancellationToken())) as stream:
            first = await asyncio.wait_for(anext(stream), .5)
            assert isinstance(first, ThinkingDeltaEvent) and first.delta == '查阅公开资料'
            release.set()
            rest = [event async for event in stream]
            assert len(rest) == 1 and rest[0].message.content == '最终回复'
    asyncio.run(scenario())


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
            yield ThinkingDeltaEvent('superseded thinking')
            finished = True
            yield AssistantDoneEvent(completed('not sent'))
    async def scenario():
        provider = NaturalReplyProvider(Provider(), authorized=lambda: active)
        with pytest.raises(asyncio.CancelledError):
            _ = [item async for item in provider.stream_response(model='', system='', messages=(), tools=(), signal=CancellationToken())]
    asyncio.run(scenario())
    assert finished


def test_cancel_during_format_correction_closes_stream_without_draft_or_final_reply():
    async def scenario():
        signal = CancellationToken()
        closed = []
        class Provider:
            count = 0
            async def stream_response(self, **request):
                self.count += 1
                try:
                    yield TextDeltaEvent('private protocol draft')
                    if self.count == 1:
                        yield AssistantDoneEvent(AssistantMessage('invalid draft'))
                    else:
                        yield ThinkingDeltaEvent('整理回复格式')
                        signal.cancel()
                        yield AssistantDoneEvent(completed('must not appear'))
                finally:
                    closed.append(self.count)
        observed = []
        with pytest.raises(asyncio.CancelledError):
            async for event in NaturalReplyProvider(Provider()).stream_response(
                    model='', system='', messages=(), tools=(), signal=signal):
                observed.append(event)
        assert [event.delta for event in observed] == ['整理回复格式']
        assert closed == [1, 2]
    asyncio.run(scenario())


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
