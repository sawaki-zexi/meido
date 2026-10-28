"""Bounded owner-managed literal prompt expansion; never executes expressions."""
from __future__ import annotations

import unicodedata
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from ..chat_attachments import public_attachment, resolve_attachment
from .ledger import canonical


class PromptTag(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=80)
    category: str = Field(default='composition', min_length=1, max_length=80)
    keywords: list[str] = Field(min_length=1, max_length=16)
    prompt: str = Field(max_length=4000)
    negative_prompt: str = Field(max_length=4000)
    rating: Literal['general', 'mature', 'explicit']
    enabled: StrictBool
    reference_attachment_id: str | None = None

    @field_validator('name')
    @classmethod
    def nonempty_name(cls, value):
        if not value.strip():
            raise ValueError('tag name must not be empty')
        return value.strip()

    @field_validator('category')
    @classmethod
    def valid_category(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError('tag category must be bounded text')
        return value

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

    @field_validator('reference_attachment_id')
    @classmethod
    def valid_reference_attachment_id(cls, value):
        if value is not None and (len(value) != 32 or any(char not in '0123456789abcdef' for char in value)):
            raise ValueError('reference attachment must be an owned attachment ID')
        return value


def _fold(value):
    return unicodedata.normalize('NFKC', value).casefold()


def _append(first, additions):
    parts = []
    seen = set()
    for index, fragment in enumerate([first, *additions]):
        if '::' in fragment or any(char in fragment for char in '{}[]'):
            # NovelAI weights span commas; removing even a repeated word can
            # remove a closing marker or change the weight of following tags.
            # Preserve the entire fragment without interpreting its syntax.
            parts.append(fragment.strip())
            continue
        for part in fragment.split(','):
            clean = part.strip()
            if clean and (index == 0 or clean.casefold() not in seen):
                parts.append(clean)
                seen.add(clean.casefold())
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

    def _reference_attachment(self, role_id, identifier, *, required=False):
        if not identifier:
            return None
        item = resolve_attachment(self.app.roles.workspace_path(role_id), identifier)
        if item is None or item.get('kind') != 'image':
            if required:
                raise ValueError('reference attachment must belong to this role and be an image')
            return None
        return public_attachment(item)

    def _view(self, role_id, identifier, tag, *, reference_attachment=None):
        if reference_attachment is None and tag.reference_attachment_id:
            reference_attachment = self._reference_attachment(role_id, tag.reference_attachment_id)
        return {
            'id': identifier,
            **tag.model_dump(),
            'referenceAttachment': reference_attachment,
        }

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
        tags = []
        for row in rows[:limit]:
            tag = PromptTag.model_validate(json.loads(row['value']))
            tags.append(self._view(role_id, row['id'], tag))
        return {'tags': tags,
            'revision': self.revision(role_id), 'nextCursor': rows[limit - 1]['id'] if len(rows) > limit else None}

    def save(self, role_id, value, identifier=None):
        tag = PromptTag.model_validate(value)
        with self.app._lock, self.app.ledger.connect() as db:
            self._role(role_id)
            reference_attachment = self._reference_attachment(role_id, tag.reference_attachment_id, required=True)
            db.execute('BEGIN IMMEDIATE')
            if identifier is not None and not db.execute('SELECT 1 FROM novelai_prompt_tags WHERE role_id=? AND id=?', (role_id, identifier)).fetchone():
                raise KeyError('tag unavailable')
            if identifier is None and db.execute('SELECT COUNT(*) FROM novelai_prompt_tags WHERE role_id=?', (role_id,)).fetchone()[0] >= 250:
                raise ValueError('role prompt tag capacity reached')
            identifier = identifier or uuid.uuid4().hex
            db.execute('INSERT INTO novelai_prompt_tags VALUES(?,?,?) ON CONFLICT(role_id,id) DO UPDATE SET value=excluded.value',
                (role_id, identifier, canonical(tag.model_dump())))
            db.execute('INSERT INTO novelai_tag_revisions VALUES(?,1) ON CONFLICT(role_id) DO UPDATE SET revision=revision+1', (role_id,))
        return {'tag': self._view(role_id, identifier, tag, reference_attachment=reference_attachment), 'revision': self.revision(role_id)}

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
        # Shiori's adult tag gate is service-wide, while Meido keeps the
        # role-level rating grant as a separate second gate.  Reading the
        # public projection here keeps preview and committed scene CG on the
        # same effective policy.
        service_nsfw_enabled = bool(self.app.settings.public()['nsfwEnabled'])
        library = self.list(role_id)
        while library['nextCursor']:
            page = self.list(role_id, cursor=library['nextCursor'])
            library['tags'].extend(page['tags'])
            library['nextCursor'] = page['nextCursor']
        folded = _fold(text)
        ranked = []
        for item in library['tags']:
            if not (role.agentConfig.novelai.promptTagsEnabled and item['enabled']
                    and item['rating'] in role.agentConfig.novelai.allowedPromptTagRatings
                    and (item['rating'] != 'explicit' or service_nsfw_enabled)):
                continue
            score = sum(1 for word in item['keywords'] if _fold(word) in folded)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], _fold(pair[1]['name']), pair[1]['id']))
        matched = [item for _, item in ranked[:5]]
        return {'prompt': _append(prompt, [item['prompt'] for item in matched]),
            'negative_prompt': _append(negative_prompt, [item['negative_prompt'] for item in matched]),
            'matchedIds': [item['id'] for item in matched], 'revision': library['revision']}
