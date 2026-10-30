"""Application plugin switches; role ownership and purpose grants are separate."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from .herdr_control.config import HerdrConfig


class BrowserParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    headed: bool = False
    timeout_seconds: int = Field(default=45, ge=5, le=120)


class ScreenParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    visionModelConfigurationId: str | None = Field(default=None, min_length=1, max_length=200)


class EmptyParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


PARAMETERS = {'browser': BrowserParameters, 'screen': ScreenParameters, 'herdr': HerdrConfig,
              'computer': EmptyParameters, 'pet': EmptyParameters, 'autonomy': EmptyParameters}


class PluginState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool = False
    revision: StrictInt = Field(default=0, ge=0)
    cleanupPending: StrictBool = False
    values: dict = Field(default_factory=dict)


class PluginConflict(ValueError):
    pass


class PluginConfigurationStore:
    """Revoke durably before cleanup; incomplete transitions remain fail closed."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        if raw.get('version') != 1 or not isinstance(raw.get('plugins'), dict):
            raise ValueError('插件配置格式无效')
        return raw['plugins']

    def _get(self, items, identifier):
        if identifier not in PARAMETERS:
            raise KeyError(identifier)
        state = PluginState.model_validate(items.get(identifier, {}))
        state.values = PARAMETERS[identifier].model_validate(state.values).model_dump()
        return state

    def get(self, identifier) -> PluginState:
        with self._lock:
            return self._get(self._read(), identifier)

    def enabled(self, identifier) -> bool:
        state = self.get(identifier)
        return state.enabled and not state.cleanupPending

    def _write(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f'.{self.path.name}.{os.getpid()}.tmp')
        try:
            temporary.write_text(json.dumps({'version': 1, 'plugins': items}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def prepare(self, identifier, *, expected_revision, enabled=None, values=None):
        with self._lock:
            items = self._read()
            state = self._get(items, identifier)
            if type(expected_revision) is not int or state.revision != expected_revision:
                raise PluginConflict('插件设置已变化，请刷新后重试')
            if state.cleanupPending and enabled is not False:
                raise PluginConflict('原资源仍待收尾，请先重试关闭插件')
            if enabled is not None:
                if type(enabled) is not bool:
                    raise ValueError('插件开关必须为布尔值')
                state.enabled = enabled
            if values is not None:
                state.values = PARAMETERS[identifier].model_validate({**state.values, **values}).model_dump()
            state.revision += 1
            state.cleanupPending = True
            items[identifier] = state.model_dump()
            self._write(items)
            return state

    def finish(self, identifier, revision):
        with self._lock:
            items = self._read()
            state = self._get(items, identifier)
            if state.revision != revision:
                raise PluginConflict('插件设置已变化')
            state.cleanupPending = False
            items[identifier] = state.model_dump()
            self._write(items)
            return state

    def effective_role(self, role):
        """Project app policy without writing legacy role files."""
        if role is None:
            return None
        config = role.agentConfig.model_copy(deep=True)
        config.browserUse.enabled = self.enabled('browser')
        config.computerUse.enabled = self.enabled('computer')
        screen = self.get('screen')
        config.screenPerception.enabled = screen.enabled and not screen.cleanupPending
        config.screenPerception.visionModelConfigurationId = screen.values['visionModelConfigurationId']
        # Repository/profile grants remain purpose-specific; the duplicate role
        # master switch is no longer an admission boundary.
        config.coding.enabled = True
        return role.model_copy(update={'agentConfig': config})

    def hold(self, identifier, revision):
        with self._lock:
            items = self._read()
            state = self._get(items, identifier)
            if state.revision == revision:
                state.cleanupPending = True
                items[identifier] = state.model_dump()
                self._write(items)
