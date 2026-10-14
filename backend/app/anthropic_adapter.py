"""Compatibility alias for :mod:`backend.app.ai.anthropic_adapter`."""

import sys as _sys
from .ai import anthropic_adapter as _implementation

_sys.modules[__name__] = _implementation
