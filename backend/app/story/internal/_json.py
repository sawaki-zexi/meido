# Adapted from Shiori-Agent plugins/story at d3acaed186b172a78a5892e0307e9affd89759f0.
# Copyright (c) 2026 YinFengWindy; MIT. See SHIORI-LICENSE.txt.
"""JSON helpers kept local so Story does not import retired role runtime details."""

from __future__ import annotations

import json
from typing import Any


def dump(value: Any) -> str:
    """Serialize durable Story payloads deterministically."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load(value: str, default: Any) -> Any:
    """Deserialize a durable payload and preserve a caller-provided default."""

    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
