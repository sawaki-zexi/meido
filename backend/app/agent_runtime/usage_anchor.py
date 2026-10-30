"""Request-scoped usage anchors, adapted from Shiori 71c8b0e.

Copyright (c) 2026 YinFengWindy. MIT: third_party/shiori-agent/LICENSE.
Ownership is explicit on MeidoProvider rather than ambient ContextVars; child
providers own temporary stores and ordinary turns bind the host's shared store.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

MAX_ANCHOR_CONTEXTS = 64
SYSTEM_CONTEXT_FRAME_MARKER = '<system-reminder data-system-context-frame="true">'
Context = tuple[str, ...]
Request = dict[str, Any]
Ticket = tuple[Context | None, object, Request]


def estimate_tokens(value: Any) -> int:
    """Shiori local estimate, extended to the native protocols' image blocks."""
    if isinstance(value, str):
        wide = sum(ord(character) > 127 for character in value)
        return (len(value) - wide + 2) // 3 + wide * 2
    if isinstance(value, dict):
        if (value.get('type') in ('image_url', 'input_image', 'image')
            or isinstance(value.get('inlineData'), dict) and 'mimeType' in value['inlineData']
            or isinstance(value.get('image'), dict) and 'source' in value['image']):
            image = value.get('image_url')
            detail = image.get('detail') if isinstance(image, dict) else value.get('detail')
            return 85 if detail == 'low' else 4096
        return 2 + sum(estimate_tokens(str(key)) + estimate_tokens(item) + 2 for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return 2 + sum(estimate_tokens(item) + 2 for item in value)
    return estimate_tokens(str(value)) if value is not None else 1


def input_cost(request: Request) -> int:
    return estimate_tokens({key: value for key, value in request.items()
                            if key in {'messages', 'tools', 'response_format'}})


@dataclass(frozen=True)
class InputEstimate:
    tokens: int
    source: Literal['actual', 'anchor_delta', 'local']


@dataclass
class _Anchor:
    request: Request
    prompt_tokens: int


def mark_context_frame(request: Request, frame: str) -> Request:
    """Only the host-created second message is replaceable, never user markers."""
    messages = request.get('messages', [])
    if frame and len(messages) > 1 and messages[1].get('role') == 'user':
        content = messages[1].get('content', messages[1].get('parts'))
        if content == frame or (isinstance(content, list) and len(content) == 1
                               and content[0].get('text') == frame):
            request['_context_frames'] = (1,)
    return request


def _stable_messages(request: Request) -> list[dict]:
    frames = request.get('_context_frames', ())
    return [message for index, message in enumerate(request.get('messages', [])) if index not in frames]


class UsageAnchors:
    """64-context LRU with one latest in-flight ticket per actual context."""
    def __init__(self, max_contexts: int = MAX_ANCHOR_CONTEXTS) -> None:
        if max_contexts <= 0:
            raise ValueError('max_contexts 必须为正整数')
        self._max_contexts = max_contexts
        self._anchors: dict[Context, _Anchor] = {}
        self._pending: dict[Context, object] = {}
        self._views: OrderedDict[Context, Context] = OrderedDict()

    def _forget(self, key: Context) -> None:
        self._anchors.pop(key, None)
        self._pending.pop(key, None)

    def _key(self, context: Context | None) -> Context | None:
        if context is None:
            return None
        key = context[:3]
        if self._views.get(key) != context:
            self._forget(key)
            self._views[key] = context
        self._views.move_to_end(key)
        while len(self._views) > self._max_contexts:
            evicted, _ = self._views.popitem(last=False)
            self._forget(evicted)
        return key

    def estimate(self, request: Request, context: Context | None) -> InputEstimate:
        local = input_cost(request)
        key = self._key(context)
        anchor = self._anchors.get(key) if key is not None else None
        if anchor is None:
            return InputEstimate(local, 'local')
        if anchor.request == request:
            return InputEstimate(anchor.prompt_tokens, 'actual')
        previous, current = _stable_messages(anchor.request), _stable_messages(request)
        old_other = {k: v for k, v in anchor.request.items() if k not in {'messages', '_context_frames'}}
        new_other = {k: v for k, v in request.items() if k not in {'messages', '_context_frames'}}
        if old_other == new_other and current[:len(previous)] == previous:
            return InputEstimate(max(0, anchor.prompt_tokens + local - input_cost(anchor.request)), 'anchor_delta')
        if key is not None:
            self._forget(key)
        return InputEstimate(local, 'local')

    def begin(self, request: Request, context: Context | None) -> Ticket:
        key, ticket = self._key(context), object()
        if key is not None:
            self._pending[key] = ticket
        return key, ticket, deepcopy(request)

    def finish(self, ticket: Ticket, prompt_tokens: int | None) -> None:
        key, identity, request = ticket
        if key is None or self._pending.get(key) is not identity:
            return
        self._pending.pop(key, None)
        if prompt_tokens is not None:
            self._anchors[key] = _Anchor(request, prompt_tokens)
