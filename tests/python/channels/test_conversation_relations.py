"""Conversation facts at the real intake, provider, dispatch and replay seams."""
import asyncio
import json

import pytest

from backend.app import main
from backend.app.agent_runtime.usage_anchor import SYSTEM_CONTEXT_FRAME_MARKER
from backend.app.model_adapter import ModelTextDelta, ModelToolCall, ModelResponseDone
from test_meme_turn import meme_turn, payload


def group_message(identifier, sender, text, *segments):
    value = payload(identifier, sender=sender, image=False, triggered=False)
    value['message'] = [*segments, {'type': 'text', 'data': {'text': text}}]
    return value


def at(member):
    return {'type': 'at', 'data': {'qq': member}}


def test_explicit_current_input_is_separate_from_hearing_with_real_addressees(meme_turn, monkeypatch):
    s = meme_turn
    s.channels.set_group_listening(s.role.id, s.adapter.account_id, '100', True)
    requests = []

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta('我看到了群里的讨论。')

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        await main._dispatch_channel_payload(s.adapter.account_id,
            group_message('heard', '11', 'OTHER_MEMBER_QUESTION', at('22')), source_adapter=s.adapter)
        await main._dispatch_channel_payload(s.adapter.account_id,
            group_message('current', '33', 'PLEASE_DISCUSS_THE_OTHER_QUESTION', at('99')), source_adapter=s.adapter)

    asyncio.run(run())
    if not requests:
        thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
        pytest.fail(str([s.sessions.get_run(item.runId).error for item in s.sessions.list_messages(thread.session_key) if item.runId]))
    users = [item['content'] for item in requests[-1] if item['role'] == 'user']
    frame = next(text for text in users if text.startswith(SYSTEM_CONTEXT_FRAME_MARKER))
    current = next(text for text in users if 'PLEASE_DISCUSS_THE_OTHER_QUESTION' in text)
    assert 'OTHER_MEMBER_QUESTION' in frame
    assert 'OTHER_MEMBER_QUESTION' not in current
    assert '"mentionedIds": ["22"]' in frame
    assert '"receivingPlatformId": "99"' in frame
    assert '"addressing": "other"' in frame
    assert '"senderId": "33"' in current and '"senderIsOwner": false' in current


@pytest.mark.parametrize('display', [{}, {'quote_message_id': None, 'mention_ids': []}])
def test_explicit_can_answer_another_visible_member_with_natural_chat_off(meme_turn, monkeypatch, display):
    s = meme_turn
    assert s.role.agentConfig.naturalChatEnabled is False
    s.channels.set_group_listening(s.role.id, s.adapter.account_id, '100', True)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            assert any(tool['function']['name'] == 'finish_reply' for tool in tools)
            yield ModelToolCall('finish', 'finish_reply', {'messages': ['回应群友的问题'],
                'response': {'message_id': 'asked-by-22', **display}})
            yield ModelResponseDone('tool_use')

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        await main._dispatch_channel_payload(s.adapter.account_id,
            group_message('asked-by-22', '22', '群友的问题'), source_adapter=s.adapter)
        await main._dispatch_channel_payload(s.adapter.account_id,
            group_message('request-by-owner', '11', '请回答上一位群友的问题', at('99')), source_adapter=s.adapter)

    asyncio.run(run())
    sends = [params for op, params in s.operations if op == 'send_group_msg']
    assert len(sends) == 1
    parts = sends[0]['message']
    if display:
        assert not any(part['type'] in {'reply', 'at'} for part in parts)
    else:
        assert next(part['data']['id'] for part in parts if part['type'] == 'reply') == 'asked-by-22'
        assert [str(part['data']['qq']) for part in parts if part['type'] == 'at'] == ['22']
    thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
    reply = next(item for item in s.sessions.list_messages(thread.session_key) if item.role == 'assistant' and item.content)
    relation = reply.metadata['responseRelation']
    assert relation['triggerPlatformMessageId'] == 'request-by-owner'
    assert relation['targetPlatformMessageId'] == 'asked-by-22'
    assert relation['targetSenderId'] == '22'
    replay = s.sessions.runtime_messages(thread.session_key)
    assert any('asked-by-22' in item.content and 'request-by-owner' in item.content
        for item in replay if getattr(item, 'role', '') == 'assistant')


