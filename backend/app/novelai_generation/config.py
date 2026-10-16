from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from ..credentials import CredentialStore

MODELS = ('nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full')
SAMPLERS = ('k_euler_ancestral', 'k_euler')


def _default_tag_ratings() -> list[Literal['general', 'mature', 'explicit']]:
    return ['general']


class NovelaiRoleConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    manualEnabled: StrictBool = False
    manualDailyAttempts: StrictInt = Field(default=10, ge=1, le=100)
    sceneCgEnabled: StrictBool = False
    sceneCgDailyAttempts: StrictInt = Field(default=3, ge=1, le=100)
    scenePromptModelConfigurationId: str | None = None
    autoWritebackRoleAssets: StrictBool = False
    promptTagsEnabled: StrictBool = False
    allowedPromptTagRatings: list[Literal['general', 'mature', 'explicit']] = Field(default_factory=_default_tag_ratings, min_length=1, max_length=3)
    consentRevision: StrictInt = Field(default=0, ge=0)


class NovelaiSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool = False
    allowedModels: list[str] = Field(default_factory=lambda: [MODELS[0]], min_length=1, max_length=2)
    defaultModel: str = MODELS[0]
    allowedSamplers: list[str] = Field(default_factory=lambda: list(SAMPLERS), min_length=1, max_length=2)
    defaultSampler: str = SAMPLERS[0]
    maxPixels: StrictInt = Field(default=1_048_576, ge=4096, le=1_048_576)
    maxSteps: StrictInt = Field(default=28, ge=1, le=28)
    dailyAttemptLimit: StrictInt = Field(default=20, ge=1, le=1000)

    @field_validator('allowedModels', 'allowedSamplers')
    @classmethod
    def allowlist(cls, value, info):
        allowed = MODELS if info.field_name == 'allowedModels' else SAMPLERS
        if len(set(value)) != len(value) or any(item not in allowed for item in value):
            raise ValueError('unsupported allowlist')
        return value


class NovelaiSettingsStore:
    """Public config is separate from the secret and changes its own revision."""

    def __init__(self, database_path: str | Path, credentials: CredentialStore,
                 *, lock: threading.RLock | None = None):
        self.database_path = str(database_path)
        self.credentials = credentials
        self.lock = lock or threading.RLock()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS novelai_settings(singleton INTEGER PRIMARY KEY CHECK(singleton=1), settings TEXT NOT NULL, revision INTEGER NOT NULL, token_configured INTEGER NOT NULL, cleanup_pending INTEGER NOT NULL DEFAULT 0)')
            connection.execute('INSERT OR IGNORE INTO novelai_settings VALUES(1,?,1,0,0)', (NovelaiSettings().model_dump_json(),))

    def public(self) -> dict[str, object]:
        with self.lock, sqlite3.connect(self.database_path) as connection:
            row = connection.execute('SELECT settings,revision,token_configured,cleanup_pending FROM novelai_settings WHERE singleton=1').fetchone()
        return {**json.loads(row[0]), 'revision': row[1], 'tokenConfigured': bool(row[2]), 'credentialCleanupPending': bool(row[3])}

    def save(self, changes: dict[str, object], *, expected_revision: int) -> dict[str, object]:
        with self.lock, sqlite3.connect(self.database_path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT settings,revision FROM novelai_settings WHERE singleton=1').fetchone()
            if type(expected_revision) is not int or row[1] != expected_revision:
                raise ValueError('service revision changed')
            settings = NovelaiSettings.model_validate({**json.loads(row[0]), **changes})
            if settings.defaultModel not in settings.allowedModels or settings.defaultSampler not in settings.allowedSamplers:
                raise ValueError('default must be in allowlist')
            connection.execute('UPDATE novelai_settings SET settings=?, revision=revision+1 WHERE singleton=1', (settings.model_dump_json(),))
        return self.public()

    def set_token(self, token: str) -> dict[str, object]:
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or not token.isascii() or any(character.isspace() for character in token)
                or re.search(r'\$\{|\$env:', token, re.IGNORECASE)):
            raise ValueError('invalid service token')
        with self.lock:
            self._prepare_credential_change()
            self.credentials.set('token', token)
            with sqlite3.connect(self.database_path) as connection:
                connection.execute('UPDATE novelai_settings SET token_configured=1,cleanup_pending=0 WHERE singleton=1')
        return self.public()

    def clear_token(self) -> dict[str, object]:
        with self.lock:
            self._prepare_credential_change()
            self.credentials.set('token', '')
            with sqlite3.connect(self.database_path) as connection:
                connection.execute('UPDATE novelai_settings SET cleanup_pending=0 WHERE singleton=1')
        return self.public()

    def _prepare_credential_change(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute('UPDATE novelai_settings SET token_configured=0,cleanup_pending=1,revision=revision+1 WHERE singleton=1')

    def dispatch_token(self) -> str:
        """Called only after a durably admitted dispatch, never during save/health."""
        token = self.credentials.get('token')
        if not token or not token.isascii() or any(char.isspace() for char in token) or '${' in token:
            raise PermissionError('NovelAI credential unavailable')
        return token
