"""Stored provenance reaches actual provider and maintenance requests."""
import asyncio
import json

import pytest

from backend.app import main
from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.ai.model_types import (ModelAssistantMessage, ModelStreamDone, ModelTextBlock,
    ModelThinkingBlock, ModelResponseMetadata)
from backend.app.channels.consolidation import ChannelMemoryMaintenance
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.model_adapter import ModelTextDelta
from test_meme_turn import meme_turn
from test_conversation_relations import group_message, at


@pytest.mark.parametrize('channel, expected_shared', [('qq', True), ('feishu', False), ('qqbot', False)])
def test_same_numeric_sender_names_and_ids_use_recorded_platform_or_application_scope(meme_turn, channel, expected_shared):
    s = meme_turn
    requests = []

    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools, **kwargs):
            requests.append(messages)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock('ok'),)))

    async def run():
        from test_host import MemoryCredentials
        s.channels.credentials = MemoryCredentials()
        for existing in s.channels.list_role_accounts(s.role.id):
            if existing.channel == channel: s.channels.delete_account(existing.account_id)
        for number in range(2):
            account = s.channels.register_channel_account(s.role.id, channel, account_id=f'{channel}-{number}',
                platform_id=f'application-{number}', enabled=True, allow_group=True)
            thread = s.channels.ensure_channel_thread(s.role.id, channel, account.account_id, '100', 'group')
            s.channels.append_event(thread, direction='inbound', content='同名成员发言', external_message_id='duplicate',
                metadata={'senderId': '42', 'senderName': '同名', 'senderIsOwner': False, 'receivingPlatformId': f'bot-{number}'})
            provider = MeidoProvider(Adapter(), s.role, None)
            async for _ in provider.stream_response(model='', system='', messages=s.sessions.runtime_messages(thread.session_key),
                    tools=(), signal=CancellationToken()): pass
            s.channels.delete_account(account.account_id)

    asyncio.run(run())
    sources = []
    for request in requests:
        text = next(block.text for message in request for block in message.content
            if isinstance(block, ModelTextBlock) and block.text.startswith('[消息来源] '))
        sources.append(json.loads(text.split('\n', 1)[0].removeprefix('[消息来源] ')))
    assert (sources[0]['senderKey'] == sources[1]['senderKey']) is expected_shared
    assert sources[0]['accountId'] != sources[1]['accountId']
    assert sources[0]['threadId'] != sources[1]['threadId']
    assert sources[0]['messageId'] == sources[1]['messageId'] == 'duplicate'


def test_legacy_unknown_source_and_signed_thinking_survive_actual_rich_request(meme_turn):
    s = meme_turn
    thread = s.channels.ensure_channel_thread(s.role.id, 'feishu',
        s.channels.register_channel_account(s.role.id, 'feishu', account_id='legacy', platform_id='new-app', enabled=True).account_id,
        'old-chat', 'private')
    # A legacy message cannot acquire the replacement account's identity.
    old = s.sessions.append_message(thread.session_key, 'user', '旧消息原文', metadata={
        'channel': 'feishu', 'threadId': thread.thread_id, 'senderId': '42', 'replyTo': 'expired'})
    thinking = {'type': 'thinking', 'text': 'opaque prior reasoning', 'signature': 'unchanged-signature', 'redacted': False}
    s.sessions.append_message(thread.session_key, 'assistant', '已发回复', metadata={
        'channel': 'feishu', 'threadId': thread.thread_id, 'deliveryStatus': 'sent',
        'mentionIds':['42'], 'responseRelation':{'requestedMentionIds':['42']},
        'content_blocks': [thinking, {'type': 'text', 'text': '已发回复'}]})
    requests = []

    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools, **kwargs):
            requests.extend(messages)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock('ok'),)))

    async def run():
        provider = MeidoProvider(Adapter(), s.role, None)
        async for _ in provider.stream_response(model='', system='', messages=s.sessions.runtime_messages(thread.session_key),
                tools=(), signal=CancellationToken()): pass

    asyncio.run(run())
    user_text = next(block.text for message in requests for block in message.content
        if isinstance(block, ModelTextBlock) and old.id in block.text)
    source = json.loads(user_text.split('\n', 1)[0].removeprefix('[消息来源] '))
    assert source['receivingPlatformId'] is None and source['senderIsOwner'] is None
    assert source['senderScope'] == 'legacy:' + thread.thread_id
    assert source['replyToSenderId'] is None and source['replyToSenderIdentity'] == '未知发送者'
    assistant = next(message for message in requests if isinstance(message, ModelAssistantMessage))
    assert isinstance(assistant.content[0], ModelThinkingBlock)
    assert assistant.content[0].signature == 'unchanged-signature'
    assert isinstance(assistant.content[1], ModelTextBlock) and assistant.content[1].text.startswith('[消息来源] ')
    own_source = json.loads(assistant.content[1].text.removeprefix('[消息来源] '))
    assert own_source['mentionedIds'] == [] and own_source['addressing'] == 'unknown'
    assert own_source['actualMentionIds'] == [] and own_source['displayEvidence'] == 'unknown'
    assert own_source['responseRelation']['requestedMentionIds'] == ['42']
    assert s.sessions.list_messages(thread.session_key)[-1].metadata['content_blocks'][0] == thinking