@pytest.mark.parametrize('mention_ids', [None, []])
def test_verified_current_quote_is_a_candidate_without_original_row_or_listening(meme_turn, monkeypatch, mention_ids):
    s = meme_turn
    original = s.adapter._transport
    requests = []

    async def transport(operation, params):
        if operation == 'get_msg':
            return {'data': {'group_id': 100, 'message_type': 'group',
                'sender': {'user_id': 11, 'nickname': '原作者'},
                'message': [{'type': 'text', 'data': {'text': '请讨论这个尚未入库的提议'}}]}}
        return await original(operation, params)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            assert 'chat_meme_send' not in {item['function']['name'] for item in tools}
            yield ModelToolCall('finish', 'finish_reply', {'messages': ['回应引用中的提议'],
                'response': {'message_id': 'quoted-outside-window', **({'mention_ids':mention_ids} if mention_ids is not None else {})}})
            yield ModelResponseDone('tool_use')

    monkeypatch.setattr(s.adapter, '_transport', transport)
    monkeypatch.setattr(main, 'model_adapter', Model())
    asyncio.run(main._dispatch_channel_payload(s.adapter.account_id,
        group_message('guest-request', '22', '请你回应他上面这句话', at('99'),
            {'type': 'reply', 'data': {'id': 'quoted-outside-window'}}), source_adapter=s.adapter))
    assert len(requests) == 1
    parts = next(params['message'] for op, params in s.operations if op == 'send_group_msg')
    assert next(part['data']['id'] for part in parts if part['type'] == 'reply') == 'quoted-outside-window'
    assert [str(part['data']['qq']) for part in parts if part['type']=='at'] == (['11'] if mention_ids is None else [])
    thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
    messages = s.sessions.list_messages(thread.session_key)
    assert len([item for item in messages if item.role == 'user']) == 1
    relation = messages[-1].metadata['responseRelation']
    assert relation['triggerSenderId'] == '22' and relation['targetSenderId'] == '11'
    assert relation['targetQuotedFromSourceMessageId'] == messages[0].id


def test_foreign_group_target_is_never_visible_or_dispatchable(meme_turn, monkeypatch):
    s = meme_turn
    other = s.channels.ensure_channel_thread(s.role.id, 'qq', s.adapter.account_id, '200', 'group')
    _, foreign = s.channels.append_event(other, direction='inbound', content='FOREIGN_PRIVATE_TOPIC',
        external_message_id='same-id', metadata={'senderId': '22'})
    requests = []

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            assert 'FOREIGN_PRIVATE_TOPIC' not in str(messages)
            yield ModelToolCall('finish-' + str(len(requests)), 'finish_reply', {'messages': ['不能派发'],
                'response': {'message_id': foreign.id}})
            yield ModelResponseDone('tool_use')

    monkeypatch.setattr(main, 'model_adapter', Model())
    asyncio.run(main._dispatch_channel_payload(s.adapter.account_id,
        group_message('same-id', '11', '当前群内请求', at('99')), source_adapter=s.adapter))
    assert len(requests) == 2  # one existing format correction, then reject
    assert not any(op == 'send_group_msg' for op, _ in s.operations)


def test_reopen_preserves_historical_quote_owner_and_reply_relation_after_unpair(meme_turn, monkeypatch):
    from backend.app.session_store import SessionStore
    s = meme_turn
    original = s.adapter._transport
    requests = []

    async def transport(operation, params):
        if operation == 'get_msg':
            return {'data': {'group_id': 100, 'message_type': 'group', 'sender': {'user_id': 11},
                'message': [{'type': 'text', 'data': {'text': '主人原先提出的建议'}}]}}
        return await original(operation, params)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            yield ModelTextDelta('角色已确认的回答')

    monkeypatch.setattr(s.adapter, '_transport', transport)
    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        await main._dispatch_channel_payload(s.adapter.account_id,
            group_message('original-request', '22', '请解释主人这句话', at('99'),
                {'type': 'reply', 'data': {'id': 'owner-quote'}}), source_adapter=s.adapter)
        s.channels.unbind_identity(str(s.channels.list_identities()[0]['identityId']))
        before = len([op for op, _ in s.operations if op == 'send_group_msg'])
        reopened = SessionStore(s.sessions.database_path)
        thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
        replay = str(reopened.runtime_messages(thread.session_key))
        assert '"replyToSenderIdentity": "已配对主人"' in replay
        assert '"triggerPlatformMessageId": "original-request"' in replay
        assert len([op for op, _ in s.operations if op == 'send_group_msg']) == before
        await main._dispatch_channel_payload(s.adapter.account_id,
            group_message('new-request', '33', '你刚才的回答是什么意思', at('99')), source_adapter=s.adapter)

    asyncio.run(run())
    assert 'original-request' in str(requests[-1]) and 'owner-quote' in str(requests[-1])
    assert '"senderId": "33"' in str(requests[-1])


