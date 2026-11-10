"""Two real model requests, validated choices, platform payloads and receipts."""
import asyncio
from dataclasses import replace
import json

import pytest

from backend.app import main
from test_conversation_relations import at, group_message
from test_natural_turn import natural
from test_meme_turn import meme_turn
from backend.app.model_adapter import ModelTextDelta


async def until(predicate):
    async def wait():
        while not predicate(): await asyncio.sleep(.002)
    await asyncio.wait_for(wait(), 5)


@pytest.mark.parametrize('max_calls, expected', [(1, 1), (2, 2)])
def test_each_actual_phase_uses_original_quota_and_interval(natural, monkeypatch, max_calls, expected):
    s = natural
    s.scheduler.limits = replace(s.scheduler.limits, max_calls=max_calls, min_role_interval=3)
    requests = s.calls

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            assert not tools
            requests.append(messages)
            if '[自然参与规划]' in messages[0]['content']:
                yield ModelTextDelta(json.dumps({'action': 'reply', 'response': {
                    'message_id': 'new-question', 'intent': '从旁观者角度讨论安排'}}))
            else:
                yield ModelTextDelta(json.dumps({'text': '我可以帮你们一起看安排。'}))

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        try:
            await main._dispatch_channel_payload(s.adapter.account_id,
                group_message('new-question', '11', '你明天能来吗', at('22')), source_adapter=s.adapter)
            await asyncio.sleep(0)
            await s.clock.advance(1)
            await until(lambda: bool(requests))
            assert len(requests) == 1 and not s.operations
            if max_calls == 2:
                batch = s.scheduler.batches[s.thread.thread_id]
                assert batch.deadline == 1010 and batch.calls == 1
            await s.clock.advance(3)
            await until(lambda: not s.scheduler.batches)
            assert len(requests) == expected
            with s.channels._connect() as db:
                assert db.execute('SELECT calls FROM channel_natural_quota').fetchone()[0] == expected
            first = '\n'.join(item['content'] for item in requests[0])
            assert '"mentionedIds": ["22"]' in first
            assert '"receivingPlatformId": "99"' in first
            assert '"addressing": "other"' in first
            assert len(s.operations) == (1 if expected == 2 else 0)
            if expected == 2:
                message = s.sessions.list_messages(s.thread.session_key)[0]
                relation = message.metadata['responseRelation']
                assert relation['targetPlatformMessageId'] == 'new-question'
                assert relation['targetSenderId'] == '11'
                assert relation['triggerSourceMessageId'] is None
                assert [part['type'] for part in s.operations[0][1]['message']] == ['text']
                runs = [s.sessions.get_run(message.metadata['sourceRunId']), s.sessions.get_run(message.metadata['naturalPlanRunId'])]
                assert {run.modelSnapshot['naturalPhase'] for run in runs} == {'plan', 'expression'}
        finally:
            await s.scheduler.close()

    asyncio.run(run())


@pytest.mark.parametrize('action', ['wait', 'silence'])
def test_wait_or_silence_has_no_expression_request_or_empty_reply(natural, monkeypatch, action):
    s = natural
    requests = s.calls

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            assert '[自然参与规划]' in messages[0]['content'] and not tools
            yield ModelTextDelta(json.dumps({'action': action}))

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        try:
            await s.receive('listen')
            await s.clock.advance(1)
            await until(lambda: bool(requests))
            assert len(requests) == 1 and not s.operations
            assert not s.sessions.list_messages(s.thread.session_key)
        finally:
            await s.scheduler.close()

    asyncio.run(run())


