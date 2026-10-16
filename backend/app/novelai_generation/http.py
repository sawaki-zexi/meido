"""Exactly one local dispatch attempt; the remote service has no payment dedup key."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
ORIGIN = 'https://image.novelai.net'


class GenerationHttpError(RuntimeError):
    def __init__(self, category: str, *, definite_response: bool = False):
        super().__init__(category)
        self.category = category
        self.definite_response = definite_response


@dataclass(frozen=True)
class ReceivedResponse:
    media_type: str
    size: int


class NovelaiHttp:
    def __init__(self):
        self._origin = ORIGIN

    @classmethod
    def for_test(cls, origin: str) -> NovelaiHttp:
        """Construction seam only: no endpoint is accepted in owner/model requests."""
        parsed = urlsplit(origin)
        if (parsed.scheme != 'http' or parsed.username or parsed.password or parsed.path
                or parsed.query or parsed.fragment or not parsed.hostname
                or not ipaddress.ip_address(parsed.hostname).is_loopback):
            raise ValueError('fixture transport requires a loopback HTTP origin')
        value = cls()
        value._origin = origin
        return value

    async def generate(self, request: dict[str, object], token: str, destination: Path,
                       *, deadline: float) -> ReceivedResponse:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix('.partial')
        size = 0
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(30, connect=10), transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False)) as client:
                    async with client.stream('POST', self._origin + '/ai/generate-image', json=request,
                            headers={'Authorization': 'Bearer ' + token,
                                     'Accept': 'application/zip, application/json',
                                     'Accept-Encoding': 'identity'}) as response:
                        if response.status_code not in {200, 201}:
                            raise GenerationHttpError('http_' + str(response.status_code),
                                definite_response=response.status_code in {400, 401, 402, 403, 422})
                        if response.headers.get('content-encoding', 'identity').lower() not in {'identity', ''}:
                            raise GenerationHttpError('unsupported_content_encoding')
                        media_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                        if media_type not in {'application/zip', 'application/x-zip-compressed', 'application/json', 'image/png', 'image/jpeg', 'image/webp'}:
                            raise GenerationHttpError('invalid_response_type')
                        length = response.headers.get('content-length')
                        if length and length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
                            raise GenerationHttpError('response_budget_exceeded')
                        with partial.open('xb') as output:
                            async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                                size += len(chunk)
                                if size > MAX_RESPONSE_BYTES:
                                    raise GenerationHttpError('response_budget_exceeded')
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        if not size:
                            raise GenerationHttpError('empty_response')
                        os.replace(partial, destination)
                        return ReceivedResponse(media_type, size)
        except GenerationHttpError:
            raise
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            # Never include a URL, response body, token or encoded input in errors.
            raise GenerationHttpError('transport_unknown') from None
        finally:
            partial.unlink(missing_ok=True)