def test_actual_external_maintenance_request_keeps_sources_quotes_and_response_relations(meme_turn, monkeypatch):
    s = meme_turn

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            yield ModelTextDelta('确认给这位成员的回复')

    monkeypatch.setattr(main, 'model_adapter', Model())
    asyncio.run(main._dispatch_channel_payload(s.adapter.account_id,
        group_message('question', '22', '这次讨论的具体问题', at('99')), source_adapter=s.adapter))
    thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
    originals = s.sessions.list_messages(thread.session_key)
    payloads = []

    def complete(messages, configuration, **kwargs):
        payloads.append(json.loads(messages[1]['content']))
        return {'summary': '保留问题与角色回答的归属', 'groupNote': '', 'members': []}

    monkeypatch.setattr(main, '_complete_model_json', complete)
    worker = ChannelMemoryMaintenance(lambda: s.channels, lambda: MultichannelGroupRepository(s.channels),
        main._summarize_channel_memory, lambda *_: None)
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=False, through_sequence=originals[-1].sequence))
    payload = payloads[-1]
    user = next(item for item in payload['messages'] if item['direction'] == 'inbound')
    own = next(item for item in payload['messages'] if item['direction'] == 'outbound')
    assert user['sourceMessageId'] == originals[0].id and user['senderId'] == '22'
    assert own['responseRelation']['targetPlatformMessageId'] == 'question'
    assert own['senderId'] == '99' and own['deliveryStatus'] == 'sent'
    assert all(item['senderId'] == '22' and item['senderIsOwner'] is False for item in payload['memberFactsMessages'])
    assert s.sessions.list_messages(thread.session_key) == originals


def test_owner_statement_extractor_receives_only_owner_inputs_with_quote_attribution(meme_turn, monkeypatch):
    from datetime import datetime, timedelta
    s = meme_turn
    s.channels.set_group_listening(s.role.id, s.adapter.account_id, '100', True)
    thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
    yesterday = datetime.now().astimezone() - timedelta(days=1)
    s.channels.listening.hear(thread, '请讨论甲的安排', 'owner', {'senderId': '11', 'senderIsOwner': True,
        'receivingPlatformId': '99', 'mentionedIds': ['22'], 'replyTo': 'member',
        'replyContextVerified': True, 'replyToSenderId': '22', 'replyToSenderIsOwner': False,
        'replyToContent': '这是群友甲的安排'}, timestamp=yesterday)
    s.channels.listening.hear(thread, '群友自己的偏好', 'member', {'senderId': '22', 'senderIsOwner': False}, timestamp=yesterday)
    extracted = []

    def complete(messages, configuration, **kwargs):
        extracted.append(messages)
        return {'history_entries': [], 'pending_items': []}

    monkeypatch.setattr(main, '_complete_model_json', complete)

    async def owner_statements(_thread, messages):
        assert all(item['metadata']['senderIsOwner'] is True for item in messages)
        main._owner_channel_layer(s.role.id, messages)

    async def summarize(*_): return {'members': []}
    worker = ChannelMemoryMaintenance(lambda: s.channels, lambda: MultichannelGroupRepository(s.channels),
        summarize, lambda *_: None, owner_statements=owner_statements)
    assert asyncio.run(worker.consolidate(thread.thread_id, listening=True))
    text = str(extracted)
    assert '"replyToSenderId": "22"' in text and '"senderId": "11"' in text
    assert '群友自己的偏好' not in text