def test_natural_timeline_includes_explicit_question_own_reply_and_selected_older_target(natural, monkeypatch):
    s = natural
    requests = s.calls

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append((messages, tools))
            if tools:
                yield ModelTextDelta('此前给甲的回答')
            elif '[自然参与规划]' in messages[0]['content']:
                yield ModelTextDelta(json.dumps({'action': 'reply', 'response': {
                    'message_id': 'explicit-question', 'topic_message_ids': ['followup'],
                    'intent': '向乙解释之前给甲的回答', 'quote_message_id': 'explicit-question', 'mention_ids': ['22']}}))
            else:
                yield ModelTextDelta(json.dumps({'text': '我之前的意思是可以一起讨论。'}))

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        try:
            await main._dispatch_channel_payload(s.adapter.account_id,
                group_message('explicit-question', '22', '甲明确向角色提问', at('99')), source_adapter=s.adapter)
            sent = next(item for item in s.sessions.list_messages(s.thread.session_key) if item.role == 'assistant' and item.content)
            profile = s.channels.external_memory.members.profile(s.thread, '22')
            with s.channels._connect() as db:
                s.channels.external_memory.members.write_in(db, s.thread, profile['key'],
                    profile='FOCUSED_MEMBER_22_PROFILE', shorthand='甲的速记')
            old_reply_id = sent.metadata['platformMessageId']
            s.operations.clear()
            await main._dispatch_channel_payload(s.adapter.account_id,
                group_message('followup', '33', '你刚说的是什么意思'), source_adapter=s.adapter)
            await asyncio.sleep(0)
            await s.clock.advance(1)
            await until(lambda: not s.scheduler.batches)
            assert len(requests) == 3 and len(s.operations) == 1
            planner = requests[1][0]
            events = []
            for message in planner:
                try: event = json.loads(message['content'])
                except ValueError: continue
                if isinstance(event, dict): events.append(event)
            own = next(event for event in events if event['sourceKind'] == 'role_sent')
            assert own['messageId'] == old_reply_id and own['senderId'] == '99'
            assert own['responseRelation']['targetPlatformMessageId'] == 'explicit-question'
            assert any(event['messageId'] == 'explicit-question' and event['senderId'] == '22' for event in events)
            assert any(event['messageId'] == 'followup' and event['senderId'] == '33' for event in events)
            assert 'FOCUSED_MEMBER_22_PROFILE' in str(requests[2][0])
            segments = s.operations[0][1]['message']
            assert next(part['data']['id'] for part in segments if part['type'] == 'reply') == 'explicit-question'
            assert [str(part['data']['qq']) for part in segments if part['type'] == 'at'] == ['22']
            reply = s.sessions.list_messages(s.thread.session_key)[-1]
            assert reply.metadata['responseRelation']['targetPlatformMessageId'] == 'explicit-question'
            assert 'followup' in reply.metadata['relatedInputIds']
        finally:
            await s.scheduler.close()

    asyncio.run(run())


def test_related_input_during_expression_exhausts_original_three_calls_without_old_send(natural, monkeypatch):
    s = natural
    requests, expression_entered, release = s.calls, None, None

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            assert not tools
            requests.append(messages)
            if '[自然参与规划]' in messages[0]['content']:
                yield ModelTextDelta(json.dumps({'action': 'reply', 'response': {
                    'message_id': 'first', 'intent': '回应原话题'}}))
            else:
                expression_entered.set()
                await release.wait()
                yield ModelTextDelta(json.dumps({'text': '这个旧草稿不能再发'}))

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        nonlocal expression_entered, release
        expression_entered, release = asyncio.Event(), asyncio.Event()
        s.block = release
        try:
            await s.receive('first', sender='22')
            await s.clock.advance(1)
            await asyncio.wait_for(expression_entered.wait(), 5)
            batch = s.scheduler.batches[s.thread.thread_id]
            deadline = batch.deadline
            await s.receive('correction', sender='22')
            release.set()
            await until(lambda: not s.scheduler.batches)
            assert len(requests) == 3
            assert batch.calls == 3 and batch.deadline == deadline
            assert not s.operations and not s.sessions.list_messages(s.thread.session_key)
        finally:
            await s.scheduler.close()

    asyncio.run(run())