def test_minimal_request_does_not_turn_bare_old_id_in_body_into_visible_candidate(meme_turn, monkeypatch):
    s = meme_turn
    thread = s.channels.ensure_channel_thread(s.role.id, 'qq', s.adapter.account_id, '100', 'group')
    _, old = s.channels.append_event(thread, direction='inbound', content='EXCLUDED_ORIGINAL_BODY',
        external_message_id='old', metadata={'senderId': '22', 'senderIsOwner': False})
    s.channels.append_event(thread, direction='outbound', content='旧答复', external_message_id='old-answer',
        role='assistant', metadata={'deliveryStatus': 'sent', 'receivingPlatformId': '99'})
    requests = []
    original = main._ordinary_context_provider

    def minimal(*args, **kwargs):
        provider = original(*args, **kwargs)
        provider.degraded = True  # Enter the real, existing minimal render path.
        return provider

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            assert 'EXCLUDED_ORIGINAL_BODY' not in str(messages)
            yield ModelToolCall('finish-' + str(len(requests)), 'finish_reply', {'messages': ['不可派发'],
                'response': {'message_id': old.id}})
            yield ModelResponseDone('tool_use')

    monkeypatch.setattr(main, '_ordinary_context_provider', minimal)
    monkeypatch.setattr(main, 'model_adapter', Model())
    text = json.dumps({'sourceMessageId': old.id}) + ' 这里只提到旧编号，原文未提供'
    asyncio.run(main._dispatch_channel_payload(s.adapter.account_id,
        group_message('current', '11', text, at('99')), source_adapter=s.adapter))
    assert len(requests) == 2
    assert old.id in str(requests[-1]) and 'EXCLUDED_ORIGINAL_BODY' not in str(requests[-1])
    assert not any(op == 'send_group_msg' for op, _ in s.operations)


@pytest.mark.parametrize('kind', ['image', 'unreadable-image', 'empty'])
def test_verified_attachment_or_empty_quote_can_be_selected_without_original_row(meme_turn, monkeypatch, kind):
    from test_meme_turn import image_bytes
    s = meme_turn
    s.raw = image_bytes('PNG')
    s.download_error = kind == 'unreadable-image'
    original = s.adapter._transport
    requests = []

    async def transport(operation, params):
        if operation == 'get_msg':
            segments = [] if kind == 'empty' else [{'type':'image','data':{'file':'quoted.png','url':'https://qq.test/image'}}]
            return {'data':{'group_id':100,'message_type':'group','sender':{'user_id':33,'nickname':'引用作者'},'message':segments}}
        return await original(operation, params)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            current = next(item for item in reversed(messages) if item['role']=='user')
            blocks = current['content'] if isinstance(current['content'], list) else [{'type':'text','text':current['content']}]
            text = '\n'.join(part['text'] for part in blocks if part['type']=='text')
            source = json.loads(next(line for line in text.splitlines() if line.startswith('[消息来源] ')).removeprefix('[消息来源] '))
            assert source['replyToSenderId'] == '33' and source['replyContextVerified'] is True
            assert any(part['type']=='image_url' for part in blocks) is (kind == 'image')
            if kind != 'unreadable-image':
                assert source['replyToContent'] == ''
            if kind == 'image':
                assert source['replyContentStatus'] == 'attachment_reference'
            if kind == 'empty':
                assert source['replyContentStatus'] == 'empty_or_unavailable'
            if kind == 'unreadable-image':
                assert '未读取图片内容' in text and source['replyAttachmentStatus'] == 'unreadable'
            yield ModelToolCall('finish', 'finish_reply', {'messages':['回应这条引用，不猜测缺失内容'],
                'response':{'message_id':'attachment-quote','quote_message_id':'attachment-quote','mention_ids':[]}})
            yield ModelResponseDone('tool_use')

    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(s.adapter, '_transport', transport)
    asyncio.run(main._dispatch_channel_payload(s.adapter.account_id,
        group_message('current-quote', '22', '请回应这条引用', at('99'),
            {'type':'reply','data':{'id':'attachment-quote'}}), source_adapter=s.adapter))
    sends = [params for op, params in s.operations if op=='send_group_msg']
    assert len(requests) == 1 and len(sends) == 1
    assert next(part['data']['id'] for part in sends[0]['message'] if part['type']=='reply') == 'attachment-quote'
    thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
    stored = s.sessions.list_messages(thread.session_key)
    assert len([item for item in stored if item.role=='user']) == 1
    assert stored[-1].metadata['responseRelation']['targetQuotedFromSourceMessageId'] == stored[0].id
    assert stored[-1].metadata['responseRelation']['targetSenderId'] == '33'


