"""Compatibility alias for :mod:`backend.app.ai.model_types`."""

import sys as _sys
from .ai import model_types as _implementation

_sys.modules[__name__] = _implementation