def test_expression_can_update_choice_then_generate_again_under_original_budget(natural, monkeypatch):
    s = natural
    requests = s.calls

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            assert not tools
            if len(requests) == 1:
                value = {'action': 'reply', 'response': {'message_id': 'first', 'intent': '先回应甲'}}
            elif len(requests) == 2:
                value = {'response': {'message_id': 'second', 'intent': '改为接乙的话题',
                    'quote_message_id': 'second', 'mention_ids': []}}
            else:
                assert '"targetPlatformMessageId": "second"' in str(messages)
                value = {'text': '这是给乙的话题的新表达'}
            yield ModelTextDelta(json.dumps(value))

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        try:
            await s.receive('first', sender='22')
            await s.receive('second', sender='33')
            await s.clock.advance(1)
            await until(lambda: not s.scheduler.batches)
            assert len(requests) == 3 and len(s.operations) == 1
            parts = s.operations[0][1]['message']
            assert next(part['data']['id'] for part in parts if part['type'] == 'reply') == 'second'
            reply = s.sessions.list_messages(s.thread.session_key)[0]
            assert reply.metadata['responseRelation']['targetSenderId'] == '33'
            assert reply.metadata['responseRelation']['targetPlatformMessageId'] == 'second'
            with s.channels._connect() as db:
                assert db.execute('SELECT calls FROM channel_natural_quota').fetchone()[0] == 3
        finally:
            await s.scheduler.close()

    asyncio.run(run())


def test_only_confirmed_partial_self_speech_is_selectable_and_revalidated(natural, monkeypatch):
    s = natural
    s.channels.append_event(s.thread, direction='outbound', content='CONFIRMED_PREFIX_UNSENT_SUFFIX',
        external_message_id='old-own', role='assistant', status='completed', metadata={
            'receivingPlatformId': '99', 'deliveryStatus': 'uncertain', 'partialDelivery': True,
            'parts': [{'kind': 'text', 'status': 'sent', 'content': 'CONFIRMED_PREFIX', 'platformMessageId': 'old-own'},
                {'kind': 'text', 'status': 'uncertain', 'content': '_UNSENT_SUFFIX', 'platformMessageId': None}]})
    s.channels.append_event(s.thread, direction='outbound', content='NO_CONFIRMED_RECEIPT', external_message_id='unconfirmed',
        role='assistant', status='completed', metadata={'deliveryStatus': 'uncertain'})
    requests = s.calls

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            assert 'UNSENT_SUFFIX' not in str(messages) and 'NO_CONFIRMED_RECEIPT' not in str(messages)
            assert 'CONFIRMED_PREFIX' in str(messages)
            value = ({'action': 'reply', 'response': {'message_id': 'old-own',
                'topic_message_ids': ['followup'], 'intent': '解释我实际发出的前半句'}}
                if '[自然参与规划]' in messages[0]['content'] else {'text': '解释已发的部分'})
            yield ModelTextDelta(json.dumps(value))

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        try:
            await s.receive('followup', sender='22')
            await s.clock.advance(1)
            await until(lambda: not s.scheduler.batches)
            assert len(requests) == 2 and len(s.operations) == 1
        finally:
            await s.scheduler.close()

    asyncio.run(run())


def test_quoted_target_author_correction_invalidates_expression_without_original_quote_row(natural, monkeypatch):
    s = natural
    requests, entered, release = s.calls, None, None
    original = s.adapter._transport

    async def transport(operation, params):
        if operation == 'get_msg':
            return {'data':{'group_id':100, 'message_type':'group', 'sender':{'user_id':11},
                'message':[{'type':'text','data':{'text':'我的原先安排'}}]}}
        return await original(operation, params)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            assert not tools
            if '[自然参与规划]' in messages[0]['content']:
                value = {'action':'reply','response':{'message_id':'unrecorded-quote', 'intent':'回应引用里甲的安排'}}
            else:
                entered.set()
                await release.wait()
                value = {'text':'这个旧表达应被丢弃'}
            yield ModelTextDelta(json.dumps(value))

    monkeypatch.setattr(s.adapter, '_transport', transport)
    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        s.block = release
        try:
            await main._dispatch_channel_payload(s.adapter.account_id, group_message('quoting-member', '22', '请讨论甲的原话',
                {'type':'reply','data':{'id':'unrecorded-quote'}}), source_adapter=s.adapter)
            await asyncio.sleep(0)
            await s.clock.advance(1)
            await asyncio.wait_for(entered.wait(), 5)
            await s.receive('author-correction', sender='11')
            release.set()
            await until(lambda: not s.scheduler.batches)
            assert len(requests) == 3
            assert not any(op == 'send_group_msg' for op, _ in s.operations)
            assert not s.sessions.list_messages(s.thread.session_key)
        finally:
            await s.scheduler.close()

    asyncio.run(run())


