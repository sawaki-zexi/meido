"""Compatibility alias for :mod:`backend.app.ai.mistral_adapter`."""

import sys as _sys
from .ai import mistral_adapter as _implementation

_sys.modules[__name__] = _implementation
