"""Compatibility alias for :mod:`backend.app.ai.bedrock_adapter`."""

import sys as _sys
from .ai import bedrock_adapter as _implementation

_sys.modules[__name__] = _implementation
