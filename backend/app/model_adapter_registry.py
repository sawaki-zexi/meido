"""Compatibility alias for :mod:`backend.app.ai.model_adapter_registry`."""

import sys as _sys
from .ai import model_adapter_registry as _implementation

_sys.modules[__name__] = _implementation
