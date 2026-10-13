"""Compatibility alias for :mod:`backend.app.ai.google_vertex_adapter`."""

import sys as _sys
from .ai import google_vertex_adapter as _implementation

_sys.modules[__name__] = _implementation
