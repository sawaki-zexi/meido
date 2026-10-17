"""Bounded owner-managed literal prompt expansion; never executes expressions."""
from __future__ import annotations

import unicodedata
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from .ledger import canonical


class PromptTag(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=80)
    keywords: list[str] = Field(min_length=1, max_length=16)
    prompt: str = Field(max_length=4000)
    negative_prompt: str = Field(max_length=4000)
    rating: Literal['general', 'mature', 'explicit']
    enabled: StrictBool

    @field_validator('name')
    @classmethod
    def nonempty_name(cls, value):
        if not value.strip():
            raise ValueError('tag name must not be empty')
        return value.strip()

    @field_validator('keywords')
    @classmethod
    def valid_keywords(cls, values):
        if any(not value.strip() or len(value) > 80 for value in values):
            raise ValueError('tag keywords must be bounded literal text')
        return list(dict.fromkeys(values))

    @field_validator('prompt', 'negative_prompt')
    @classmethod
    def ascii_tags(cls, value):
        if not value.isascii() or any(ord(char) < 32 for char in value):
            raise ValueError('English ASCII prompt tags required')
        return value.strip()


def _fold(value):
    return unicodedata.normalize('NFKC', value).casefold()


def _append(first, additions):
    parts = [item.strip() for item in first.split(',') if item.strip()]
    for addition in additions:
        for part in addition.split(','):
            if part.strip() and part.strip() not in parts:
                parts.append(part.strip())
    result = ', '.join(parts)
    if len(result) > 4000:
        raise ValueError('expanded prompt exceeds its approved bound')
    return result


class PromptTags:
    def __init__(self, application):
        self.app = application
        with self.app.ledger.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS novelai_prompt_tags(role_id TEXT,id TEXT,value TEXT NOT NULL,
                PRIMARY KEY(role_id,id)); CREATE TABLE IF NOT EXISTS novelai_tag_revisions(role_id TEXT PRIMARY KEY,revision INTEGER NOT NULL);''')

    def _role(self, role_id):
        role = self.app.roles.get(role_id)
        if role is None or self.app.is_role_deleting(role_id):
            raise KeyError('role unavailable')
        return role

    def revision(self, role_id):
        with self.app.ledger.connect() as db:
            row = db.execute('SELECT revision FROM novelai_tag_revisions WHERE role_id=?', (role_id,)).fetchone()
        return row[0] if row else 0

    def list(self, role_id, *, limit=50, cursor=None):
        import json
        self._role(role_id)
        if type(limit) is not int or not 1 <= limit <= 50 or (cursor is not None and
                (len(cursor) != 32 or any(char not in '0123456789abcdef' for char in cursor))):
            raise ValueError('invalid prompt tag page')
        with self.app.ledger.connect() as db:
            rows = db.execute('SELECT id,value FROM novelai_prompt_tags WHERE role_id=? AND id>? ORDER BY id LIMIT ?',
                (role_id, cursor or '', limit + 1)).fetchall()
        return {'tags': [{'id': row['id'], **json.loads(row['value'])} for row in rows[:limit]],
            'revision': self.revision(role_id), 'nextCursor': rows[limit - 1]['id'] if len(rows) > limit else None}

    def save(self, role_id, value, identifier=None):
        tag = PromptTag.model_validate(value)
        with self.app._lock, self.app.ledger.connect() as db:
            self._role(role_id)
            db.execute('BEGIN IMMEDIATE')
            if identifier is not None and not db.execute('SELECT 1 FROM novelai_prompt_tags WHERE role_id=? AND id=?', (role_id, identifier)).fetchone():
                raise KeyError('tag unavailable')
            if identifier is None and db.execute('SELECT COUNT(*) FROM novelai_prompt_tags WHERE role_id=?', (role_id,)).fetchone()[0] >= 250:
                raise ValueError('role prompt tag capacity reached')
            identifier = identifier or uuid.uuid4().hex
            db.execute('INSERT INTO novelai_prompt_tags VALUES(?,?,?) ON CONFLICT(role_id,id) DO UPDATE SET value=excluded.value',
                (role_id, identifier, canonical(tag.model_dump())))
            db.execute('INSERT INTO novelai_tag_revisions VALUES(?,1) ON CONFLICT(role_id) DO UPDATE SET revision=revision+1', (role_id,))
        return {'tag': {'id': identifier, **tag.model_dump()}, 'revision': self.revision(role_id)}

    def delete(self, role_id, identifier):
        with self.app._lock, self.app.ledger.connect() as db:
            self._role(role_id)
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('DELETE FROM novelai_prompt_tags WHERE role_id=? AND id=?', (role_id, identifier)).rowcount:
                raise KeyError('tag unavailable')
            db.execute('INSERT INTO novelai_tag_revisions VALUES(?,1) ON CONFLICT(role_id) DO UPDATE SET revision=revision+1', (role_id,))
        return {'revision': self.revision(role_id)}

    def expand(self, role_id, text, prompt='', negative_prompt=''):
        with self.app._lock:
            return self._expand(role_id, text, prompt, negative_prompt)

    def _expand(self, role_id, text, prompt, negative_prompt):
        if (not all(isinstance(value, str) for value in (text, prompt, negative_prompt)) or len(text) > 12000
                or max(len(prompt), len(negative_prompt)) > 4000 or not (prompt + negative_prompt).isascii()
                or any(ord(char) < 32 for char in prompt + negative_prompt)):
            raise ValueError('bounded current message and prompts required')
        role = self._role(role_id)
        library = self.list(role_id)
        while library['nextCursor']:
            page = self.list(role_id, cursor=library['nextCursor'])
            library['tags'].extend(page['tags'])
            library['nextCursor'] = page['nextCursor']
        folded = _fold(text)
        matched = [item for item in library['tags'] if role.agentConfig.novelai.promptTagsEnabled and item['enabled']
            and item['rating'] in role.agentConfig.novelai.allowedPromptTagRatings
            and any(_fold(word) in folded for word in item['keywords'])]
        return {'prompt': _append(prompt, [item['prompt'] for item in matched]),
            'negative_prompt': _append(negative_prompt, [item['negative_prompt'] for item in matched]),
            'matchedIds': [item['id'] for item in matched], 'revision': library['revision']}
