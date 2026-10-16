"""Opt-in accounting context for each serialized model image request.

The application owns authorization and the durable reservation. This boundary
receives only trusted image fingerprints and returns counts, sizes and hashes.
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
import base64
import binascii
import hashlib
import json
import re

import httpx


@dataclass(frozen=True, slots=True)
class ImageProof:
    media_type: str
    base64_sha256: str
    encoded_bytes: int


@dataclass(frozen=True, slots=True)
class RequestImageUsage:
    protocol: str
    body_bytes: int
    image_occurrences: int
    image_base64_bytes: int
    image_input_estimate: int
    wire_non_image_bytes: int
    envelope_bytes: int
    body_sha256: str


@dataclass(frozen=True, slots=True)
class _ImageBudget:
    images: tuple[ImageProof, ...]
    canonical_text_bytes: int
    reserve: Callable[[RequestImageUsage], Awaitable[None]]


_active_budget: ContextVar[_ImageBudget | None] = ContextVar("model_image_request_budget", default=None)

_PROTOCOLS = frozenset({"openai-completions", "openai-responses", "azure-openai-responses",
    "openai-codex-responses", "anthropic-messages", "google-generative", "google-vertex",
    "mistral-conversations", "bedrock-converse", "pi-messages"})
_SAFE_CODES = frozenset({"invalid_context", "invalid_body", "invalid_image", "image_bytes",
    "unproved_image", "unsupported_protocol", "body_bytes", "non_image_bytes", "envelope_bytes",
    "run_image_occurrences", "run_image_bytes", "run_image_estimate", "run_envelope_bytes", "authorization"})


class ImageRequestBudgetError(ValueError):
    """A fixed rejection code, never a provider payload or image value."""

    def __init__(self, code: str):
        self.code = code if code in _SAFE_CODES else "invalid_context"
        super().__init__(f"image request budget rejected: {self.code}")


@contextmanager
def image_request_budget(
    *, images: tuple[ImageProof, ...], canonical_text_bytes: int,
    reserve: Callable[[RequestImageUsage], Awaitable[None]],
) -> Iterator[None]:
    """Keep this scope around the complete consumption of a model generator."""
    if (not isinstance(canonical_text_bytes, int) or isinstance(canonical_text_bytes, bool)
            or not 0 <= canonical_text_bytes <= 48 * 1024 or not callable(reserve)):
        raise ImageRequestBudgetError("invalid_context")
    if any(not isinstance(proof, ImageProof) or proof.media_type not in {"image/png", "image/jpeg"}
           or not isinstance(proof.encoded_bytes, int) or isinstance(proof.encoded_bytes, bool)
           or not 0 < proof.encoded_bytes <= 1024 * 1024
           or not isinstance(proof.base64_sha256, str) or re.fullmatch("[0-9a-f]{64}", proof.base64_sha256) is None
           for proof in images):
        raise ImageRequestBudgetError("invalid_context")
    token = _active_budget.set(_ImageBudget(tuple(images), canonical_text_bytes, reserve))
    try:
        yield
    finally:
        _active_budget.reset(token)


def _data_url(url: object) -> tuple[str, str]:
    if not isinstance(url, str):
        raise ImageRequestBudgetError("invalid_image")
    prefix, separator, data = url.partition(";base64,")
    if not prefix.startswith("data:") or not separator:
        raise ImageRequestBudgetError("unproved_image")
    return prefix[5:], data


def _content_parts(messages: object, field: str = "content") -> Iterator[dict]:
    for message in messages if isinstance(messages, list) else ():
        content = message.get(field) if isinstance(message, dict) else None
        for part in content if isinstance(content, list) else ():
            if isinstance(part, dict):
                yield part


def _wire_images(payload: dict, protocol: str) -> Iterator[tuple[object, object]]:
    """Only message image fields can use image quota, never arbitrary JSON."""
    if protocol in {"openai-completions", "mistral-conversations"}:
        for part in _content_parts(payload.get("messages")):
            if part.get("type") == "image_url":
                image = part.get("image_url")
                yield _data_url(image.get("url") if isinstance(image, dict) else image)
    elif protocol in {"openai-responses", "azure-openai-responses", "openai-codex-responses"}:
        for part in _content_parts(payload.get("input")):
            if part.get("type") == "input_image":
                yield _data_url(part.get("image_url"))
    elif protocol == "anthropic-messages":
        for part in _content_parts(payload.get("messages")):
            if part.get("type") == "image":
                source = part.get("source")
                if not isinstance(source, dict) or source.get("type") != "base64":
                    raise ImageRequestBudgetError("unproved_image")
                yield source.get("media_type"), source.get("data")
    elif protocol in {"google-generative", "google-vertex"}:
        for part in _content_parts(payload.get("contents"), "parts"):
            if "fileData" in part:
                raise ImageRequestBudgetError("unproved_image")
            if "inlineData" in part:
                image = part["inlineData"]
                if not isinstance(image, dict):
                    raise ImageRequestBudgetError("invalid_image")
                yield image.get("mimeType"), image.get("data")
    elif protocol == "bedrock-converse":
        for part in _content_parts(payload.get("messages")):
            if "image" in part:
                image = part["image"]
                source = image.get("source") if isinstance(image, dict) else None
                if not isinstance(source, dict):
                    raise ImageRequestBudgetError("invalid_image")
                yield f"image/{image.get('format')}", source.get("bytes")
    elif protocol == "pi-messages":
        context = payload.get("context")
        for part in _content_parts(context.get("messages") if isinstance(context, dict) else None):
            if part.get("type") == "image":
                yield part.get("mimeType"), part.get("data")
    else:
        raise ImageRequestBudgetError("unsupported_protocol")


def _measure_request(request: httpx.Request, protocol: str, budget: _ImageBudget) -> RequestImageUsage:
    body = request.content
    if len(body) > 1024 * 1024 + 56 * 1024:
        raise ImageRequestBudgetError("body_bytes")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImageRequestBudgetError("invalid_body") from None
    if not isinstance(payload, dict):
        raise ImageRequestBudgetError("invalid_body")
    proofs = {(proof.media_type, proof.base64_sha256, proof.encoded_bytes) for proof in budget.images}
    occurrences = encoded_bytes = 0
    for media_type, data in _wire_images(payload, protocol):
        if not isinstance(media_type, str) or media_type not in {"image/png", "image/jpeg"} or not isinstance(data, str):
            raise ImageRequestBudgetError("invalid_image")
        try:
            encoded = data.encode("ascii")
            if not encoded or len(encoded) > 1024 * 1024:
                raise ImageRequestBudgetError("image_bytes")
            base64.b64decode(encoded, validate=True)
        except (UnicodeEncodeError, binascii.Error):
            raise ImageRequestBudgetError("invalid_image") from None
        if (media_type, hashlib.sha256(encoded).hexdigest(), len(encoded)) not in proofs:
            raise ImageRequestBudgetError("unproved_image")
        occurrences += 1
        encoded_bytes += len(encoded)
    non_image = len(body) - encoded_bytes
    envelope = max(0, non_image - budget.canonical_text_bytes)
    if encoded_bytes > 1024 * 1024:
        raise ImageRequestBudgetError("image_bytes")
    if non_image > 56 * 1024:
        raise ImageRequestBudgetError("non_image_bytes")
    if envelope > 8 * 1024:
        raise ImageRequestBudgetError("envelope_bytes")
    return RequestImageUsage(protocol, len(body), occurrences, encoded_bytes,
        occurrences * 8192, non_image, envelope, hashlib.sha256(body).hexdigest())


def image_budget_client_kwargs(protocol: str) -> dict:
    """Attach to existing clients only while a caller owns this budget scope.

    HTTPX invokes request hooks for every serialized send, before transport I/O.
    The existing default transport has zero automatic connection retries.
    """
    budget = _active_budget.get()
    if budget is None:
        return {}

    async def before_send(request: httpx.Request) -> None:
        usage = _measure_request(request, protocol, budget)
        await budget.reserve(usage)

    return {"event_hooks": {"request": [before_send]}}


def _configured_protocol(args, kwargs) -> str:
    configuration = kwargs.get("configuration") if "configuration" in kwargs else args[2] if len(args) > 2 else None
    protocol = getattr(configuration, "api", None)
    return protocol if protocol in _PROTOCOLS else "unknown"


def _safe_error(error: object, protocol: str) -> str:
    text = str(error)
    own_prefix = f"image_budget:{protocol}:"
    if text.startswith(own_prefix):
        existing = text[len(own_prefix):]
        if existing in _SAFE_CODES or re.fullmatch(r"provider_error(?::HTTP[1-5]\d{2})?", existing):
            return text
    prefix = "image request budget rejected: "
    code = text[len(prefix):] if text.startswith(prefix) else ""
    if code in _SAFE_CODES:
        return f"image_budget:{protocol}:{code}"
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status is None:
        match = re.match(r"HTTP (\d{3})\b", text)
        status = int(match[1]) if match else None
    suffix = f":HTTP{status}" if isinstance(status, int) and 100 <= status <= 599 else ""
    return f"image_budget:{protocol}:provider_error{suffix}"


def budgeted_model_stream(method):
    """Project protected provider errors before they leave the AI boundary."""
    @wraps(method)
    async def stream(*args, **kwargs):
        protected = _active_budget.get() is not None
        protocol = _configured_protocol(args, kwargs)
        try:
            async with aclosing(method(*args, **kwargs)) as source:
                async for event in source:
                    if protected and getattr(event, "type", None) == "error":
                        event = replace(event, error=_safe_error(event.error, protocol))
                    yield event
        except Exception as error:
            if not protected or (isinstance(error, RuntimeError) and str(error) == "agent run cancelled"):
                raise
            from .model_types import ModelStreamError
            yield ModelStreamError(_safe_error(error, protocol))
    return stream


def budgeted_model_completion(method):
    @wraps(method)
    async def complete(*args, **kwargs):
        protected = _active_budget.get() is not None
        try:
            return await method(*args, **kwargs)
        except Exception as error:
            if not protected or (isinstance(error, RuntimeError) and str(error) == "agent run cancelled"):
                raise
            raise RuntimeError(_safe_error(error, _configured_protocol(args, kwargs))) from None
    return complete