def test_telegram_selection_reaches_real_mentions_and_quote_fallback_keeps_logical_target(meme_turn, monkeypatch):
    from backend.app.channels.telegram import TelegramAccountAdapter
    from backend.app.channels.delivery import DeliveryRejected, OutboundScheduler
    from backend.app.channels.models import TargetKind, target_from_chat
    from backend.app.model_adapter import ModelToolCall, ModelResponseDone
    from test_host import MemoryCredentials
    s = meme_turn
    s.channels.credentials = MemoryCredentials()
    account = s.channels.save_account(s.role.id, token='synthetic', enabled=True, allow_group=True)
    account = s.channels.set_platform_identity(account.account_id, '99')
    thread = s.channels.ensure_channel_thread(s.role.id, 'telegram', account.account_id, '100', 'group')
    _, question = s.channels.append_event(thread, direction='inbound', content='另一个成员的问题', external_message_id='older',
        metadata={'senderId':'33', 'senderIsOwner':False, 'receivingPlatformId':'99'})
    _, current = s.channels.append_event(thread, direction='inbound', content='请回答那位成员', external_message_id='current',
        metadata={'senderId':'22', 'senderIsOwner':False, 'receivingPlatformId':'99', 'triggered':True})
    target = target_from_chat(account_id=account.account_id, chat_id='100', kind=TargetKind.GROUP,
        member_ids=['22','33'])
    s.channels.remember_targets(s.role.id, [target])
    wire = []

    class Transport:
        async def send_message(self, token, chat, text, **options):
            wire.append((chat, text, options))
            if len(wire) == 1: raise DeliveryRejected('missing quote', recovery='quote_missing')
            return {'message_id': 'confirmed-telegram'}

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            yield ModelToolCall('finish', 'finish_reply', {'messages':['这句回应群友'],
                'response':{'message_id':question.id, 'mention_ids':['33']}})
            yield ModelResponseDone('tool_use')

    adapter = TelegramAccountAdapter(account.account_id, lambda:'synthetic', Transport(), scheduler=OutboundScheduler())
    main.channel_host.register(adapter)
    main.channel_adapters[account.account_id] = adapter
    monkeypatch.setattr(main, 'model_adapter', Model())
    asyncio.run(main._handle_channel_message(thread, current.content, {**current.metadata, 'messageId':current.id}, False))
    assert len(wire) == 2
    assert wire[0][2]['reply_to'] == 'older' and wire[1][2]['reply_to'] is None
    assert '@33' in wire[1][1]
    assert any(entity['url'] == 'tg://user?id=33' for entity in wire[1][2]['entities'])
    reply = s.sessions.list_messages(thread.session_key)[-1]
    assert reply.metadata['responseRelation']['targetSourceMessageId'] == question.id
    assert reply.metadata['responseRelation']['requestedQuoteMessageId'] == 'older'
    shown = str(s.sessions.runtime_messages(thread.session_key)[-1].content)
    assert '"actualReplyTo": null' in shown and '"actualMentionIds": ["33"]' in shown


def test_channel_context_inspection_measures_separate_hearing_and_response_protocol(meme_turn, monkeypatch):
    from backend.app.context_inspection import _prepare
    from backend.app.models import ModelConfigurationInput
    s = meme_turn
    s.channels.set_group_listening(s.role.id, s.adapter.account_id, '100', True)
    thread = s.channels.get_thread_by_chat(s.role.id, s.adapter.account_id, '100', chat_type='group')
    s.channels.listening.hear(thread, 'INSPECT_HEARING', 'heard', {'senderId':'22', 'senderIsOwner':False,
        'mentionedIds':['33'], 'receivingPlatformId':'99'})
    main.model_configuration_store.save(ModelConfigurationInput(providerId='custom', provider='test',
        baseUrl='http://localhost', model='fixture', contextWindowTokens=32768))
    requests = []
    original = MeidoProvider.prepare_request

    def capture(provider, *args, **kwargs):
        value = original(provider, *args, **kwargs)
        requests.append(value)
        return value

    monkeypatch.setattr(MeidoProvider, 'prepare_request', capture)

    async def run():
        _, _, messages, capabilities, window, protocol, _ = await _prepare(main, s.role.id,
            compact=False, thread=thread, sender_id='11')
        try:
            value, _ = await window.inspect(messages, CancellationToken(), protocol_tools=protocol)
            assert value['inputTokens'] > 0
            assert 'response' in protocol[0]['function']['parameters']['properties']
        finally:
            await capabilities.close()

    asyncio.run(run())
    assert any('INSPECT_HEARING' in request.context and '"mentionedIds": ["33"]' in request.context for request in requests)


