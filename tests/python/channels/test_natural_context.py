"""Natural requests retain attribution and complete memory blocks under pressure."""
import asyncio
import json
from dataclasses import replace
from time import perf_counter

import pytest

from backend.app import main
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.usage_anchor import InputEstimate
from backend.app.agent_runtime.file_tools import ReadFileTool
from backend.app.agent_runtime.tools import AgentToolResult
from backend.app.model_adapter import ModelTextDelta, ModelToolCall, ModelResponseDone
from test_conversation_relations import group_message
from test_meme_turn import meme_turn
from test_natural_turn import natural


@pytest.mark.parametrize('shorthand', ['', '成员速记' * 30], ids=['profile-only', 'with-shorthand'])
def test_natural_memory_budget_keeps_notes_and_complete_member_entries(natural, shorthand):
    s = natural

    async def run():
        try:
            for index in range(12):
                await s.receive(str(index), sender=str(22 + index))
                profile = s.channels.external_memory.members.profile(s.thread, str(22 + index))
                with s.channels._connect() as db:
                    s.channels.external_memory.members.write_in(db, s.thread, profile['key'],
                        profile=f'PROFILE_{22 + index}_' + '成员详细资料"\\\n' * 200, shorthand=shorthand)
            main.group_repository.update_context(s.role.id, s.thread.thread_id,
                group_note='GROUP_NOTE' + '约定' * 600, summary='GROUP_ACTIVITY' + '动态' * 600)
            blocks = main._channel_memory_blocks(s.role.id, s.thread.thread_id, '33', natural=True,
                trigger_metadata={'contextMemberIds': [str(22 + index) for index in range(12)]})
            assert any(block.startswith('[当前群笔记]') for block in blocks)
            assert any(block.startswith('[当前群近期摘要]') for block in blocks)
            member_block = next(block for block in blocks if block.startswith('[当前会话成员资料'))
            members = json.loads(member_block.split('\n', 1)[1])
            # The current speaker and first response candidate retain useful
            # facts even when twelve relevant profiles compete for capacity.
            assert [member['senderId'] for member in members[:2]] == ['33', '22']
            assert members[0]['profile'].startswith('PROFILE_33_')
            assert members[1]['profile'].startswith('PROFILE_22_')
            assert all(member.get('profile') or member['shorthand'] for member in members)
            assert len(member_block) <= 1600
            assert all('sources' in member and 'senderId' in member for member in members)
            assert all(member['sources'] == [s.thread.thread_id] for member in members)
            assert '摘录' in str(blocks)
        finally:
            await s.scheduler.close()

    asyncio.run(run())


def test_selected_target_and_tool_result_survive_expression_and_repair(natural, monkeypatch, record_property):
    s = natural
    s.scheduler.limits = replace(s.scheduler.limits, context_chars=30000, max_calls=4)
    original = MeidoProvider.estimate_prepared
    calls, executions = s.calls, []

    def estimate(self, prepared):
        count = sum('sourceMessageId' in str(message) for message in prepared.messages)
        expression = '[自然参与表达]' in str(prepared.messages)
        return InputEstimate(original(self, prepared).tokens + count * (3000 if expression else 0), 'test')

    async def read(self, arguments, context, on_update=None):
        executions.append(arguments['path'])
        return AgentToolResult('VERIFIED_TOOL_FACT' + '原文材料' * 500)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append((messages, tools))
            inputs = [json.loads(item['content']) for item in messages
                if item['role'] == 'user' and item['content'].startswith('{')]
            ids = {item['messageId'] for item in inputs}
            assert {'target-0', 'target-9'} <= ids
            if len(calls) == 1:
                yield ModelTextDelta(json.dumps({'action': 'reply', 'response': {
                    'message_id': 'target-0', 'topic_message_ids': ['target-9'],
                    'intent': '根据主人请托回答另一位群友的问题'}}))
            elif len(calls) == 2:
                assert len(inputs) < 10
                yield ModelToolCall('read-once', 'read_file', {'path': 'source.txt'})
                yield ModelResponseDone('tool_use')
            elif len(calls) == 3:
                assert 'VERIFIED_TOOL_FACT' in str(messages)
                yield ModelTextDelta('需要修正的普通正文')
            else:
                assert not tools
                assert 'VERIFIED_TOOL_FACT' in str(messages)
                assert any(item.get('tool_calls') for item in messages)
                yield ModelTextDelta('{"text":"根据核对的资料回答群友。"}')

    monkeypatch.setattr(MeidoProvider, 'estimate_prepared', estimate)
    monkeypatch.setattr(ReadFileTool, 'execute', read)
    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        started = perf_counter()
        try:
            for index in range(10):
                await main._dispatch_channel_payload(s.adapter.account_id,
                    group_message('target-' + str(index), '22' if index == 0 else '11',
                        '请查资料回答前面群友的问题' if index == 9 else '讨论问题'), source_adapter=s.adapter)
            await asyncio.sleep(0)
            pending = tuple(s.scheduler.tasks.values())
            await s.clock.advance(1)
            await asyncio.wait_for(asyncio.gather(*pending), 8)
            assert len(calls) == 4 and len(s.operations) == 1
            assert executions == ['source.txt']
            sent = next(item for item in s.sessions.list_messages(s.thread.session_key)
                if item.role == 'assistant' and item.metadata.get('deliveryStatus') == 'sent')
            assert sent.metadata['responseRelation']['targetSenderId'] == '22'
            record_property('natural_context_replay', json.dumps({
                'messages': 10, 'planning_calls': 1, 'expression_calls': 1,
                'tool_continuation_calls': 1, 'format_correction_calls': 1,
                'tool_executions': len(executions), 'input_characters': [len(str(items)) for items, _ in calls],
                'elapsed_seconds': round(perf_counter() - started, 3)}, ensure_ascii=False))
        finally:
            await s.scheduler.close()
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(run())


