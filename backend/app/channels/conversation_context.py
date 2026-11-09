"""Recorded conversation facts and bounded, same-chat response choices.

This projection reads immutable ingress/receipt facts, never current pairing or
account aliases. Authorization remains owned by the existing source leases.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from collections.abc import Mapping

from .external_members import identity_scope, member_key

PROJECTION_VERSION = 1
SOURCE_MARKER = '[消息来源] '
RESPONSE_PROMPT = (
    '[群内应答关系]\n消息来源说明谁说、在哪、真实 @ 谁、引用谁；user 协议角色不等于主人在向你下任务。'
    '先结合当前请求和上下文理解谁在跟谁说话，再决定怎么回、回应谁。'
    '主人 @ 你也可能请你回应别的群友；回应对象可以是当前可见的其他消息、成员或整体话题。'
    '明确指向他人的话不能自动当作对你说；可以适当接话、澄清，不能冒充被问的人。'
    '通过 finish_reply 的可选 response 表达选择：message_id 使用已展示消息的 sourceMessageId 或 messageId，'
    'member_id 使用本群已展示成员 ID，topic_message_ids 为相关消息，intent 为一句交流意图。'
    'quote_message_id 与 mention_ids 只控制显示引用和 @，与逻辑回应对象分别选择；null 和空列表表示明确不引用、不 @。'
    '不填显示方式时沿用默认；选择他人不会改变本轮真实发言人、工具权限或发送会话。'
    '历史 selectionOrigin=default 只记录缺省关联与显示策略，不是对旧回复正文语义的保证。'
    '空引用正文不表示引用作者未知；attachment_reference 只说明有附件引用，可读性以实际图片或读取结果为准。'
)


def _id(value):
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) else None


def _ids(value):
    return list(dict.fromkeys(item for raw in value if (item := _id(raw)))) if isinstance(value, (list, tuple)) else []


def message_source(metadata: Mapping, *, source_id=None, created_at=None, session_key=None, role='user'):
    """One reader for new flat metadata and legacy records with unknown facts."""
    meta = metadata
    channel, thread_id = _id(meta.get('channel')), _id(meta.get('threadId'))
    own_id = _id(meta.get('receivingPlatformId'))
    if channel not in {'qq', 'telegram'} and meta.get('receivingPlatformIdentityVerified') is not True:
        own_id = None
    # Only a saved account snapshot proves a historical platform identity.
    account = meta.get('viaAccount')
    if own_id is None and channel in {'qq', 'telegram'} and isinstance(account, Mapping):
        candidate = _id(account.get('platformAccountId'))
        if candidate != _id(account.get('accountId')):
            own_id = candidate
    outgoing = role == 'assistant' or meta.get('direction') == 'outbound'
    display = actual_display(meta) if outgoing else {}
    sender_id = own_id if outgoing else _id(meta.get('senderId'))
    scope = identity_scope(channel or '', thread_id or session_key or 'unknown', dict(meta)) if channel else None
    mentions = _ids(display.get('actualMentionIds') if outgoing else meta.get('mentionedIds'))
    relation = meta.get('responseRelation') if isinstance(meta.get('responseRelation'), dict) else {}
    quote_id = _id(display.get('actualReplyTo')) if outgoing else _id(meta.get('replyTo'))
    verified = bool(quote_id and relation.get('requestedQuoteSenderId')) if outgoing else meta.get('replyContextVerified') is True
    quote_sender = _id(relation.get('requestedQuoteSenderId')) if outgoing and verified else _id(meta.get('replyToSenderId')) if verified else None
    directed = set(mentions)
    if quote_sender:
        directed.add(quote_sender)
    if own_id and directed:
        addressing = 'mixed' if own_id in directed and directed - {own_id} else 'role' if own_id in directed else 'other'
    else:
        addressing = 'unknown'
    quote_owner = relation.get('requestedQuoteSenderIsOwner') if outgoing else meta.get('replyToSenderIsOwner')
    quote_identity = ('角色本人' if quote_sender and quote_sender == own_id else
        '已配对主人' if quote_sender and quote_owner is True else
        '未配对成员' if quote_sender and quote_owner is False else '成员（主人身份未知）' if quote_sender else '未知发送者')
    quoted_text = str(meta.get('replyToContent') or '') if verified else ''
    has_quoted_attachments = bool(meta.get('quotedAttachments') or meta.get('replyQuoteAttachmentsAvailable'))
    content_status = ('none' if not quote_id else 'unavailable' if meta.get('replyOriginalVisible') is False else
        'text' if quoted_text else 'attachment_reference' if has_quoted_attachments else
        'empty_or_unavailable' if verified else 'unknown')
    return {
        'projectionVersion': PROJECTION_VERSION,
        'roleId': _id(meta.get('roleId')), 'channel': channel,
        'accountId': _id(meta.get('accountId')), 'receivingPlatformId': own_id,
        'receivingApplicationId': (_id(account.get('platformAccountId')) if isinstance(account, Mapping)
            and channel not in {'qq', 'telegram'} and account.get('platformAccountId') != account.get('accountId') else None),
        'threadId': thread_id, 'sessionKey': session_key or _id(meta.get('sourceSessionKey')),
        'chatId': _id(meta.get('chatId')), 'chatType': _id(meta.get('chatType')),
        'messageThreadId': meta.get('messageThreadId'),
        'sourceMessageId': _id(source_id), 'messageId': _id(meta.get('platformMessageId')),
        'createdAt': created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        'direction': 'outbound' if outgoing else _id(meta.get('direction')),
        'sourceKind': 'role_sent' if outgoing else 'quoted' if meta.get('quotedFromSourceMessageId') else 'listening' if meta.get('listening') is True else 'explicit' if meta.get('triggered') is True else 'unknown',
        'quotedFromSourceMessageId': _id(meta.get('quotedFromSourceMessageId')),
        'senderScope': scope, 'senderKey': member_key(channel, scope, sender_id) if channel and scope and sender_id else None,
        'senderId': sender_id, 'senderName': meta.get('senderName') if not outgoing else meta.get('roleName'),
        'senderIsOwner': False if outgoing else meta.get('senderIsOwner') if isinstance(meta.get('senderIsOwner'), bool) else None,
        'mentionedIds': mentions, 'addressing': addressing,
        'replyTo': quote_id, 'replyContextVerified': verified,
        'replyToSenderId': quote_sender, 'replyToSenderName': meta.get('replyToSenderName') if verified and not outgoing else relation.get('requestedQuoteSenderName') if verified else None,
        'replyToSenderIdentity': quote_identity,
        'replyToContent': quoted_text,
        'replyMissing': bool(quote_id and not quoted_text),
        'replyContentStatus': content_status,
        'replyHasAttachments': has_quoted_attachments,
        'replyAttachmentStatus': ('unreadable' if meta.get('quotedAttachmentFailures') else
            'reference' if has_quoted_attachments else 'not_reported'),
        'responseRelation': meta.get('responseRelation') if isinstance(meta.get('responseRelation'), dict) else None,
        'deliveryStatus': meta.get('deliveryStatus') if outgoing else None,
        'actualReplyTo': display.get('actualReplyTo'),
        'actualMentionIds': display.get('actualMentionIds', []),
        'displayEvidence': display.get('displayEvidence'),
    }


def project_record(record):
    if not isinstance(record, Mapping):
        record = record.model_dump(mode='json')
    return {**message_source(record.get('metadata') or {}, source_id=record.get('id'),
        created_at=record.get('createdAt'), session_key=record.get('sessionKey'), role=record.get('role', 'user')),
        'runId': record.get('runId') or (record.get('metadata') or {}).get('sourceRunId'),
        'text': str(record.get('content') or '')}


def render_record(record, *, text_limit=None):
    projected = project_record(record)
    if text_limit is not None:
        projected['text'] = projected['text'][:text_limit]
        projected['replyToContent'] = projected['replyToContent'][:text_limit]
    return json.dumps(projected, ensure_ascii=False)


def source_header(stored):
    return SOURCE_MARKER + json.dumps(message_source(stored.metadata, source_id=stored.id,
        created_at=stored.createdAt, session_key=stored.sessionKey, role=stored.role), ensure_ascii=False)


def with_source_block(blocks, text):
    """Keep leading signed thinking blocks at their provider-required position."""
    index = 0
    while index < len(blocks) and isinstance(blocks[index], dict) and blocks[index].get('type') in {'thinking', 'redacted_thinking'}:
        index += 1
    return [*blocks[:index], {'type': 'text', 'text': text}, *blocks[index:]]


def memory_records(messages):
    """Use only the consumer's admitted window, including private quote lookup.

    Cached private quotes cannot pull in raw text or attachments beyond these
    sources. This performs no storage read and never enlarges the window.
    """
    from ..models import Message
    from ..conversation_history import _quote_lookup, _user_projection
    from .delivery_projection import delivered_message
    records = []
    for item in messages:
        record = dict(item) if isinstance(item, Mapping) else item.model_dump(mode='json')
        if record.get('role') == 'assistant' and record.get('metadata', {}).get('channel') not in {None, '', 'desktop'}:
            projected = delivered_message(Message.model_validate(record))
            if projected is None or (projected.metadata.get('deliveryStatus') != 'sent'
                    and projected.metadata.get('deliveryProjection') is not True):
                continue
            record = projected.model_dump(mode='json')
        records.append(record)
    originals = [Message.model_validate(record) for record in records
        if record.get('sessionKey') and isinstance(record.get('sequence'), int)]
    quotes = _quote_lookup(originals)
    result = []
    for record in records:
        meta = record.get('metadata') or {}
        if (record.get('role') == 'user' and record.get('sessionKey')
                and meta.get('channel') not in {None, '', 'desktop'}
                and meta.get('chatType') != 'group' and meta.get('listening') is not True):
            message = Message.model_validate(record)
            content, metadata = _user_projection(message, quotes)
            record = {**record, 'content':content, 'metadata':metadata}
        result.append(record)
    return result


def memory_conversation(messages):
    lines = []
    for record in memory_records(messages):
        if record.get('metadata', {}).get('channel') not in {None, '', 'desktop'}:
            lines.append(render_record(record))
        else:
            lines.append(f"[{record.get('createdAt')}] {record.get('role', '').upper()}: {record.get('content', '')}")
    return '\n'.join(lines)


def record_fingerprint(record):
    return hashlib.sha256(render_record(record).encode()).hexdigest()


def current_timeline(channels, thread, *, through_message_id=None, inputs=(), limit=30):
    """Only this original thread. Hearing overflow remains in its recent window."""
    from .delivery_projection import delivered_message
    records = []
    for item in channels.sessions.list_messages(thread.session_key):
        if item.metadata.get('channelTurnState') == 'invalidated':
            continue
        delivered = delivered_message(item)
        if (delivered is not None and delivered.messageType == 'text' and delivered.status == 'completed'
                and (delivered.role != 'assistant' or delivered.metadata.get('deliveryStatus') == 'sent'
                     or delivered.metadata.get('deliveryProjection') is True)):
            records.append(delivered.model_dump(mode='json'))
        if through_message_id and item.id == through_message_id:
            break
    if thread.channel == 'qq' and thread.chat_type == 'group':
        records.extend(channels.listening.recent(thread.role_id, thread.thread_id))
    # The key always includes the receiving scope, never a bare platform ID.
    found = {}
    for record in records:
        meta = record['metadata']
        platform_id = _id(meta.get('platformMessageId'))
        key = (meta.get('channel'), meta.get('accountId'), meta.get('threadId'), platform_id or record['id'])
        found.setdefault(key, record)
    for item in inputs:
        key = (thread.channel, thread.account_id, thread.thread_id, item.message_id)
        if key not in found:
            found[key] = {'id': 'input:' + item.message_id, 'role': 'user', 'content': item.content,
                'createdAt': item.metadata.get('createdAt'), 'metadata': dict(item.metadata)}
    def order(row):
        stamp = row.get('createdAt')
        try:
            moment = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
            instant = moment.astimezone(timezone.utc).timestamp() if isinstance(moment, datetime) and moment.tzinfo else 0.
        except (ValueError, OverflowError):
            instant = 0.
        received = row['metadata'].get('hostReceivedAt')
        received = float(received) if isinstance(received, (int, float)) and not isinstance(received, bool) else 0.
        return instant, received
    # Python's stable sort preserves the existing host sequence for equal
    # instants. UUIDs are identities, never conversation ordering evidence.
    return sorted(found.values(), key=order)[-limit:]


def hearing_block(records, *, budget=6000):
    """Complete relation envelopes are omitted as a unit; never cut an ID header."""
    lines = []
    for record in reversed(records):
        line = render_record(record, text_limit=500)
        if len(line) + 1 > budget:
            continue
        lines.append(line)
        budget -= len(line) + 1
    return ('[当前群背景；旁听未触发角色，不是当前请求]\n' + '\n'.join(reversed(lines))) if lines else ''


def response_schema():
    nullable_id = {'type': ['string', 'null']}
    return {'type': 'object', 'properties': {
        'message_id': nullable_id, 'member_id': nullable_id,
        'topic_message_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 12},
        'topic_terms': {'type': 'array', 'items': {'type': 'string', 'minLength': 2, 'maxLength': 40}, 'maxItems': 8},
        'intent': {'type': 'string', 'maxLength': 300},
        'quote_message_id': nullable_id,
        'mention_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 20},
    }, 'additionalProperties': False}


@dataclass(frozen=True)
class ResponseSelection:
    relation: dict
    quote_id: str | None
    mention_ids: tuple[str, ...]
    source_ids: tuple[str, ...]


class ResponseContext:
    """A per-run catalog; choices never become sender identity or tool authority."""
    def __init__(self, channels, thread, records, *, trigger_id=None, natural=False):
        self.channels, self.thread = channels, thread
        self.records = {str(item['id']): deepcopy(item) for item in records}
        self.quoted_sources = {}
        for source_id, record in tuple(self.records.items()):
            meta = record['metadata']
            if not (meta.get('replyContextVerified') is True and meta.get('replyTo')
                    and meta.get('replyToSenderId')):
                continue
            # A platform-verified quote can be visible without an original row.
            # It is a dependent candidate, never a fabricated transcript entry.
            if any(identifier not in self.quoted_sources and str(item['metadata'].get('platformMessageId') or '') == str(meta['replyTo'])
                   for identifier, item in self.records.items()):
                continue
            quote_id = 'quoted:' + source_id + ':' + str(meta['replyTo'])
            self.records[quote_id] = {'id': quote_id, 'role': 'user', 'content': str(meta.get('replyToContent') or ''),
                'metadata': {**{key: meta.get(key) for key in ('roleId', 'channel', 'accountId', 'threadId', 'chatId', 'chatType', 'messageThreadId', 'viaAccount', 'receivingPlatformId', 'receivingPlatformIdentityVerified')},
                    'direction': 'inbound', 'platformMessageId': str(meta['replyTo']),
                    'senderId': str(meta['replyToSenderId']), 'senderName': meta.get('replyToSenderName'),
                    'senderIsOwner': meta.get('replyToSenderIsOwner'), 'quotedFromSourceMessageId': source_id}}
            self.quoted_sources[quote_id] = source_id
        self.trigger_id, self.natural = trigger_id, natural
        self.visible = set()
        self.selection = None

    def observe_request(self, request, messages=()):
        # Only identifiers in the actual request count, including minimal and
        # window rerenders. A previous request remains seen within this run.
        self.visible.update(identifier for message in messages for identifier in message.source_message_ids if identifier in self.records)
        self.visible.update(source_id for source_id, record in self.records.items()
            if source_id not in self.quoted_sources and render_record(record, text_limit=500) in request.context)
        self.visible.update(quote_id for quote_id, parent in self.quoted_sources.items() if parent in self.visible)

    def _message(self, identifier):
        if not isinstance(identifier, str) or not identifier:
            raise ValueError('回应消息编号无效')
        candidates = [record for source_id, record in self.records.items() if source_id in self.visible
            and (source_id == identifier or str(record['metadata'].get('platformMessageId') or '') == identifier)]
        if len(candidates) > 1 and len({(project_record(item)['senderKey'], item['content']) for item in candidates}) == 1:
            candidates = candidates[-1:]
        if len(candidates) != 1:
            raise ValueError('回应消息不在本轮已展示的当前会话中')
        return candidates[0]

    def choose(self, value=None, *, require_intent=False):
        origin = 'default' if value is None else 'model'
        if value is None:
            value = {'message_id': self.trigger_id} if self.trigger_id else {}
        if not isinstance(value, dict) or set(value) - set(response_schema()['properties']):
            raise ValueError('回应关系字段无效')
        if any(value.get(key) is not None and (not isinstance(value[key], str) or not value[key])
               for key in ('message_id', 'member_id', 'quote_message_id')):
            raise ValueError('回应关系编号无效')
        intent = value.get('intent', '')
        if not isinstance(intent, str) or len(intent) > 300 or require_intent and not intent.strip():
            raise ValueError('自然回应需要简短交流意图')
        topics = value.get('topic_message_ids', [])
        terms = value.get('topic_terms', [])
        mentions = value.get('mention_ids', [])
        if (not isinstance(topics, list) or len(topics) > 12 or any(not isinstance(item, str) or not item for item in topics)
                or not isinstance(mentions, list) or len(mentions) > 20 or any(not isinstance(item, str) or not item for item in mentions)):
            raise ValueError('回应关系列表无效')
        if not isinstance(terms, list) or len(terms) > 8 or any(not isinstance(item, str) or not 2 <= len(item) <= 40 for item in terms):
            raise ValueError('话题关键词无效')
        target = self._message(value['message_id']) if value.get('message_id') else None
        related = [self._message(item) for item in topics]
        observed_members = set()
        member_sources = {}
        for source_id in self.records:
            if source_id not in self.visible: continue
            source = project_record(self.records[source_id])
            for identifier in _ids([source['senderId'], *source['mentionedIds'], source['replyToSenderId']]):
                observed_members.add(identifier)
                member_sources.setdefault(identifier, source_id)
        member = value.get('member_id') or (project_record(target)['senderId'] if target else None)
        if member and member not in observed_members:
            raise ValueError('回应成员不在本轮当前会话中')
        if any(item not in observed_members for item in mentions):
            raise ValueError('提及成员不在本轮当前会话中')
        if require_intent and target is None and not related:
            raise ValueError('自然话题需要可回查的消息依据')
        quote = self._message(value['quote_message_id']) if value.get('quote_message_id') else None
        if 'quote_message_id' not in value and not self.natural:
            quote = target
        quote_id = _id(quote['metadata'].get('platformMessageId')) if quote else None
        for record in [target, *related, quote]:
            if record is None: continue
            source = project_record(record)
            for identifier in _ids([source['senderId'], *source['mentionedIds'], source['replyToSenderId']]):
                member_sources[identifier] = record['id']
        if 'mention_ids' not in value:
            mentions = [member] if member and not self.natural and self.thread.channel == 'qq' and self.thread.chat_type == 'group' else []
        source_ids = tuple(dict.fromkeys(self.quoted_sources.get(identifier, identifier) for identifier in [*([self.trigger_id] if self.trigger_id else []),
            *(item['id'] for item in [target, *related, quote] if item),
            *(member_sources[item] for item in [member, *mentions] if item)]))
        trigger = self.records.get(self.trigger_id)
        selection = ResponseSelection({
            'selectionOrigin': origin,
            'triggerSourceMessageId': self.trigger_id,
            'triggerPlatformMessageId': _id(trigger['metadata'].get('platformMessageId')) if trigger else None,
            'triggerSenderId': project_record(trigger)['senderId'] if trigger else None,
            'targetSourceMessageId': target['id'] if target else None,
            'targetPlatformMessageId': _id(target['metadata'].get('platformMessageId')) if target else None,
            'targetQuotedFromSourceMessageId': self.quoted_sources.get(target['id']) if target else None,
            'targetSenderId': member,
            'topicSourceMessageIds': list(dict.fromkeys(item['id'] for item in related)),
            'topicTerms': list(dict.fromkeys(terms)),
            'intent': intent, 'requestedQuoteMessageId': quote_id, 'requestedMentionIds': list(dict.fromkeys(mentions)),
            'requestedQuoteSourceMessageId': quote['id'] if quote else None,
            'requestedQuoteSenderId': project_record(quote)['senderId'] if quote else None,
            'requestedQuoteSenderName': project_record(quote)['senderName'] if quote else None,
            'requestedQuoteSenderIsOwner': project_record(quote)['senderIsOwner'] if quote else None,
            'channel': self.thread.channel, 'accountId': self.thread.account_id, 'threadId': self.thread.thread_id,
        }, quote_id, tuple(dict.fromkeys(mentions)), source_ids)
        self.selection = selection
        return selection

    def valid(self, selection=None):
        selection = selection or self.selection
        if selection is None:
            return False
        from .delivery_projection import delivered_message
        current = {}
        for original in self.channels.sessions.list_messages(self.thread.session_key):
            item = delivered_message(original)
            if item is not None and (item.role != 'assistant' or item.metadata.get('deliveryStatus') == 'sent'
                                    or item.metadata.get('deliveryProjection') is True):
                current[item.id] = item.model_dump(mode='json')
        if self.thread.channel == 'qq' and self.thread.chat_type == 'group':
            current.update((item['id'], item) for item in self.channels.listening.recent(self.thread.role_id, self.thread.thread_id))
        return all(source_id in current and record_fingerprint(current[source_id]) == record_fingerprint(self.records[source_id])
                   for source_id in selection.source_ids)


def actual_display(metadata):
    """Project confirmed platform display, including quote-missing fallback."""
    from .delivery_projection import confirmed_parts
    requested = metadata.get('responseRelation') or {}
    quote = requested.get('requestedQuoteMessageId', metadata.get('replyTo'))
    mentions = _ids(requested.get('requestedMentionIds', metadata.get('mentionIds')))
    if isinstance(metadata.get('parts'), list):
        parts = confirmed_parts(metadata)
        shown = bool(parts)
        quote = quote if any(not part.get('withoutReply') for part in parts) else None
    else:
        # A legacy/synthetic success without part details proves the content,
        # not how that platform displayed a quote or mention.
        shown = False
    if metadata.get('channel') == 'qq' and metadata.get('chatType') != 'group' or metadata.get('channel') in {'qqbot', 'grok_bot'}:
        quote = None
    return {'actualReplyTo': quote if shown else None, 'actualMentionIds': mentions if shown else [],
        'displayEvidence': 'confirmed_parts' if shown else 'unknown'}