def test_unshown_business_exchange_keeps_empty_prose_and_signed_blocks_in_real_replay(meme_turn):
    from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage
    from backend.app.ai.model_types import ModelToolCallBlock
    s = meme_turn
    thread = s.channels.ensure_channel_thread(s.role.id, 'qq', s.adapter.account_id, '100', 'group')
    s.channels.append_event(thread, direction='inbound', content='当前真实请求', external_message_id='request',
        metadata={'senderId':'22', 'senderIsOwner':False})
    thinking = {'type':'thinking','text':'','signature':'redacted-signature','redacted':True}
    call = {'type':'tool_call','id':'signed','name':'lookup','arguments':{},
        'thought_signature':'required-signature','namespace':'functions'}
    s.sessions.append_agent_message(thread.session_key, AssistantMessage('', (ToolCall('signed','lookup'),),
        'tool_use', metadata={'channel':'qq','threadId':thread.thread_id, 'content_blocks':[thinking, call]}), run_id='business')
    s.sessions.append_agent_message(thread.session_key, ToolResultMessage('signed','lookup','真实工具结果'), run_id='business')
    requests = []

    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools, **kwargs):
            requests.extend(messages)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock('ok'),)))

    async def run():
        provider = MeidoProvider(Adapter(), s.role, None)
        async for _ in provider.stream_response(model='', system='', messages=s.sessions.runtime_messages(thread.session_key),
                tools=(), signal=CancellationToken()): pass

    asyncio.run(run())
    business = next(message for message in requests if isinstance(message, ModelAssistantMessage))
    assert [type(block) for block in business.content] == [ModelThinkingBlock, ModelToolCallBlock]
    assert business.content[0].signature == 'redacted-signature'
    assert business.content[1].thought_signature == 'required-signature'
    assert business.content[1].namespace == 'functions'
    assert not any(isinstance(block, ModelTextBlock) for block in business.content)


@pytest.mark.parametrize('origin', ['allowed', 'missing', 'other-chat'])
def test_private_memory_request_resolves_quotes_only_inside_its_admitted_window(meme_turn, monkeypatch, origin):
    s = meme_turn
    thread = s.channels.ensure_channel_thread(s.role.id, 'qq', s.adapter.account_id, '11', 'private')
    other = s.channels.ensure_channel_thread(s.role.id, 'qq', s.adapter.account_id, 'other', 'private')
    _, original = s.channels.append_event(thread if origin != 'other-chat' else other,
        direction='inbound', content='SAVED_ORIGINAL_BODY', external_message_id='origin',
        metadata={'senderId':'11', 'senderIsOwner':True})
    _, current = s.channels.append_event(thread, direction='inbound', content='CURRENT_PRIVATE_INPUT',
        external_message_id='current', metadata={'senderId':'11', 'senderIsOwner':True,
            'replyTo':'origin', 'replyContextVerified':True, 'replyToSenderId':'22',
            'replyToSenderIsOwner':False, 'replyToContent':'CACHED_PRIVATE_QUOTE_OUTSIDE_WINDOW'})
    _, reply = s.channels.append_event(thread, direction='outbound', content='CONFIRMED_ASSISTANT',
        external_message_id='reply', role='assistant', metadata={'deliveryStatus':'sent'})
    window = ([original] if origin != 'missing' else []) + [current, reply]
    requests = []

    def complete(messages, configuration, **kwargs):
        requests.append(messages)
        return {'history_entries':[], 'pending_items':[]}

    monkeypatch.setattr(main, '_complete_model_json', complete)
    monkeypatch.setattr(main, 'roles_root', s.roles.root)
    draft = main._consolidation_provider(f'consolidation:{s.role.id}:window', window)
    records = [json.loads(line) for line in draft['conversation'].splitlines() if line.startswith('{')]
    facts = next(item for item in records if item['sourceMessageId'] == current.id)
    assert 'CACHED_PRIVATE_QUOTE_OUTSIDE_WINDOW' not in str(requests)
    assert current.id in str(requests) and 'CURRENT_PRIVATE_INPUT' in str(requests)
    if origin == 'allowed':
        assert facts['replyToContent'] == 'SAVED_ORIGINAL_BODY' and facts['replyMissing'] is False
        assert facts['replyToSenderId'] == '11' and facts['replyToSenderIdentity'] == '已配对主人'
    else:
        assert facts['replyToContent'] == '' and facts['replyMissing'] is True
    assert s.sessions.list_messages(thread.session_key)[-2].metadata['replyToContent'] == 'CACHED_PRIVATE_QUOTE_OUTSIDE_WINDOW'
