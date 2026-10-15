"""Compatibility alias for :mod:`backend.app.ai.model_adapter`."""

import sys as _sys
from .ai import model_adapter as _implementation

_sys.modules[__name__] = _implementation
