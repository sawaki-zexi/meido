"""Bounded response decoding straight into deterministic role attachment staging."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import warnings
import zipfile
from collections.abc import Iterator
from pathlib import Path

from PIL import Image

from ..chat_attachments import public_attachment, resolve_attachment

MAX_DECODED_BYTES = 64 * 1024 * 1024
MAX_ITEMS = 16


def _images(spool: Path, media_type: str) -> Iterator[bytes]:
    if spool.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('response budget exceeded')
    if media_type in {'application/zip', 'application/x-zip-compressed'}:
        with zipfile.ZipFile(spool) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_ITEMS or any(item.is_dir() or item.flag_bits & 1 for item in entries):
                raise ValueError('invalid image archive entries')
            total = 0
            for entry in entries:
                content = bytearray()
                with archive.open(entry) as source:
                    while chunk := source.read(64 * 1024):
                        total += len(chunk)
                        if total > MAX_DECODED_BYTES:
                            raise ValueError('decoded image budget exceeded')
                        content.extend(chunk)
                yield bytes(content)
    elif media_type == 'application/json':
        try:
            value = json.loads(spool.read_bytes())
        except (ValueError, RecursionError):
            raise ValueError('invalid image response') from None
        if not isinstance(value, dict) or set(value) != {'images'}:
            raise ValueError('invalid image response')
        images = value['images']
        if not isinstance(images, list) or not 1 <= len(images) <= MAX_ITEMS:
            raise ValueError('invalid image response')
        seen, total = set(), 0
        for image in images:
            if (not isinstance(image, dict) or set(image) != {'image', 'index', 'seed'}
                    or not isinstance(image['image'], str)
                    or type(image['index']) is not int or not 0 <= image['index'] < MAX_ITEMS
                    or image['index'] in seen or type(image['seed']) is not int or not 0 <= image['seed'] <= 2**32-1):
                raise ValueError('invalid image response')
            seen.add(image['index'])
            try:
                decoded = base64.b64decode(image['image'], validate=True)
            except (ValueError, binascii.Error):
                raise ValueError('invalid image encoding') from None
            total += len(decoded)
            if total > MAX_DECODED_BYTES:
                raise ValueError('decoded image budget exceeded')
            yield decoded
    elif media_type in {'image/png', 'image/jpeg', 'image/webp'}:
        yield spool.read_bytes()
    else:
        raise ValueError('unsupported image response')


def validate_image(content: bytes, *, max_pixels: int) -> tuple[str, str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > max_pixels or image.width < 1 or image.height < 1 or getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('image pixel budget exceeded')
                kind = image.format
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
        return {'PNG': ('image/png', '.png'), 'JPEG': ('image/jpeg', '.jpg'), 'WEBP': ('image/webp', '.webp')}[kind]
    except (OSError, ValueError, KeyError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValueError('invalid or oversized generated image') from None


def owned_outputs(workspace: Path, attempt_id: str, *, max_pixels: int):
    """Account for only this attempt's deterministic files, including orphans."""
    directory = workspace / 'attachments'
    if not directory.resolve().is_relative_to(workspace.resolve()):
        raise PermissionError('generated attachments are outside their role')
    attachments, size = [], 0
    for index in range(MAX_ITEMS):
        identifier = hashlib.sha256(f'{attempt_id}:{index}'.encode()).hexdigest()[:32]
        owned = []
        for suffix in ('.png', '.jpg', '.webp'):
            path = directory / f'{identifier}-generated-{index + 1}{suffix}'
            if path.is_file() and not path.is_symlink():
                size += path.stat().st_size
                owned.append(path.resolve())
        item = resolve_attachment(workspace, identifier)
        item_path = item.get('path') if item is not None else None
        if item is None or item.get('id') != identifier or not isinstance(item_path, str) or Path(item_path) not in owned:
            continue
        content = Path(item_path).read_bytes()
        try:
            media_type, _ = validate_image(content, max_pixels=max_pixels)
        except ValueError:
            continue
        if item.get('size') == len(content) and item.get('mediaType') == media_type and item.get('kind') == 'image':
            attachments.append(public_attachment(item))
    return attachments, size


def import_response(spool: Path, media_type: str, workspace: Path, attempt_id: str, *, max_pixels: int) -> list[dict[str, object]]:
    directory = workspace / 'attachments'
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(workspace.resolve()):
        raise PermissionError('generated attachment directory is outside its role')
    result = []
    try:
        for index, content in enumerate(_images(spool, media_type)):
            kind, suffix = validate_image(content, max_pixels=max_pixels)
            attachment_id = hashlib.sha256(f'{attempt_id}:{index}'.encode()).hexdigest()[:32]
            name = f'generated-{index + 1}{suffix}'
            target = directory / f'{attachment_id}-{name}'
            staging = target.with_suffix(target.suffix + '.partial')
            metadata_path = directory / f'{attachment_id}.json'
            metadata_staging = metadata_path.with_suffix('.partial')
            metadata = {'id': attachment_id, 'name': name, 'mediaType': kind, 'size': len(content),
                        'kind': 'image', 'path': str(target.resolve()), 'text': ''}
            try:
                if target.exists():
                    if target.is_symlink() or target.stat().st_size != len(content):
                        raise OSError('generated attachment conflicts with an existing file')
                    with target.open('rb') as source:
                        if hashlib.file_digest(source, 'sha256').digest() != hashlib.sha256(content).digest():
                            raise OSError('generated attachment conflicts with an existing file')
                else:
                    with staging.open('wb') as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(staging, target)
                metadata_staging.write_text(json.dumps(metadata), encoding='utf-8')
                os.replace(metadata_staging, metadata_path)
            finally:
                staging.unlink(missing_ok=True)
                metadata_staging.unlink(missing_ok=True)
            result.append(public_attachment(metadata))
    except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError):
        raise ValueError('invalid image archive') from None
    if not result:
        raise ValueError('empty image response')
    return result
