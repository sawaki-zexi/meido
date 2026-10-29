from __future__ import annotations

import base64
import io
import hashlib
import mimetypes
import os
import stat
import json
import re
import uuid
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

from fastapi import HTTPException, Request

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_TEXT = 32_000
MAX_GIF_DECODED_PIXELS = 40_000_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json"}
DOCUMENT_SUFFIXES = {".pdf", ".docx"}
PUBLIC_FIELDS = ("id", "name", "mediaType", "size", "kind")
MEDIA_TYPES_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _safe_name(name: str) -> str:
    value = Path(name or "attachment").name.strip()
    return value[:160] or "attachment"


def _image_signature(data: bytes, media_type: str) -> bool:
    return (
        media_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")
    ) or (
        media_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff")
    ) or (
        media_type == "image/gif" and data[:6] in {b"GIF87a", b"GIF89a"}
    ) or (
        media_type == "image/webp" and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    ) or (media_type == "image/bmp" and data.startswith(b"BM"))


def _extract_text(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return data.decode("utf-8", errors="replace")[:MAX_ATTACHMENT_TEXT]
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                xml = archive.read("word/document.xml")
            root = ElementTree.fromstring(xml)
            text = " ".join(item.text or "" for item in root.iter() if item.tag.endswith("}t"))
            return text[:MAX_ATTACHMENT_TEXT]
        except (KeyError, OSError, ValueError, ElementTree.ParseError):
            raise HTTPException(status_code=415, detail="DOCX 附件无法读取")
    if suffix == ".pdf":
        text_parts: list[str] = []
        for match in re.finditer(rb"(<<.*?>>)\s*stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
            dictionary, stream = match.group(1), match.group(2)
            if b"/FlateDecode" in dictionary:
                try:
                    stream = zlib.decompress(stream)
                except zlib.error:
                    continue
            text_parts.extend(_pdf_literal_strings(stream))
        if not text_parts:
            text_parts.extend(_pdf_literal_strings(data))
        text = " ".join(text_parts)
        return text[:MAX_ATTACHMENT_TEXT]
    return ""


def _pdf_literal_strings(data: bytes) -> list[str]:
    values: list[str] = []
    for match in re.finditer(rb"(?<!\\)\(((?:\\.|[^()])*)\)", data):
        value = match.group(1)
        value = re.sub(rb"\\([\\()\\\\])", rb"\1", value)
        value = re.sub(rb"\\[nrt]", lambda item: {b"\\n": b"\n", b"\\r": b"\r", b"\\t": b"\t"}[item.group(0)], value)
        values.append(value.decode("utf-8", errors="replace"))
    return values


async def read_request_bytes(request: Request) -> bytes:
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="附件不能超过 10 MB")
        content.extend(chunk)
    if not content:
        raise HTTPException(status_code=422, detail="附件不能为空")
    return bytes(content)


def save_attachment(root: Path, filename: str, media_type: str, data: bytes, *, output: bool = False) -> dict[str, object]:
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="附件不能超过 10 MB")
    name = _safe_name(filename)
    suffix = Path(name).suffix.lower()
    normalized_type = MEDIA_TYPES_BY_SUFFIX.get(suffix)
    if normalized_type is None and output:
        normalized_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if normalized_type is None:
        raise HTTPException(status_code=415, detail="仅支持 PNG、JPEG、WebP、GIF、BMP、TXT、MD、CSV、JSON、PDF 或 DOCX")
    claimed_type = media_type.split(";", 1)[0].strip().lower()
    accepted_claims = {normalized_type, "application/octet-stream"}
    if normalized_type == "image/jpeg":
        accepted_claims.add("image/jpg")
    if claimed_type and claimed_type not in accepted_claims:
        raise HTTPException(status_code=415, detail="文件扩展名与媒体类型不匹配")
    if normalized_type in IMAGE_TYPES and not _image_signature(data, normalized_type):
        raise HTTPException(status_code=415, detail="图片内容与文件格式不匹配")
    if normalized_type == "image/gif":
        # Validate the animation, but persist the untouched original rather than
        # a preview. Bound cumulative decoding work as well as uploaded bytes.
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as image:
                decoded_pixels = 0
                frame = 0
                while True:
                    decoded_pixels += image.width * image.height
                    if decoded_pixels > MAX_GIF_DECODED_PIXELS:
                        raise ValueError("GIF decoded pixels exceed 40 million")
                    image.load()
                    frame += 1
                    try:
                        image.seek(frame)
                    except EOFError:
                        break
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            raise HTTPException(status_code=415, detail="GIF 附件损坏或解码总量超过 4000 万像素") from error
    if not output and suffix == ".pdf" and not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="PDF 附件无法读取")
    if not output and suffix == ".docx" and not zipfile.is_zipfile(io.BytesIO(data)):
        raise HTTPException(status_code=415, detail="DOCX 附件无法读取")
    # Stable output references make repeated send preparation reuse the same owned bytes.
    attachment_id = (hashlib.sha256(name.encode("utf-8") + b"\0" + data).hexdigest()[:32]
                     if output else uuid.uuid4().hex)
    directory = root / "attachments"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{attachment_id}-{name}"
    metadata_path = directory / f"{attachment_id}.json"
    if output:
        for target in (directory, path, metadata_path):
            if target.is_symlink() or target.is_junction() or (target.is_file() and target.stat().st_nlink > 1):
                raise ValueError('output attachment storage was redirected')
    path.write_bytes(data)
    result = {
        "id": attachment_id,
        "name": name,
        "mediaType": normalized_type,
        "size": len(data),
        "path": str(path.resolve()),
        "kind": "image" if normalized_type in IMAGE_TYPES else "file",
        # Sending a file does not request document extraction or model ingestion.
        "text": "" if output else _extract_text(name, data),
    }
    (directory / f"{attachment_id}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def public_attachment(item: dict[str, object]) -> dict[str, object]:
    """Return the attachment contract safe to send to the browser."""

    return {field: item[field] for field in PUBLIC_FIELDS if field in item}


def resolve_attachment(root: Path, attachment_id: str) -> dict[str, object] | None:
    if not re.fullmatch(r"[0-9a-f]{32}", attachment_id):
        return None
    metadata = root / "attachments" / f"{attachment_id}.json"
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    if not isinstance(path, str):
        return None
    try:
        resolved = Path(path).resolve()
        attachments_root = (root / "attachments").resolve()
        resolved.relative_to(attachments_root)
    except (OSError, ValueError):
        return None
    value["path"] = str(resolved)
    return value


def load_image(path: str, media_type: str) -> tuple[str, str]:
    data = Path(path).read_bytes()
    return media_type, base64.b64encode(data).decode("ascii")


async def prepare_output_attachment(root: Path, reference: str, *, kind: str | None = None,
                                    allow_local: bool = True, authorized=lambda: True) -> dict[str, object]:
    """Adopt an explicit send's ID, workspace file, or public image URL.

    Inspection results are not deliveries. Local paths obey the existing role
    file-tool boundary; remote images use the same DNS-pinned HTTP reader as web_fetch.
    """
    if not authorized():
        raise PermissionError('media source unavailable')
    root = Path(os.path.abspath(root))
    directory = root / 'attachments'
    if directory.is_symlink() or directory.is_junction():
        raise ValueError('attachment directory was redirected')
    source = reference.removeprefix('attachment:')
    if re.fullmatch(r'[0-9a-f]{32}', source):
        item = resolve_attachment(root, source)
        if item is None or item.get('id') != source or not Path(str(item['path'])).is_file():
            raise ValueError('attachment unavailable')
    elif source.startswith(('http://', 'https://')):
        if kind == 'file':
            raise ValueError('remote sources must be images')
        from .network_tools import download_public_bytes
        data, _headers, final_url = await download_public_bytes(source)
        image_type = next((value for value in IMAGE_TYPES if _image_signature(data, value)), None)
        if image_type is None:
            raise ValueError('URL did not return a supported image')
        from urllib.parse import unquote, urlsplit
        suffix = next(key for key, value in MEDIA_TYPES_BY_SUFFIX.items() if value == image_type)
        name = Path(unquote(urlsplit(final_url).path)).stem or 'image'
        if not authorized():
            raise PermissionError('media source unavailable')
        item = save_attachment(root, name + suffix, image_type, data, output=True)
    else:
        if not allow_local:
            raise PermissionError('external turns cannot read role workspace files')
        path = Path(source)
        if not path.is_absolute():
            path = root / path
        if os.name == 'nt':
            # Windows normalization can erase trailing-dot aliases; use the
            # same lexical rejection as the existing role file tools first.
            reserved = {'CON', 'PRN', 'AUX', 'NUL',
                *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
            for name in path.parts:
                if name not in {path.anchor, '.', '..'} and (':' in name or name.rstrip(' .') != name
                        or name.split('.')[0].upper() in reserved):
                    raise PermissionError('unsupported Windows output path')
        path = Path(os.path.abspath(path))
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise PermissionError('file is outside this role workspace') from None
        for part in (root, *(root / Path(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1))):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                raise PermissionError('linked files are not role-owned output')
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise PermissionError('file is not a role workspace file')
        with path.open('rb') as stream:
            data = stream.read(MAX_ATTACHMENT_BYTES + 1)
        if not authorized():
            raise PermissionError('media source unavailable')
        item = save_attachment(root, path.name, '', data, output=True)
    if kind is not None and item.get('kind') != kind:
        raise ValueError('attachment kind does not match')
    if not authorized():
        raise PermissionError('media source unavailable')
    return item
