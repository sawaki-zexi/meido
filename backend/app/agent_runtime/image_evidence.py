"""Transient visual views and receipts at the model-adapter submission boundary.

Receipts describe bytes supplied, never successful recognition or authorization
to reuse an attachment. They contain no pixels, paths or download addresses.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import uuid
from dataclasses import replace

from PIL import Image

from .types import ImageContent, ImageSource, ToolResultMessage, UserMessage

MAX_RECEIPT_IMAGES = 64
MAX_SOURCE_MESSAGES = 8


def safe_identifier(value: str) -> str:
    if re.fullmatch(r'[A-Za-z0-9_.:-]{0,128}', value):
        return value
    return 'opaque-' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def image_views(raw: bytes, media_type: str, source_id: str, *, normalize=False,
                source_kind='message', source_message_id='') -> tuple[ImageContent, ...]:
    digest = hashlib.sha256(raw).hexdigest()
    provenance = ImageSource(safe_identifier(source_id), digest,
        source_kind=source_kind if source_kind in {'message', 'current', 'reference'} else 'unknown',
        source_message_id=safe_identifier(source_message_id))
    try:
        opened = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError as error:
        raise ValueError('image decoded pixels exceed limit') from error
    except (OSError, ValueError):
        if normalize or media_type == 'image/gif':
            raise
        # Older attachment registrations accepted signatures without decoding.
        # Preserve their payload contract without inventing frame coverage.
        return (ImageContent(media_type, base64.b64encode(raw).decode('ascii'),
            source=replace(provenance, frame_count=0)),)
    with opened as image:
        count = getattr(image, 'n_frames', 1)
        if count * image.width * image.height > 40_000_000:
            raise ValueError('image decoded pixels exceed limit')
        if count == 1 and not normalize and media_type != 'image/gif':
            return (ImageContent(media_type, base64.b64encode(raw).decode('ascii'), source=provenance),)
        indices = sorted({0, count // 2, count - 1})
        result, encoded_bytes = [], 0
        for index in indices:
            image.seek(index)
            frame = image.convert('RGB')
            frame.thumbnail((1024, 1024))
            output = io.BytesIO()
            frame.save(output, 'PNG')
            data = output.getvalue()
            encoded_bytes += len(data)
            if encoded_bytes > 4 * 1024 * 1024:
                raise ValueError('image view exceeds encoded budget')
            result.append(ImageContent('image/png', base64.b64encode(data).decode('ascii'),
                source=replace(provenance, frame_index=index, frame_count=count, sampled=count > len(indices))))
        return tuple(result)


def image_label(image: ImageContent) -> str:
    source = image.source
    if source is None:
        return ''
    coverage = '；有限采样，未覆盖完整动画' if source.sampled else ''
    position = f'帧位置 {source.frame_index + 1}/{source.frame_count}{coverage}' if source.frame_count else '动画覆盖未核验'
    return (f'[图片输入 source={safe_identifier(source.source_id)}；来源={source.source_kind}；内容版本 sha256={source.content_sha256}；'
        f'{position}。'
        '仅表示本次提供的视觉材料，不代表已正确理解；不要猜测未提供的内容。]')


def representation_digest(encoded: str) -> str:
    try:
        return hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
    except (binascii.Error, ValueError):
        return ''  # Invalid bytes are not successful pixel evidence.


def prepare_images(messages, *, supported: bool):
    """Keep source association outside the provider wire format."""
    normalized, sources = [], []
    for message in messages:
        if isinstance(message, UserMessage):
            attachments = []
            for attachment in message.attachments:
                image = attachment.image
                if image is not None:
                    source = image.source
                    digest = representation_digest(image.data_base64)
                    sources.append({'sourceId': safe_identifier(source.source_id) if source else '',
                        'sourceMessageIds': [safe_identifier(value) for value in message.source_message_ids[:MAX_SOURCE_MESSAGES]],
                        'sourceKind': source.source_kind if source else 'unknown',
                        'originalMessageId': safe_identifier(source.source_message_id) if source else '',
                        'contentSha256': source.content_sha256 if source else digest,
                        'representationSha256': digest,
                        'frameIndex': source.frame_index if source else 0,
                        'frameCount': source.frame_count if source else 1,
                        'coverage': ('sampled' if source.sampled else 'complete') if source and source.frame_count else 'unknown',
                        'status': 'prepared' if supported else 'unsupported'})
                    if not supported:
                        attachment = replace(attachment, image=None, text='\n'.join(filter(None, (
                            attachment.text, image_label(image), '本次模型不支持视觉：未提供像素，未读取图片内容。'))))
                attachments.append(attachment)
            if tuple(attachments) != message.attachments:
                message = replace(message, attachments=tuple(attachments))
        elif isinstance(message, ToolResultMessage) and not supported and message.images:
            message = replace(message, images=(), content=message.content + '\n本次模型不支持视觉：未提供像素。')
        normalized.append(message)
    return normalized, tuple(sources)


def submitted_receipt(wire_messages, sources, *, boundary='model_adapter'):
    from ..ai.model_types import ModelImageBlock

    images = []
    def visit(value):
        if isinstance(value, ModelImageBlock):
            images.append((value.media_type, value.data_base64))
        elif isinstance(value, dict):
            if value.get('type') in {'image_url', 'input_image'}:
                field = value.get('image_url', '')
                url = field.get('url', '') if isinstance(field, dict) else field
                if isinstance(url, str) and url.startswith('data:') and ';base64,' in url:
                    media, encoded = url[5:].split(';base64,', 1)
                    images.append((media, encoded))
            elif value.get('type') == 'image' and isinstance(value.get('source'), dict):
                source = value['source']
                if source.get('type') == 'base64':
                    images.append((source.get('media_type', ''), source.get('data', '')))
            elif value.get('type') == 'image' and 'mimeType' in value and 'data' in value:
                images.append((value['mimeType'], value['data']))
            elif isinstance(value.get('inlineData'), dict):
                images.append((value['inlineData'].get('mimeType', ''), value['inlineData'].get('data', '')))
            elif isinstance(value.get('image'), dict) and isinstance(value['image'].get('source'), dict):
                source = value['image']
                images.append(('image/' + source.get('format', ''), source['source'].get('bytes', '')))
            else:
                for item in value.values():
                    visit(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit(item)
        elif hasattr(value, 'content'):
            visit(value.content)
    visit(wire_messages)
    remaining = list(sources)
    records = []
    for index, (media, encoded) in enumerate(images):
        digest = representation_digest(encoded)
        source = next((item for item in remaining if item['representationSha256'] == digest
            and item['status'] == 'prepared'), None)
        if source is not None:
            remaining.remove(source)
        records.append({**(source or {'sourceId': '', 'sourceMessageIds': [], 'contentSha256': digest,
            'representationSha256': digest, 'coverage': 'unknown'}),
            'status': 'pixels_submitted' if digest else 'invalid_encoding', 'imageIndex': index, 'mediaType': media})
    records.extend({**item, 'status': item['status'] if item['status'] == 'unsupported' else 'not_submitted'}
        for item in remaining)
    return {'requestId': 'vision-' + uuid.uuid4().hex, 'boundary': boundary,
        'understanding': 'quality-unverified', 'images': records[:MAX_RECEIPT_IMAGES],
        'totalImages': len(records), 'submittedImages': len(images),
        'omittedImages': max(0, len(records) - MAX_RECEIPT_IMAGES)}


def run_media_observer(sessions, run_id):
    def observe(receipt):
        current = sessions.get_run(run_id)
        if current is not None and receipt['images']:
            previous = current.modelSnapshot.get('mediaRequests', [])
            sessions.update_run(run_id, status=current.status, model_snapshot={**current.modelSnapshot,
                'mediaRequests': [*previous[-7:], receipt]})
    return observe
