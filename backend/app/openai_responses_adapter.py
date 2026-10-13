"""Compatibility alias for :mod:`backend.app.ai.openai_responses_adapter`."""

import sys as _sys
from .ai import openai_responses_adapter as _implementation

_sys.modules[__name__] = _implementation