def test_full_request_pressure_evicts_old_messages_before_abandoning_reply(natural, monkeypatch, record_property):
    s = natural
    s.scheduler.limits = replace(s.scheduler.limits, context_chars=30000)
    original = MeidoProvider.estimate_prepared

    def estimate(self, prepared):
        # Deterministic provider capacity pressure. Schemas still participate in
        # the real estimator; every original transcript row adds a known cost.
        count = sum('sourceMessageId' in str(message) for message in prepared.messages)
        return InputEstimate(original(self, prepared).tokens + count * 6000, 'test')

    monkeypatch.setattr(MeidoProvider, 'estimate_prepared', estimate)

    async def run():
        started = perf_counter()
        try:
            for index in range(100):
                await s.receive('budget-' + str(index), sender='22')
            pending = tuple(s.scheduler.tasks.values())
            await s.clock.advance(1)
            await asyncio.wait_for(asyncio.gather(*pending), 5)
            assert len(s.calls) == 2 and len(s.operations) == 1
            for messages, _ in s.calls:
                inputs = [json.loads(item['content']) for item in messages
                    if item['role'] == 'user' and item['content'].startswith('{')]
                assert inputs[-1]['messageId'] == 'budget-99'
                assert len(inputs) < 30
                assert all(int(item['messageId'].split('-')[-1]) >= 70 for item in inputs)
                assert all(item['senderId'] == '22' for item in inputs)
            record_property('natural_context_hundred_messages', json.dumps({
                'messages': 100, 'planning_calls': 1, 'expression_calls': 1,
                'input_characters': [len(str(items)) for items, _ in s.calls],
                'elapsed_seconds': round(perf_counter() - started, 3)}))
        finally:
            await s.scheduler.close()
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(run())


def test_unfittable_tool_result_stops_request_and_keeps_committed_fact(natural, monkeypatch):
    s = natural
    original = MeidoProvider.estimate_prepared
    calls, executions = s.calls, []

    def estimate(self, prepared):
        if 'COMMITTED_OVERSIZED_FACT' in str(prepared.messages):
            return InputEstimate(40000, 'test')
        return original(self, prepared)

    async def read(self, arguments, context, on_update=None):
        executions.append(arguments['path'])
        return AgentToolResult('COMMITTED_OVERSIZED_FACT')

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append((messages, tools))
            if len(calls) == 1:
                yield ModelTextDelta(json.dumps({'action': 'reply', 'response': {
                    'message_id': 'required', 'intent': '核对当前材料'}}))
            else:
                yield ModelToolCall('required-read', 'read_file', {'path': 'required.txt'})
                yield ModelResponseDone('tool_use')

    monkeypatch.setattr(MeidoProvider, 'estimate_prepared', estimate)
    monkeypatch.setattr(ReadFileTool, 'execute', read)
    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        try:
            await s.receive('required')
            pending = tuple(s.scheduler.tasks.values())
            await s.clock.advance(1)
            await asyncio.wait_for(asyncio.gather(*pending), 5)
            assert len(calls) == 2 and executions == ['required.txt']
            assert not s.operations
            fact = next(item for item in s.sessions.list_messages(s.thread.session_key) if item.role == 'tool')
            assert fact.content == 'COMMITTED_OVERSIZED_FACT'
            assert '超出容量' in (s.sessions.get_run(fact.runId).error or '')
        finally:
            await s.scheduler.close()
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(run())


def test_current_message_keeps_request_after_long_body(natural):
    s = natural
    body = '背景说明' * 400 + '\n请根据以上内容回答群友甲。'

    async def run():
        try:
            await main._dispatch_channel_payload(s.adapter.account_id,
                group_message('long-current', '11', body), source_adapter=s.adapter)
            await asyncio.sleep(0)
            pending = tuple(s.scheduler.tasks.values())
            await s.clock.advance(1)
            await asyncio.wait_for(asyncio.gather(*pending), 5)
            assert len(s.calls) == 2 and len(s.operations) == 1
            for messages, _ in s.calls:
                source = next(json.loads(item['content']) for item in messages
                    if item['role'] == 'user' and item['content'].startswith('{'))
                assert source['text'] == body
        finally:
            await s.scheduler.close()
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(run())
