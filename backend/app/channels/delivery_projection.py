"""Read confirmed channel content without changing its original transcript."""
from __future__ import annotations


def confirmed_parts(metadata):
    parts = metadata.get('parts')
    if not isinstance(parts, list):
        return []
    return [{**part, 'content': part['content'] if part.get('status') == 'sent' else part['confirmedContent']}
            for part in parts if isinstance(part, dict) and part.get('platformMessageId') and part.get('kind') != 'preview'
            and (part.get('status') == 'sent' or part.get('confirmedContent') is not None)]


def delivered_message(message):
    if (message.role != 'assistant' or message.metadata.get('channel') in {None, '', 'desktop'}
            or not isinstance(message.metadata.get('parts'), list)):
        return message
    parts = confirmed_parts(message.metadata)
    if not parts:
        return None
    if message.metadata.get('deliveryStatus') == 'sent':
        return message
    indices = {part.get('attachmentIndex') for part in parts if part.get('kind') == 'media'}
    attachments = [item for index, item in enumerate(message.metadata.get('attachments', [])) if index in indices]
    content = ''.join(str(part.get('content', '')) for part in parts)
    # Rich providers prefer content_blocks over the plain content field. The
    # original blocks/signatures describe the full generated answer, including
    # unsent text or images. Only rebuild what the receipts actually establish;
    # confirmed attachments keep their role-owned IDs for media loading.
    metadata = {key: value for key, value in message.metadata.items() if key not in {'content_blocks', 'thinking'}}
    metadata.update(attachments=attachments, deliveryProjection=True,
        content_blocks=[{'type': 'text', 'text': content, 'text_signature': None}])
    return message.model_copy(update={'content': content, 'status': 'completed', 'metadata': metadata})