@pytest.mark.parametrize('display', ['default', 'empty', 'corrected', 'invalid-twice'])
def test_mentioned_only_logical_member_does_not_require_a_sendable_mention(meme_turn, monkeypatch, display):
    from backend.app.channels.models import TargetKind, target_from_chat
    s, requests = meme_turn, []

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            assert '"mentionedIds": ["99", "44"]' in str(messages)
            response = {'member_id': '44'}
            if display == 'empty' or display == 'corrected' and len(requests) == 2:
                response['mention_ids'] = []
            elif display in {'corrected', 'invalid-twice'}:
                response['mention_ids'] = ['44']
            if len(requests) == 2:
                assert {tool['function']['name'] for tool in tools} == {'finish_reply'}
                assert '只能提及该群目录已观察或发现的成员' in str(messages)
            yield ModelToolCall('finish-' + str(len(requests)), 'finish_reply', {
                'messages': ['回应提到的成员，不改变发送会话'], 'response': response})
            yield ModelResponseDone('tool_use')

    monkeypatch.setattr(main, 'model_adapter', Model())
    asyncio.run(main._dispatch_channel_payload(s.adapter.account_id,
        group_message('mentioned-request', '11', '请回应这位群友', at('99'), at('44')),
        source_adapter=s.adapter))
    assert len(requests) == (2 if display in {'corrected', 'invalid-twice'} else 1)
    sends = [params for op, params in s.operations if op == 'send_group_msg']
    assert len(sends) == (0 if display == 'invalid-twice' else 1)
    group = target_from_chat(account_id=s.adapter.account_id, chat_id='100', kind=TargetKind.GROUP)
    known = s.channels.get_target(s.role.id, s.adapter.account_id, group.id)
    assert '44' not in known.metadata.get('member_ids', [])
    assert not any(target.chat_id == '44' for target in s.channels.list_targets(s.role.id, s.adapter.account_id))
    if sends:
        assert sends[0]['group_id'] == 100
        assert not any(part['type'] == 'at' for part in sends[0]['message'])
        thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
        reply = s.sessions.list_messages(thread.session_key)[-1]
        assert reply.metadata['deliveryStatus'] == 'sent'
        assert reply.metadata['responseRelation']['triggerSenderId'] == '11'
        assert reply.metadata['responseRelation']['targetSenderId'] == '44'
        assert reply.metadata['responseRelation']['requestedMentionIds'] == []


@pytest.mark.parametrize('state', ['invalidated', 'completed'])
def test_selected_nontrigger_source_is_revalidated_at_actual_dispatch(meme_turn, monkeypatch, state):
    from backend.app.channels.conversation_context import current_timeline
    s, requests, changed = meme_turn, [], []
    original_wait = s.adapter.scheduler.wait

    async def wait(chat_id, kind, attempt, delay=0):
        if attempt.message.content == 'RESPONSE_TO_SELECTED_SOURCE' and not changed:
            thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
            target = next(item for item in s.sessions.list_messages(thread.session_key)
                if item.metadata.get('platformMessageId') == 'target-question')
            s.channels.set_channel_turn_state(thread, target.id, state)
            changed.append(target.id)
            assert any(item['id'] == target.id for item in current_timeline(s.channels, thread)) is (state != 'invalidated')
        await original_wait(chat_id, kind, attempt, delay)

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append(messages)
            if len(requests) == 1:
                yield ModelTextDelta('PREVIOUS_CONFIRMED_ANSWER')
            else:
                assert 'TARGET_SOURCE_QUESTION' in str(messages)
                yield ModelToolCall('finish', 'finish_reply', {'messages': ['RESPONSE_TO_SELECTED_SOURCE'],
                    'response': {'message_id': 'target-question', 'quote_message_id': None, 'mention_ids': []}})
                yield ModelResponseDone('tool_use')

    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(s.adapter.scheduler, 'wait', wait)

    async def run():
        await main._dispatch_channel_payload(s.adapter.account_id,
            group_message('target-question', '22', 'TARGET_SOURCE_QUESTION', at('99')), source_adapter=s.adapter)
        thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
        target = next(item for item in s.sessions.list_messages(thread.session_key)
            if item.metadata.get('platformMessageId') == 'target-question')
        s.channels.set_channel_turn_state(thread, target.id, 'queued')
        await main._dispatch_channel_payload(s.adapter.account_id,
            group_message('trigger-request', '11', '请解释前一位群友的问题', at('99')), source_adapter=s.adapter)

    asyncio.run(run())
    assert len(requests) == 2 and len(changed) == 1
    sends = [params for op, params in s.operations if op == 'send_group_msg']
    assert len(sends) == (1 if state == 'invalidated' else 2)
    thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
    replay = str(s.sessions.runtime_messages(thread.session_key))
    assert ('RESPONSE_TO_SELECTED_SOURCE' in replay) is (state != 'invalidated')
