"""Host-owned human origin; request bodies cannot manufacture this capability."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..agent_runtime.tools import ToolContext
from ..channels.source_authority import FrozenChannelSource, capture_channel_source
from ..task_runtime import TaskOrigin
from .ledger import canonical


@dataclass(frozen=True)
class GenerationContext:
    origin: TaskOrigin
    human_key: str
    request_key: str
    attachment_ids: frozenset[str]
    channel_source: FrozenChannelSource | None = None
    channel_revision: tuple[int, int] | None = None
    ui_action: bool = False
    reply_to_message_id: str | None = None
    message_thread_id: int | None = None
    mention_ids: tuple[str, ...] = ()
    scene_revision: int | None = None
    scene_model_id: str | None = None
    scene_model_epoch: int = 0
    scene_tag_revision: int = 0

    @property
    def source_scope(self):
        return self.origin.source_scope_id or 'desktop'

    def to_dict(self):
        value = {'origin': self.origin.to_dict(), 'humanKey': self.human_key, 'requestKey': self.request_key,
                'attachmentIds': sorted(self.attachment_ids),
                'channelRevision': list(self.channel_revision) if self.channel_revision is not None else None,
                'uiAction': self.ui_action}
        if self.reply_to_message_id is not None or self.message_thread_id is not None or self.mention_ids:
            value['deliveryContext'] = {'replyToMessageId': self.reply_to_message_id,
                'messageThreadId': self.message_thread_id, 'mentionIds': list(self.mention_ids)}
        if self.scene_revision is not None:
            value['scene'] = {'revision': self.scene_revision, 'modelId': self.scene_model_id,
                              'modelEpoch': self.scene_model_epoch, 'tagRevision': self.scene_tag_revision}
        return value

    @classmethod
    def from_dict(cls, value):
        raw = value['origin']
        origin = TaskOrigin(raw['roleId'], raw['sessionKey'], raw.get('parentRunId', ''),
            raw.get('toolCallId'), raw.get('capabilityVersion', ''), raw.get('sourceThreadId', ''),
            raw.get('sourceAccountId', ''), raw.get('sourceScopeId', ''),
            canonical(raw['sourceAuthority']) if raw.get('sourceAuthority') is not None else '')
        source = FrozenChannelSource.from_dict(raw['sourceAuthority']) if raw.get('sourceAuthority') is not None else None
        delivery = value.get('deliveryContext', {})
        return cls(origin, value['humanKey'], value['requestKey'], frozenset(value['attachmentIds']), source,
                   tuple(value['channelRevision']) if value.get('channelRevision') is not None else None,
                   value.get('uiAction', False), delivery.get('replyToMessageId'),
                   delivery.get('messageThreadId'), tuple(delivery.get('mentionIds', ())),
                   value.get('scene', {}).get('revision'), value.get('scene', {}).get('modelId'),
                   value.get('scene', {}).get('modelEpoch', 0), value.get('scene', {}).get('tagRevision', 0))

    def denial(self, channels):
        if self.channel_source is not None:
            denied = self.channel_source.authorization_denial(channels)
            if denied:
                return denied
            if tuple(channels.directory_revision(self.channel_source.account_id, self.channel_source.channel)) != self.channel_revision:
                return 'source channel configuration changed'
        return None


def capture_tool_context(sessions, channels, context: ToolContext, *, role_id: str, is_owner: bool,
                         source_thread_id=None, source_account_id=None, _completed=False) -> GenerationContext:
    context.signal.raise_if_cancelled()
    run, user = sessions.get_run(context.run_id), sessions.user_message_for_run(context.run_id)
    if (not is_owner or context.role_id != role_id or not context.tool_call_id
            or run is None or run.roleId != role_id or run.sessionKey != context.session_key
            or run.status not in ({'completed'} if _completed else {'created', 'running'}) or user is None
            or user.sessionKey != context.session_key or user.messageType != 'text' or user.status != 'completed'
            or user.metadata.get('internalEvent')):
        raise PermissionError('generation requires the actual current owner input')
    source, revision, scope, authority = None, None, '', ''
    if context.session_key.startswith('thread:'):
        source = capture_channel_source(channels, sessions, role_id=role_id,
            session_key=context.session_key, run_id=context.run_id, owner_context=True)
        if source_thread_id is not None and source.thread_id != source_thread_id or source_account_id is not None and source.account_id != source_account_id:
            raise PermissionError('generation source does not match its host origin')
        payload = source.to_dict()
        scope = hashlib.sha256(canonical({key: value for key, value in payload.items() if key != 'message_created_at'}).encode()).hexdigest()
        authority = canonical(payload)
        revision = tuple(channels.directory_revision(source.account_id, source.channel))
    elif (context.session_key != f'role:{role_id}' or source_account_id
            or source_thread_id not in {None, f'desktop:{role_id}'}
            or run.modelSnapshot.get('humanInputSource') != 'desktop_owner'
            or user.metadata.get('channel', 'desktop') != 'desktop'):
        raise PermissionError('generation requires a host-accepted desktop human run')
    origin = TaskOrigin(role_id, context.session_key, context.run_id, context.tool_call_id,
        context.capability_snapshot_id or '', source.thread_id if source else f'desktop:{role_id}',
        source.account_id if source else '', scope, authority)
    attachment_ids = frozenset(str(item['id']) for key in ('attachments', 'quotedAttachments')
        for item in user.metadata.get(key, []) if isinstance(item, dict) and item.get('id') and item.get('kind') == 'image')
    metadata = user.metadata
    return GenerationContext(origin, context.run_id, f'tool:{context.run_id}:{context.tool_call_id}',
        attachment_ids, source, revision, reply_to_message_id=str(metadata['platformMessageId'])
            if source and metadata.get('platformMessageId') is not None else None,
        message_thread_id=metadata.get('messageThreadId') if source else None,
        mention_ids=(source.sender_id,) if source and source.channel == 'qq' and source.chat_type == 'group' else ())
