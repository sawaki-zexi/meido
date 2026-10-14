"""Compatibility alias for :mod:`backend.app.ai.model_protocol`."""

import sys as _sys
from .ai import model_protocol as _implementation

_sys.modules[__name__] = _implementation
