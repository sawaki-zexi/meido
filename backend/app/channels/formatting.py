"""Prepare platform text before dispatch; formatting never performs network I/O."""
import json
import re

from telegramify_markdown import convert
from telegramify_markdown.entity import MessageEntity, split_entities


def utf16_length(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def telegram_text(text: str, *, mentions=()):
    rendered, entities = convert(text)
    if mentions:
        prefix = ' '.join('@' + value for value in mentions) + (' ' if rendered else '')
        shift = utf16_length(prefix)
        for entity in entities:
            entity.offset += shift
        offset = 0
        for value in mentions:
            length = utf16_length('@' + value)
            entities.append(MessageEntity(type='text_link', offset=offset, length=length, url=f'tg://user?id={value}'))
            offset += length + 1
        rendered = prefix + rendered
    return rendered, entities


def telegram_chunks(text, entities, *, limit=4096):
    return [(chunk, [entity.to_dict() for entity in items])
        for chunk, items in split_entities(text, entities, limit) if chunk]


def markdown_chunks(text: str, *, limit=4000):
    chunks, current = [], ''
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = ''
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current += line
    if current:
        chunks.append(current)
    return chunks


def feishu_card(text: str) -> str:
    return json.dumps({'schema': '2.0', 'config': {'update_multi': True,
        'summary': {'content': ' '.join(text.split())[:60]}},
        'body': {'elements': [{'tag': 'markdown', 'content': text}]}}, ensure_ascii=False)


def markdown_renderings(chunks):
    """Close/reopen fenced code per card; retain original chunks for receipts."""
    fence, opening, line_start = '', '', True
    rendered = []
    for chunk in chunks:
        prefix = opening + '\n' if fence else ''
        for line in chunk.splitlines(keepends=True):
            match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)', line) if line_start else None
            if match:
                marker, suffix = match.groups()
                if not fence:
                    fence, opening = marker, line.rstrip('\r\n')
                elif marker[0] == fence[0] and len(marker) >= len(fence) and not suffix.strip():
                    fence, opening = '', ''
            line_start = line.endswith(('\n', '\r'))
        rendered.append(prefix + chunk + (('' if chunk.endswith('\n') else '\n') + fence if fence else ''))
    return rendered