def test_cancelled_provider_preparation_does_not_spend_a_model_request(natural, monkeypatch):
    from backend.app.agent_runtime.meido_provider import MeidoProvider
    from test_natural_scheduler import Clock
    s = natural
    original = MeidoProvider.prepare_context
    entered, release = None, None

    async def prepare(provider, signal, **kwargs):
        entered.set()
        await release.wait()
        return await original(provider, signal, **kwargs)

    monkeypatch.setattr(MeidoProvider, 'prepare_context', prepare)

    async def run():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            await s.receive('before-prepare')
            await Clock.advance(s.clock, 1)
            await asyncio.wait_for(entered.wait(), 5)
            task = s.scheduler.tasks[s.thread.thread_id]
            s.channels.listening.set_natural_enabled(s.role.id, s.thread.thread_id, False)
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            assert not s.calls and not s.operations
            with s.channels._connect() as db:
                assert db.execute('SELECT COALESCE(SUM(calls),0) FROM channel_natural_quota').fetchone()[0] == 0
        finally:
            await s.scheduler.close()

    asyncio.run(run())


def test_mixed_utc_and_local_offsets_keep_newer_question_and_own_answer_in_bounded_request(natural, monkeypatch):
    from datetime import datetime, timedelta, timezone
    s = natural
    s.scheduler.limits = replace(s.scheduler.limits, max_inputs=3)
    base = datetime.now(timezone.utc) - timedelta(minutes=2)
    s.channels.listening.hear(s.thread, '较旧旁听', 'old-hearing', {'senderId':'44', 'receivingPlatformId':'99'},
        timestamp=base.astimezone(timezone(timedelta(hours=8))))
    _, question = s.channels.append_event(s.thread, direction='inbound', content='较新的显式问题', external_message_id='explicit-q',
        metadata={'senderId':'22', 'senderIsOwner':False, 'receivingPlatformId':'99', 'triggered':True})
    _, answer = s.channels.append_event(s.thread, direction='outbound', content='对这个问题的已发回答', external_message_id='role-a',
        role='assistant', metadata={'receivingPlatformId':'99', 'deliveryStatus':'sent'})
    with s.channels._connect() as db:
        db.execute('UPDATE messages SET created_at=? WHERE message_id=?', ((base+timedelta(seconds=60)).isoformat(), question.id))
        db.execute('UPDATE messages SET created_at=? WHERE message_id=?', ((base+timedelta(seconds=61)).isoformat(), answer.id))
    requests = s.calls

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta('{"action":"silence"}')

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        try:
            await s.receive('latest', sender='33')
            await s.clock.advance(1)
            await until(lambda: not s.scheduler.batches)
            events = [json.loads(item['content']) for item in requests[0]
                if item['role']=='user' and item['content'].startswith('{')]
            assert [item['messageId'] for item in events] == ['explicit-q', 'role-a', 'latest']
        finally:
            await s.scheduler.close()

    asyncio.run(run())


def test_too_small_context_keeps_identity_envelope_whole_and_makes_no_request(natural):
    s = natural
    s.scheduler.limits = replace(s.scheduler.limits, context_chars=50)

    async def run():
        try:
            await s.receive('cannot-fit-source')
            await s.clock.advance(1)
            await until(lambda: not s.scheduler.batches)
            assert not s.calls and not s.operations
            with s.channels._connect() as db:
                assert db.execute('SELECT COALESCE(SUM(calls),0) FROM channel_natural_quota').fetchone()[0] == 0
            assert not s.sessions.list_messages(s.thread.session_key)
        finally:
            await s.scheduler.close()

    asyncio.run(run())
