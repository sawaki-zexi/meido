"""Natural judgments share role execution, not ordinary transcript creation."""
import asyncio
from datetime import datetime, timezone

import pytest

from backend.app.agent_runtime.runtime import RuntimeManager, ActiveRunError
from backend.app.agent_runtime.provider import AssistantDoneEvent, ToolCallEndEvent, ProviderErrorEvent
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, UserMessage
from backend.app.models import AgentRun
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


@pytest.fixture
def runtime(tmp_path):
    initialize_databases(tmp_path)
    return RuntimeManager(SessionStore(tmp_path / 'sessions.db'))


def run(name='natural', role='role'):
    return AgentRun(runId=name, roleId=role, sessionKey=f'thread:{role}:group',
                    status='created', startedAt=datetime.now(timezone.utc))


class Provider:
    def __init__(self, events):
        self.events = events
        self.calls = []

    async def stream_response(self, **kwargs):
        self.calls.append(kwargs)
        for event in self.events:
            yield event


async def invoke(runtime, value, provider, **kwargs):
    return await runtime.natural_response(value, provider=provider, model='test',
        system='role and scope', messages=[UserMessage('real group event', source_message_ids=('m1',))], **kwargs)


@pytest.mark.parametrize('content', ['{"decision":"silence"}', '{"decision":"wait"}', '{"decision":"reply","text":"hello"}'])
def test_decisions_persist_run_but_no_fictional_messages(runtime, content):
    provider = Provider([AssistantDoneEvent(AssistantMessage(content))])
    result = asyncio.run(invoke(runtime, run(), provider))
    assert result.content == content
    assert provider.calls[0]['tools'] == ()
    assert runtime.sessions.list_messages(run().sessionKey) == []
    assert runtime.sessions.get_run('natural').status == 'completed'
    assert runtime.sessions.get_run('natural').turnCount == 1
    assert runtime.sessions.get_run('natural').endedAt is not None
    assert not runtime.lock_for('role').locked()


@pytest.mark.parametrize('events', [
    [ToolCallEndEvent(ToolCall('x', 'shell', {}))],
    [AssistantDoneEvent(AssistantMessage(tool_calls=(ToolCall('x', 'shell', {}),)))],
    [ProviderErrorEvent('failure')], [],
])
def test_invalid_provider_output_never_creates_message_or_executes_tool(runtime, events):
    with pytest.raises(RuntimeError):
        asyncio.run(invoke(runtime, run(), Provider(events)))
    assert runtime.sessions.get_run('natural').status == 'failed'
    assert runtime.sessions.list_messages(run().sessionKey) == []
    assert not runtime.lock_for('role').locked()


def test_busy_or_revoked_natural_call_does_not_queue_or_create_run(runtime):
    async def exercise():
        await runtime.lock_for('role').acquire()
        with pytest.raises(ActiveRunError):
            await invoke(runtime, run(), Provider([]))
        runtime.lock_for('role').release()
        with pytest.raises(ActiveRunError):
            await invoke(runtime, run(), Provider([]), can_start=lambda: False)
    asyncio.run(exercise())
    assert runtime.sessions.get_run('natural') is None
    assert runtime.sessions.list_messages(run().sessionKey) == []


def test_targeted_stop_cleans_natural_only_and_releases_role(runtime):
    async def exercise():
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        class Blocking:
            async def stream_response(self, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                    yield AssistantDoneEvent(AssistantMessage('unreachable'))
                finally:
                    cleaned.set()
        ordinary, _, _ = await runtime.reserve_run(run('ordinary', 'other'), 'hello')
        task = asyncio.create_task(invoke(runtime, run(), Blocking()))
        await entered.wait()
        assert await runtime.stop_run('natural')
        assert task.cancelled() and cleaned.is_set()
        assert runtime.sessions.get_run('natural').status == 'cancelled'
        assert runtime.sessions.get_run('ordinary').status == 'created'
        assert runtime.lock_for('other').locked()
        assert not runtime.lock_for('role').locked()
        await runtime.reserve_run(run('next'), 'explicit input')
        runtime.cancel_unstarted_run('next')
        runtime.cancel_unstarted_run(ordinary.runId)
    asyncio.run(exercise())
    assert not any(message.runId == 'natural' for message in runtime.sessions.list_messages(run().sessionKey))


def test_timeout_fails_without_empty_transcript(runtime):
    class Blocking:
        async def stream_response(self, **kwargs):
            await asyncio.Event().wait()
            yield AssistantDoneEvent(AssistantMessage())
    with pytest.raises(TimeoutError):
        asyncio.run(invoke(runtime, run(), Blocking(), provider_timeout=0.001))
    assert runtime.sessions.get_run('natural').status == 'failed'
    assert runtime.sessions.list_messages(run().sessionKey) == []
    assert not runtime.lock_for('role').locked()


def test_natural_yields_to_explicit_fifo_handoff(runtime):
    async def exercise():
        first, user, _ = await runtime.reserve_run(run('first'), 'real explicit input')
        queued = asyncio.create_task(runtime.reserve_existing_user_message(run('queued'), user.id))
        await asyncio.sleep(0)
        runtime.cancel_unstarted_run(first.runId)
        # The lock is briefly unlocked before asyncio resumes its FIFO waiter.
        # Natural work must not join that queue or overtake the explicit input.
        assert not runtime.lock_for('role').locked()
        provider = Provider([AssistantDoneEvent(AssistantMessage('silence'))])
        with pytest.raises(ActiveRunError):
            await invoke(runtime, run(), provider)
        assert provider.calls == []
        await queued
        runtime.cancel_unstarted_run('queued')
    asyncio.run(exercise())
    assert runtime.sessions.get_run('natural') is None


def test_provider_usage_is_retained_on_run_not_transcript(runtime):
    value = run()
    value.modelSnapshot = {'natural': True, 'estimatedInputTokens': 29}
    usage = {'input': 31, 'output': 7, 'total_tokens': 38, 'cache_read': 12,
             'cost': {'input': 0.001, 'output': 0.002}}
    provider = Provider([AssistantDoneEvent(AssistantMessage(
        '{"decision":"silence"}', metadata={'usage': usage, 'unrelated': 'not retained'}))])
    asyncio.run(invoke(runtime, value, provider))
    assert runtime.sessions.get_run(value.runId).modelSnapshot == {
        'natural': True, 'estimatedInputTokens': 29, 'naturalUsage': usage,
    }
    assert runtime.sessions.list_messages(value.sessionKey) == []
