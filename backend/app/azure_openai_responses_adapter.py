"""Compatibility alias for :mod:`backend.app.ai.azure_openai_responses_adapter`."""

import sys as _sys
from .ai import azure_openai_responses_adapter as _implementation

_sys.modules[__name__] = _implementation
