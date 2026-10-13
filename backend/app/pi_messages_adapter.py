"""Compatibility alias for :mod:`backend.app.ai.pi_messages_adapter`."""

import sys as _sys
from .ai import pi_messages_adapter as _implementation

_sys.modules[__name__] = _implementation
