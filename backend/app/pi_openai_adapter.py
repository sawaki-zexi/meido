"""Compatibility alias for :mod:`backend.app.ai.pi_openai_adapter`."""

import sys as _sys
from .ai import pi_openai_adapter as _implementation

_sys.modules[__name__] = _implementation
