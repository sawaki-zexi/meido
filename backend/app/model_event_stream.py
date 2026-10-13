"""Compatibility alias for :mod:`backend.app.ai.model_event_stream`."""

import sys as _sys
from .ai import model_event_stream as _implementation

_sys.modules[__name__] = _implementation
