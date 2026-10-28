"""Receipt-owned ordinary channel message sequences, without an autonomous scheduler."""
from __future__ import annotations

import asyncio

from .models import TargetKind, target_from_chat


async def deliver_channel_sequence(thread, metadata, run_id, first_id, sequence, *,
        sessions, channels, host, generation, finish_started, pause, source_current,
        generation_reply=None, reaction=None, reaction_attachment=None):
    """Each logical message owns its receipt; never persist an unstarted suffix."""
    task = asyncio.current_task()
    first = next(item for item in sessions.list_messages(thread.session_key) if item.id == first_id)
    base = {key: value for key, value in first.metadata.items() if key not in {
        'replySequence', 'attachments', 'generationAttemptIds', 'generationId', 'generationAttemptId', 'deliveryStatus', 'deliveryAttemptId', 'platformMessageId',
        'deliveryError', 'parts', 'partialDelivery', 'deliveredContent', 'receiptKind'}}
    target = target_from_chat(account_id=thread.account_id, chat_id=thread.chat_id,
        kind=TargetKind.GROUP if thread.chat_type == 'group' else TargetKind.PRIVATE)
    last_sent = None

    def authorized():
        return not task.cancelling() and source_current()

    for index, text in enumerate(sequence):
        if index:
            # Runtime released the role model gate; only this chat's lane is held.
            await pause(sequence[index - 1])
        if not authorized():
            if index == 0:
                sessions.update_message(first.id, '', 'failed', metadata={**base, 'deliveryStatus': 'failed'})
            break
        outgoing = {**base, 'replySequenceId': run_id, 'replySequenceIndex': index,
                    'naturalReply': True, 'deliveryStatus': 'pending'}
        if index == 0:
            outgoing.update({key: first.metadata[key] for key in ('attachments', 'generationAttemptIds',
                'generationId', 'generationAttemptId') if key in first.metadata})
        message = (sessions.update_message(first.id, text, 'streaming', metadata=outgoing) if index == 0
            else sessions.append_message(thread.session_key, 'assistant', text, 'streaming', run_id=run_id, metadata=outgoing))
        media_ids = (generation_reply.attachment_ids if generation_reply is not None
            else (reaction_attachment['id'],) if reaction_attachment is not None else ()) if index == 0 else ()
        cancellations = []
        try:
            result = await finish_started(host.account_send(
                thread.role_id, thread.account_id, target.id, text,
                attachment_ids=media_ids,
                frozen_attachment_bytes=({reaction_attachment['id']: reaction.raw}
                    if index == 0 and reaction_attachment is not None else None),
                authorize_source=lambda: (authorized()
                    and (index != 0 or generation_reply is None or generation_reply.authorized())
                    and (index != 0 or reaction_attachment is None or reaction.authorized())),
                reply_to_message_id=str(metadata.get('platformMessageId')) if metadata.get('platformMessageId') else None,
                actor='reply', source_thread_id=thread.thread_id, record_message=False,
                source_session_key=thread.session_key, source_run_id=run_id,
                source_tool_call_id=f'{run_id}:reply:{index}',
                source_kind='role_reaction' if index == 0 and reaction_attachment is not None else 'natural_sequence',
                mention_ids=(str(metadata['senderId']),) if thread.chat_type == 'group' and thread.channel == 'qq' and metadata.get('senderId') else (),
                message_thread_id=metadata.get('messageThreadId'),
            ), cancellations)
            delivery = {'deliveryStatus': result.status.value, 'deliveryAttemptId': result.attempt_id,
                'platformMessageId': result.platform_message_id, 'deliveryError': result.error,
                **{key: result.raw[key] for key in ('parts', 'partialDelivery', 'deliveredContent', 'receiptKind') if key in result.raw}}
        except Exception as error:
            delivery = {'deliveryStatus': 'uncertain', 'deliveryError': type(error).__name__}
        message = sessions.update_message(message.id, text,
            'completed' if delivery['deliveryStatus'] == 'sent' else 'failed', metadata={**outgoing, **delivery})
        if index == 0 and generation_reply is not None:
            await generation.record_channel_receipt(generation_reply, status=delivery['deliveryStatus'],
                delivery_id=delivery.get('deliveryAttemptId'), platform_message_id=delivery.get('platformMessageId'))
            await generation.mark_reply_synced(generation_reply)
        channels.publish(thread.role_id, {'type': 'message', 'roleId': thread.role_id,
            'thread': channels.public_thread_payload(thread),
            'message': channels.public_message_payload(thread, message)})
        if delivery['deliveryStatus'] == 'sent':
            last_sent = message
        if cancellations:
            raise cancellations[0]
        if delivery['deliveryStatus'] != 'sent':
            break
    return last_sent
