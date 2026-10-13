"""Compatibility alias for :mod:`backend.app.ai.message_transform`."""

import sys as _sys
from .ai import message_transform as _implementation

_sys.modules[__name__] = _implementation
