"""A revocable, read-only excerpt of the owner's recent group statements."""
from datetime import datetime, timedelta, timezone
import json

from .channels.conversation_context import project_record
from .channels.delivery_projection import delivered_message
from .channels.source_authority import channel_owner_binding, source_messages
from .context_windows import _stamp


def _time(value):
    try:
        result = datetime.fromisoformat(str(value))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


class OwnerGroupSpeech:
    def __init__(self, history, snapshot=None):
        self.history = history
        self.channels = history.channels
        self.sources = snapshot if snapshot is not None else self._capture()

    def _records(self, thread, *, identifiers=None):
        from .conversation_history import _COLUMNS
        since = self.history.admitted_at - timedelta(hours=6)
        ids = tuple(identifiers or ())
        selected = ' AND message_id IN (' + ','.join('?' for _ in ids) + ')' if ids else ''
        with self.channels._connect() as db:
            rows = db.execute('SELECT ' + _COLUMNS + ' FROM messages WHERE session_key=? '
                'AND julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?)' + selected + ' ORDER BY sequence',
                (thread.session_key, since.isoformat(), self.history.admitted_at.isoformat(), *ids)).fetchall()
            heard = db.execute('SELECT * FROM channel_listening_records WHERE thread_id=? '
                'AND julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?)' + selected.replace('message_id', 'id') + ' ORDER BY sequence',
                (thread.thread_id, since.isoformat(), self.history.admitted_at.isoformat(), *ids)).fetchall()
        records = []
        for row in rows:
            message = delivered_message(self.channels.sessions._message(tuple(row)))
            if message is not None and message.status == 'completed' and message.messageType == 'text':
                # Natural execution keeps an audit copy of its hearing input.
                # Availability belongs to the original listening store/window.
                if message.role == 'user' and (message.metadata.get('listening') is True or message.id.startswith('listen:')):
                    continue
                if message.role == 'assistant' and not (message.metadata.get('deliveryStatus') == 'sent'
                        or message.metadata.get('deliveryProjection') is True):
                    continue
                records.append(message.model_dump(mode='json'))
        if thread.channel == 'qq' and self.channels.group_listening(thread.role_id, thread.account_id, thread.chat_id):
            records.extend(self.channels.listening._message(row) for row in heard)
            records.extend(self.channels.listening.temporary(thread.role_id, thread.thread_id))
        return [record for record in records if (identifiers is None or record['id'] in identifiers)
            and (moment := _time(record.get('createdAt'))) is not None
            and since <= moment <= self.history.admitted_at
            and record.get('metadata', {}).get('channelTurnState') != 'invalidated']

    def _account(self, thread):
        if thread is None:
            return None
        account = self.channels.get_account(thread.account_id)
        if (account is None or account.role_id != self.history.role_id or not account.allow_group
                or not self.channels.account_effectively_enabled(account)):
            return None
        return account

    def _listening_epoch(self, thread):
        with self.channels._connect() as db:
            row = db.execute('SELECT COALESCE(MAX(rowid),0) FROM channel_listening_toggles WHERE thread_id=?',
                (thread.thread_id,)).fetchone()
        return [self.channels.listening.revision(thread.thread_id), int(row[0])]

    def _owner(self, thread, account, record):
        metadata = record['metadata']
        sender = str(metadata.get('senderId') or '')
        binding = channel_owner_binding(self.channels, thread.channel, thread.account_id, sender)
        received = _time(record['createdAt'])
        paired = _time(binding['pairedAt']) if binding else None
        created = _time(account.created_at)
        if (record['role'] != 'user' or metadata.get('senderIsOwner') is not True
                or not binding or sender in account.blocked_senders
                or received is None or paired is None or created is None or received < max(paired, created)):
            return None
        return {'identityId': binding['identityId'], 'pairedAt': binding['pairedAt'], 'senderId': sender}

    def _replies(self, records):
        by_source, by_run = {}, {}
        for reply in records:
            if reply['role'] != 'assistant' or not reply['content']:
                continue
            relation = reply['metadata'].get('responseRelation')
            relation = relation if isinstance(relation, dict) else {}
            sources = {relation.get('triggerSourceMessageId'), relation.get('targetSourceMessageId')}
            if reply.get('runId'):
                by_run.setdefault(reply['runId'], []).append(reply)
                run = self.channels.sessions.get_run(reply['runId'])
                if run is not None:
                    sources.update([run.modelSnapshot.get('currentInputId'), run.modelSnapshot.get('inputSourceMessageId')])
            for source in sources - {None}:
                by_source.setdefault(source, []).append(reply)
        return by_source, by_run

    def _capture(self):
        candidates = []
        with self.channels._connect() as db:
            groups = db.execute("SELECT thread_id FROM channel_threads WHERE role_id=? AND chat_type='group'",
                (self.history.role_id,)).fetchall()
        for row in groups:
            thread = self.channels.get_thread(self.history.role_id, row[0])
            account = self._account(thread) if thread else None
            if account is None:
                continue
            records = self._records(thread)
            by_source, by_run = self._replies(records)
            for record in records:
                binding = self._owner(thread, account, record)
                if binding is None:
                    continue
                replies, length = [], 0
                related = {reply['id']: reply for reply in [*by_source.get(record['id'], []), *by_run.get(record.get('runId'), [])]}
                for reply in sorted(related.values(), key=lambda item: (_time(item['createdAt']), item['id'])):
                    if length < 200:
                        replies.append(reply)
                        length += len(reply['content'])
                selected = [record, *replies]
                candidates.append({'threadId': thread.thread_id, 'messageId': record['id'],
                    'replyIds': [reply['id'] for reply in replies], 'timestamp': record['createdAt'],
                    'platformMessageId': record['metadata'].get('platformMessageId'),
                    'account': self.history._account(thread.account_id), 'binding': binding,
                    'listeningEpoch': self._listening_epoch(thread),
                    'fingerprint': _stamp(source_messages(selected))})
        candidates.sort(key=lambda item: (_time(item['timestamp']), item['messageId']), reverse=True)
        unique = {}
        for item in candidates:
            key = (item['threadId'], item['platformMessageId'] or item['messageId'])
            unique.setdefault(key, item)
        return list(reversed(list(unique.values())[:10]))

    def require_current(self):
        for source in self.sources:
            thread = self.channels.get_thread(self.history.role_id, source['threadId'])
            account = self._account(thread) if thread else None
            current_account = self.history._account(thread.account_id) if account else None
            # Initial host discovery fills a previously unknown platform ID,
            # as bind_channel_history permits for the same account instance.
            if current_account is not None and source['account']['platformId'] is None:
                current_account = {**current_account, 'platformId': None}
            if (account is None or current_account != source['account']
                    or self._listening_epoch(thread) != source['listeningEpoch']):
                raise PermissionError('主人近期群发言来源已失效')
            binding = channel_owner_binding(self.channels, thread.channel, thread.account_id, source['binding']['senderId'])
            if binding is None or any(binding[key] != source['binding'][key] for key in ('identityId', 'pairedAt')):
                raise PermissionError('主人近期群发言身份已变化')

    def read(self):
        self.require_current()
        result = []
        for source in self.sources:
            thread = self.channels.get_thread(self.history.role_id, source['threadId'])
            account = self._account(thread)
            if account is None:
                raise PermissionError('主人近期群发言来源已失效')
            ids = [source['messageId'], *source['replyIds']]
            records = {record['id']: record for record in self._records(thread, identifiers=ids)}
            if any(identifier not in records for identifier in ids):
                raise PermissionError('主人近期群发言原文已缺失')
            statement = records[source['messageId']]
            if self._owner(thread, account, statement) != source['binding']:
                raise PermissionError('主人近期群发言身份已变化')
            if _stamp(source_messages([records[identifier] for identifier in ids])) != source['fingerprint']:
                raise PermissionError('主人近期群发言在本轮中已变化')
            projection = project_record(statement)
            result.append({'group': thread.title or thread.chat_id, 'time': source['timestamp'],
                'threadId': thread.thread_id, 'sourceMessageId': source['messageId'],
                'senderId': projection['senderId'], 'text': statement['content'][:200],
                'mentionedIds': projection['mentionedIds'],
                'quote': {'messageId': projection['replyTo'], 'senderId': projection['replyToSenderId'],
                    'senderName': projection['replyToSenderName'], 'identity': projection['replyToSenderIdentity']},
                'reply': ''.join(records[identifier]['content'] for identifier in source['replyIds'])[:200],
                'replySources': [{'sourceMessageId': identifier,
                    'responseRelation': records[identifier]['metadata'].get('responseRelation')}
                    for identifier in source['replyIds']]})
        self.require_current()
        return result

    def blocks(self):
        rows = self.read()
        return ['[主人近期群发言]\n以下是带原始来源的只读背景，引用作者与提及对象各自独立，执行权限沿用本轮实际来源。\n'
            + json.dumps(rows, ensure_ascii=False)] if rows else []
