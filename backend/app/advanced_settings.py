"""Versioned ordinary-chat policy; specialized task permissions stay independent."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AdvancedSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    maxTokens: int = Field(default=8192, ge=1, le=131072)
    maxIterations: int = Field(default=40, ge=1, le=100)
    searchEnabled: bool = True
    spawnEnabled: bool = True
    memoryWindow: int = Field(default=40, ge=12, le=2000)
    memoryOptimizerEnabled: bool = True
    memoryOptimizerIntervalSeconds: int = Field(default=64800, ge=60, le=604800)
    summaryTokenLimit: int = Field(default=2000, ge=1, le=131072)
    compactionRetainedTurns: int = Field(default=2, ge=0, le=100)
    contextTriggerRatio: float = Field(default=.75, gt=0, lt=1)
    contextTargetRatio: float = Field(default=.4, gt=0, lt=1)
    contextSafetyMarginTokens: int = Field(default=4096, ge=0, le=131072)
    devMode: bool = False

    @model_validator(mode='after')
    def validate_budget(self):
        if self.contextTargetRatio >= self.contextTriggerRatio:
            raise ValueError('压缩目标比例必须小于触发比例')
        if self.summaryTokenLimit > self.maxTokens:
            raise ValueError('摘要长度不能超过回复 token 上限')
        return self

    @property
    def recent_memory_count(self):
        return max(4, ((self.memoryWindow + 3) // 4) * 4) // 2


class SettingsConflict(ValueError):
    pass


class AdvancedSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expectedRevision: int = Field(ge=0)
    operationId: str = Field(min_length=1, max_length=200)
    values: dict


class AdvancedSettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL)')
            defaults = AdvancedSettings()
            env = os.getenv('MEIDO_MEMORY_OPTIMIZER_ENABLED')
            interval = os.getenv('MEIDO_MEMORY_OPTIMIZER_INTERVAL_SECONDS')
            values = defaults.model_dump()
            if env is not None:
                values['memoryOptimizerEnabled'] = env.strip().lower() not in {'0', 'false', 'no', 'off'}
            if interval is not None:
                try:
                    values['memoryOptimizerIntervalSeconds'] = int(float(interval))
                    AdvancedSettings.model_validate(values)
                except (ValueError, OverflowError):
                    values['memoryOptimizerIntervalSeconds'] = defaults.memoryOptimizerIntervalSeconds
            doc = {'revision': 0, 'spawnGeneration': 0, 'values': AdvancedSettings.model_validate(values).model_dump()}
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (json.dumps(doc),))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self):
        with self._connect() as db:
            return json.loads(db.execute('SELECT document FROM settings WHERE id=1').fetchone()[0])

    def values(self):
        return AdvancedSettings.model_validate(self.get()['values'])

    def update(self, *, expected_revision, operation_id, values):
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
            raise ValueError('无效的设置修订或操作 ID')
        if not isinstance(values, dict):
            raise ValueError('设置必须为对象')
        request = json.dumps({'revision': expected_revision, 'values': values}, sort_keys=True, allow_nan=False)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT request, result FROM operations WHERE id=?', (operation_id,)).fetchone()
            if prior:
                if prior[0] != request:
                    raise SettingsConflict('相同操作 ID 不能用于不同修改')
                return json.loads(prior[1])
            current = json.loads(db.execute('SELECT document FROM settings WHERE id=1').fetchone()[0])
            if current['revision'] != expected_revision:
                raise SettingsConflict('设置已在其他窗口变化，请重新读取')
            merged = AdvancedSettings.model_validate({**current['values'], **values})
            generation = current['spawnGeneration'] + int(current['values']['spawnEnabled'] and not merged.spawnEnabled)
            result = {'revision': current['revision'] + 1, 'spawnGeneration': generation, 'values': merged.model_dump()}
            payload = json.dumps(result, allow_nan=False)
            db.execute('UPDATE settings SET document=? WHERE id=1', (payload,))
            db.execute('INSERT INTO operations VALUES (?, ?, ?)', (operation_id, request, payload))
            return result
