"""Role-bound file tools; Shiori-compatible behavior on Meido's tool seam.

Behavior reference: Shiori-Agent 4f266ae, apps/backend/agent/tools/filesystem.py.
Path authorization, bounded I/O and structured results are Meido-owned.
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import io
import os
import stat
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
import copy
from dataclasses import replace
from pathlib import Path
from typing import cast

from PIL import Image, ImageOps, UnidentifiedImageError

from .provider import CancellationToken
from .tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from .types import ImageContent

READ_MAX_BYTES = 10_000
READ_MAX_LINES = 400
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
OUTPUT_LIMIT = 12_000
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_BASE64 = 8_000_000
MAX_DIRECTORY_ENTRIES = 400
MAX_DIRECTORY_SCAN = 5_000
_LOCK_GUARD = threading.Lock()
_FILE_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
})


class _OperationSignal(CancellationToken):
    """Stop a timed-out worker without cancelling its parent agent run."""

    def __init__(self, parent: CancellationToken):
        super().__init__()
        self.parent = parent

    def is_cancelled(self) -> bool:
        return super().is_cancelled() or self.parent.is_cancelled()


@contextmanager
def _file_lock(path: Path, context: ToolContext, *, wait_seconds=None):
    key = os.path.normcase(str(path))
    with _LOCK_GUARD:
        lock, users = _FILE_LOCKS.get(key, (threading.Lock(), 0))
        _FILE_LOCKS[key] = lock, users + 1
    acquired = False
    deadline = time.monotonic() + wait_seconds if wait_seconds is not None else None
    try:
        while not acquired:
            if context.signal.is_cancelled():
                raise _FileFailure("File operation cancelled.", "cancelled")
            acquired = lock.acquire(timeout=.05)
            if not acquired and deadline is not None and time.monotonic() >= deadline:
                raise _FileFailure('path_busy: file publication is currently busy; retry.', 'path_busy')
        yield
    finally:
        if acquired:
            lock.release()
        with _LOCK_GUARD:
            _, users = _FILE_LOCKS[key]
            if users == 1:
                del _FILE_LOCKS[key]
            else:
                _FILE_LOCKS[key] = lock, users - 1


class _FileFailure(Exception):
    def __init__(self, message: str, status: str = "failed", **details: object):
        super().__init__(message)
        self.status, self.details = status, details


def _binary(data: bytes) -> bool:
    return b"\0" in data or sum(byte < 32 and byte not in b"\t\n\r\f\b" for byte in data) > len(data) * .3


class _FileTool:
    definition: ToolDefinition
    def __init__(self, role_id: str, workspace: str | Path, *, is_owner: bool = True,
                 authorized=None, max_file_bytes: int | None = None, write_policy=None, skill_versions=None):
        self.role_id = role_id
        self.workspace = Path(os.path.abspath(workspace))
        self.is_owner = is_owner
        self.authorized, self._max_file_bytes = authorized, max_file_bytes
        self.write_policy = write_policy
        self.skill_versions = {} if skill_versions is None else skill_versions
        if max_file_bytes is not None and self.definition.name in {'write_file', 'edit_file'}:
            schema = copy.deepcopy(self.definition.input_schema)
            properties = cast(dict[str, dict[str, object]], schema['properties'])
            for name in ('content', 'old_text', 'new_text'):
                if name in properties:
                    properties[name]['maxLength'] = max_file_bytes
            self.definition = replace(self.definition, input_schema=schema,
                description=self.definition.description.replace('Maximum 4 MiB.', f'Maximum {max_file_bytes} UTF-8 bytes.'))

    @property
    def max_file_bytes(self):
        return MAX_FILE_BYTES if self._max_file_bytes is None else self._max_file_bytes

    def with_write_policy(self, policy):
        tool = copy.copy(self)
        tool.write_policy = policy
        return tool

    def _skill_file(self, path):
        parts = path.relative_to(self.workspace).parts
        return bool(parts and parts[0].casefold() == 'skills' and
            (len(parts) >= 3 or len(parts) == 2 and parts[1].casefold() == 'skill.md'))

    def _skill_header(self, path):
        return self._skill_file(path) and len(path.relative_to(self.workspace).parts) in {2, 3} and path.name.casefold() == 'skill.md'

    def _skill_version_key(self, path, context):
        return context.role_id, context.session_key, context.run_id, os.path.normcase(str(path))

    def _check_skill_version(self, path, context):
        from .skills import _hash_bounded_file
        key = self._skill_version_key(path, context)
        expected = self.skill_versions.get(key)
        exists = path.exists()
        if ((exists and (expected is None or _hash_bounded_file(path)[1] != expected))
                or not exists and expected is not None):
            raise _FileFailure('Skill file conflict: read_file the current file in this run before updating; preserve the newer version.', 'failed', error_code='conflict')

    def _check(self, context: ToolContext) -> None:
        if self.authorized is None and not self.is_owner:
            raise _FileFailure("File access denied: only an owner turn may access role files.", "denied")
        if self.authorized is not None and not self.authorized(context):
            raise _FileFailure('File access denied: the host scope was revoked.', 'denied')
        if context.role_id != self.role_id:
            raise _FileFailure("File access denied: role identity does not match.", "denied")
        if context.signal.is_cancelled():
            raise _FileFailure("File operation cancelled.", "cancelled")

    def _path(self, raw: str) -> Path:
        if not raw or "\0" in raw:
            raise _FileFailure("Invalid file path.", "denied")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        if os.name == "nt":
            # GetFullPathNameW (used by abspath) strips trailing dots before
            # access. Reject these aliases before normalization loses them.
            for name in candidate.parts:
                if name in {candidate.anchor, ".", ".."}:
                    continue
                if ":" in name or name.rstrip(" .") != name or name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
                    raise _FileFailure("File access denied: unsupported Windows path.", "denied")
        candidate = Path(os.path.abspath(candidate))
        try:
            relative = candidate.relative_to(self.workspace)
        except ValueError:
            raise _FileFailure("File access denied: path is outside this role workspace.", "denied") from None
        # Check the lexical chain before resolve: a symlink pointing back into
        # the workspace is still not an authorization mechanism.
        chain = (self.workspace, *(self.workspace / Path(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1)))
        for part in chain:
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise _FileFailure("File access denied: symbolic links and reparse points are not permitted.", "denied")
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise _FileFailure("File access denied: multiply linked files are not permitted.", "denied")
        if not candidate.resolve().is_relative_to(self.workspace.resolve()):
            raise _FileFailure("File access denied: resolved path is outside the workspace.", "denied")
        return candidate

    def _result(self, content: str, *, status="succeeded", **details: object) -> AgentToolResult:
        truncated = len(content) > OUTPUT_LIMIT
        if truncated:
            content = content[:OUTPUT_LIMIT - 40] + "\n[Output truncated at character limit.]"
        metadata = {"status": status, **details, "truncated": truncated or bool(details.get("truncated"))}
        if truncated:
            metadata["truncatedBy"] = "output_limit"
        return AgentToolResult(content, metadata, status != "succeeded")

    async def execute(self, arguments: Mapping[str, object], context: ToolContext, on_update=None) -> AgentToolResult:
        del on_update
        signal = _OperationSignal(context.signal)
        operation = asyncio.create_task(asyncio.to_thread(self._run, dict(arguments), replace(context, signal=signal)))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            signal.cancel()
            await asyncio.shield(operation)
            raise

    def _run(self, arguments: Mapping[str, object], context: ToolContext) -> AgentToolResult:
        try:
            self._check(context)
            ToolRegistry.validate_definition(self.definition, dict(arguments))
            path = self._path(arguments["path"])
            skill_transaction = nullcontext()
            if self._skill_file(path):
                from ..skill_library import skill_file_transaction
                skill_transaction = skill_file_transaction(context.signal)
            file_transaction = nullcontext() if getattr(self.write_policy, 'short_publication', False) else _file_lock(path, context)
            with skill_transaction, file_transaction:
                self._path(str(path))
                result = self._execute(path, arguments, context)
                if (self.definition.name == 'read_file' and self._skill_file(path) and not result.is_error
                        and isinstance(result.details.get('contentHash'), str)):
                    self.skill_versions[self._skill_version_key(path, context)] = result.details['contentHash']
                relative = path.relative_to(self.workspace)
                if self.definition.name == 'read_file' and self._skill_header(path):
                    from .skills import SkillRegistry
                    registry = SkillRegistry.discover(self.workspace / relative.parts[0], source='role')
                    identity = os.path.normcase(os.path.join(*relative.parts[1:]))
                    skill = next((item for item in registry.skills if os.path.normcase(os.path.normpath(item.path)) == identity), None)
                    problems = [item['error'] for item in registry.diagnostics if item.get('path') and Path(item['path']) == path]
                    if skill is None:
                        problems = problems or ['skill format validation failed']
                    else:
                        try:
                            skill.materialize()
                        except (ValueError, OSError, UnicodeError) as error:
                            problems.append(str(error))
                        if skill.content_hash != result.details.get('contentHash'):
                            problems.append('skill changed during read; read_file again before verifying or updating')
                    verified = not problems and not result.details.get('truncated')
                    result = replace(result, content=result.content + ('\nSkill saved-file validation: verified.' if verified
                        else '\nSkill saved-file validation incomplete: ' + '; '.join(problems or ['read all pages before claiming full review'])),
                        details={**result.details, 'skillValidation': {'verified': verified,
                            'contentHash': skill.content_hash if skill else None, 'problems': problems}})
                return result
        except _FileFailure as error:
            return self._result(str(error), status=error.status, **error.details)
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            return self._result(f"File operation failed: {error}", status="failed")

    def _read_bytes(self, path: Path, context: ToolContext, *, max_bytes: int | None = None) -> bytes:
        max_bytes = self.max_file_bytes if max_bytes is None else max_bytes
        if not path.is_file():
            raise _FileFailure("Path is not a regular file.")
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > max_bytes:
                raise _FileFailure(f"File exceeds {max_bytes} byte input limit.", "denied", truncated=True, truncatedBy="file_limit")
            chunks: list[bytes] = []
            size = 0
            while size <= max_bytes:
                self._check(context)
                chunk = stream.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > max_bytes:
                raise _FileFailure(f"File exceeds {max_bytes} byte input limit.", "denied", truncated=True, truncatedBy="file_limit")
        return b"".join(chunks)

    def _write_bytes(self, path: Path, data: bytes, context: ToolContext):
        if len(data) > self.max_file_bytes:
            raise _FileFailure(f"Content exceeds {self.max_file_bytes} byte write limit.", "denied", truncated=True, truncatedBy="file_limit")
        self._check(context)
        self._path(str(path))
        relative = path.relative_to(self.workspace)
        if self._skill_header(path):
            from .skills import validate_skill_text
            metadata = validate_skill_text(data.decode('utf-8-sig'))
            directory_id = relative.parts[1].casefold() if os.name == 'nt' else relative.parts[1]
            if len(relative.parts) == 3 and metadata['id'] != directory_id:
                raise _FileFailure('Skill ID must match its skills/<skill-id>/SKILL.md directory.', 'failed')
        with self.write_policy(path, data, context) if self.write_policy is not None else nullcontext() as allocation:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._path(str(path))
            if allocation is not None:
                allocation.check()
            temporary = allocation.temporary if allocation is not None else path.parent / f".meido-{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("xb") as stream:
                    if allocation is not None:
                        allocation.opened(stream)
                    for start in range(0, len(data), 65536):
                        self._check(context)
                        stream.write(data[start:start + 65536])
                    stream.flush()
                    os.fsync(stream.fileno())
                prepared = getattr(allocation, 'prepared', None)
                if prepared is not None:
                    prepared()
                publication = getattr(allocation, 'publication', None)
                with publication() if publication is not None else nullcontext():
                    self._check(context)
                    self._path(str(path))
                    if allocation is not None:
                        allocation.check()
                    if self._skill_file(path):
                        self._check_skill_version(path, context)
                    os.replace(temporary, path)
                    if self._skill_file(path):
                        self.skill_versions.pop(self._skill_version_key(path, context), None)
                    if allocation is not None:
                        allocation.committed()
            finally:
                if allocation is not None:
                    allocation.cleanup()
                else:
                    temporary.unlink(missing_ok=True)
            return allocation.receipt() if allocation is not None else None


class ReadFileTool(_FileTool):
    definition = ToolDefinition(
        "read_file", "Read role-workspace text with 1-based line numbers, offset (0-based), and optional limit. Use limit=50 for a preview, then paginate relevant sections; independent files may be read together. Pages are bounded to 400 lines / 10 KB. Read each existing ordinary skill file/resource in the current run before write_file or edit_file; the host holds its version and rejects stale updates. Images are returned to vision models; binary files are rejected. Paths outside this role workspace are not accessible.",
        {"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
        }, "required": ["path"], "additionalProperties": False},
        source="role", output_limit=OUTPUT_LIMIT, timeout_seconds=15,
    )

    def __init__(self, role_id: str, workspace: str | Path, *, multimodal: bool = True, is_owner: bool = True,
                 authorized=None, max_file_bytes: int | None = None, write_policy=None, skill_versions=None):
        super().__init__(role_id, workspace, is_owner=is_owner, authorized=authorized, max_file_bytes=max_file_bytes, write_policy=write_policy, skill_versions=skill_versions)
        self.multimodal = multimodal

    def _image(self, path: Path, context: ToolContext, source: str) -> AgentToolResult:
        if not self.multimodal:
            raise _FileFailure("Image detected; a vision-capable model is required to inspect it.", "denied", path=source)
        raw = self._read_bytes(path, context, max_bytes=MAX_IMAGE_BYTES)
        source_hash = hashlib.sha256(raw).hexdigest() if self._skill_file(path) else None
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise _FileFailure("Image exceeds 40 million pixel limit.", "denied", path=source)
                image.verify()
            compressed = len(raw) * 4 // 3 > MAX_IMAGE_BASE64
            if compressed:
                with Image.open(io.BytesIO(raw)) as image:
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((1568, 1568))
                    canvas = Image.new("RGB", image.size, "white")
                    canvas.paste(image.convert("RGB"), mask=image.getchannel("A") if "A" in image.getbands() else None)
                    buffer = io.BytesIO()
                    canvas.save(buffer, "JPEG", quality=85, optimize=True)
                    raw, media_type = buffer.getvalue(), "image/jpeg"
            else:
                with Image.open(io.BytesIO(raw)) as image:
                    media_type = Image.MIME.get(image.format, "")
            self._check(context)
            encoded = base64.b64encode(raw).decode("ascii")
            if len(encoded) > MAX_IMAGE_BASE64:
                raise _FileFailure("Image result exceeds 8 MB encoded output limit.", "denied", path=source, truncated=True)
            return AgentToolResult(
                f"[{source}] Image provided to the vision model" + (" (compressed)." if compressed else "."),
                {"status": "succeeded", "path": source, "truncated": False, "compressed": compressed, "mediaType": media_type,
                 **({'contentHash': source_hash} if source_hash is not None else {})},
                images=(ImageContent(media_type, encoded, "high"),),
            )
        except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise _FileFailure(f"Image cannot be decoded safely: {error}", path=source) from error

    def _execute(self, path: Path, arguments: Mapping[str, object], context: ToolContext) -> AgentToolResult:
        source = path.relative_to(self.workspace).as_posix() or "."
        offset, limit = arguments.get("offset", 0), arguments.get("limit", READ_MAX_LINES)
        output_budget = min(READ_MAX_BYTES, OUTPUT_LIMIT - len(source) - 1000)
        selected: list[str] = []
        used = total_lines = scanned = 0
        had_errors = False
        reason = None
        digest = hashlib.sha256() if self._skill_file(path) else None
        if not path.is_file():
            raise _FileFailure("Path is not a regular file.")
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise _FileFailure("Path is not a regular file.")
            if self._max_file_bytes is not None and info.st_size > self.max_file_bytes:
                raise _FileFailure('File exceeds the host file-byte limit.', 'denied', path=source)
            head = stream.read(16)
            if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or head.startswith(b"RIFF") and head[8:12] == b"WEBP":
                return self._image(path, context, source)
            stream.seek(0)
            while scanned < MAX_SCAN_BYTES:
                self._check(context)
                if reason:
                    # Once a page is complete, count remaining line endings in
                    # bounded chunks rather than allocating/decoding each line.
                    tail = b""
                    while scanned < MAX_SCAN_BYTES:
                        self._check(context)
                        chunk = stream.read(min(65536, MAX_SCAN_BYTES - scanned))
                        if not chunk:
                            break
                        scanned += len(chunk)
                        if digest is not None:
                            digest.update(chunk)
                        total_lines += chunk.count(b"\n")
                        tail = chunk[-1:]
                    if tail and tail != b"\n" and scanned >= info.st_size:
                        total_lines += 1
                    break
                raw = stream.readline(min(READ_MAX_BYTES + 1, MAX_SCAN_BYTES - scanned))
                if not raw:
                    break
                scanned += len(raw)
                if digest is not None:
                    digest.update(raw)
                long_line = len(raw) > READ_MAX_BYTES or not raw.endswith(b"\n") and scanned < info.st_size
                while not raw.endswith(b"\n") and scanned < min(info.st_size, MAX_SCAN_BYTES):
                    self._check(context)
                    chunk = stream.readline(min(READ_MAX_BYTES + 1, MAX_SCAN_BYTES - scanned))
                    if not chunk:
                        break
                    scanned += len(chunk)
                    if digest is not None:
                        digest.update(chunk)
                    if chunk.endswith(b"\n"):
                        break
                total_lines += 1
                if total_lines == 1:
                    raw = raw.removeprefix(b"\xef\xbb\xbf")
                if _binary(raw):
                    raise _FileFailure("Binary file refused: read_file accepts text and supported images only.", "failed", path=source)
                if total_lines <= offset or reason:
                    continue
                if len(selected) >= min(limit, READ_MAX_LINES):
                    reason = "page_limit" if limit <= READ_MAX_LINES else "line_limit"
                    continue
                if long_line:
                    reason = "long_line"
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("utf-8", errors="replace")
                    had_errors = True
                numbered = f"{total_lines:6}→{text}"
                if used + len(numbered.encode("utf-8")) > output_budget:
                    reason = "byte_limit"
                    continue
                selected.append(numbered)
                used += len(numbered.encode("utf-8"))
        scan_limited = scanned < info.st_size
        reason = reason or ("scan_limit" if scan_limited else None)
        body = "".join(selected)
        next_offset = offset + len(selected)
        if reason == "long_line":
            note = "Line exceeds 10 KB; use an approved command tool for byte inspection."
        elif reason:
            note = f"Truncated: {reason}; continue with offset={next_offset} limit=50."
        else:
            note = "End of file."
        if had_errors:
            note += " Invalid UTF-8 bytes shown with replacement characters."
        return self._result(
            f"[{source}]\n{body}\n[{note}]", path=source, truncated=bool(reason),
            totalLines=None if scan_limited else total_lines, totalBytes=info.st_size,
            outputLines=len(selected), outputBytes=used, nextOffset=next_offset,
            truncatedBy=reason, hadDecodeErrors=had_errors,
            **({'contentHash': digest.hexdigest()} if digest is not None and not scan_limited and not had_errors else {}),
        )


class WriteFileTool(_FileTool):
    definition = ToolDefinition(
        "write_file", "Create or fully overwrite a UTF-8 file in this role workspace, creating missing parent directories. Prefer edit_file for precise changes and read existing files before overwriting. Existing ordinary skills and supporting files require read_file in this run; a conflict requires a fresh read and reconciliation. Do not proactively create documentation or README files unless requested. Maximum 4 MiB.",
        {"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "content": {"type": "string", "maxLength": MAX_FILE_BYTES},
        }, "required": ["path", "content"], "additionalProperties": False},
        risk="mutating", source="role", output_limit=OUTPUT_LIMIT, timeout_seconds=15,
    )

    def _execute(self, path: Path, arguments: Mapping[str, object], context: ToolContext) -> AgentToolResult:
        data = arguments["content"].encode("utf-8")
        publication = self._write_bytes(path, data, context)
        source = path.relative_to(self.workspace).as_posix()
        return self._result(f"Wrote {len(data)} UTF-8 bytes to {source}.", path=source, bytesWritten=len(data),
            **({'publication': publication} if publication is not None else {}))


class EditFileTool(_FileTool):
    definition = ToolDefinition(
        "edit_file", "Precisely replace raw old_text with new_text in a role-workspace UTF-8 file and return a unified diff. Do not include read_file line-number prefixes. Existing ordinary skills and supporting files require read_file in this run; stale versions are refused. Missing or ambiguous matches are refused; replace_all explicitly permits multiple matches. Preserves BOM and pure CRLF files.",
        {"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "old_text": {"type": "string", "minLength": 1, "maxLength": MAX_FILE_BYTES},
            "new_text": {"type": "string", "maxLength": MAX_FILE_BYTES},
            "replace_all": {"type": "boolean"},
        }, "required": ["path", "old_text", "new_text"], "additionalProperties": False},
        risk="mutating", source="role", output_limit=OUTPUT_LIMIT, timeout_seconds=15,
    )

    def _execute(self, path: Path, arguments: Mapping[str, object], context: ToolContext) -> AgentToolResult:
        raw = self._read_bytes(path, context)
        if _binary(raw):
            raise _FileFailure("Binary file refused: edit_file requires UTF-8 text.")
        bom = raw.startswith(b"\xef\xbb\xbf")
        content = raw.decode("utf-8-sig")
        old, new = arguments["old_text"], arguments["new_text"]
        pure_crlf = "\r\n" in content and "\n" not in content.replace("\r\n", "") and "\r" not in content.replace("\r\n", "")
        if pure_crlf:
            old = old.replace("\r\n", "\n").replace("\n", "\r\n")
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        count = content.count(old)
        if not count:
            raise _FileFailure("old_text not found; include exact file text without line-number prefixes.", "denied", matchCount=0)
        if count > 1 and not arguments.get("replace_all", False):
            raise _FileFailure(f"old_text matches {count} times; include more context or explicitly set replace_all=true.", "denied", matchCount=count)
        replaced_count = count if arguments.get("replace_all", False) else 1
        # raw already includes the original BOM and exact newline bytes. The
        # operands reflect CRLF compatibility above; count is non-overlapping,
        # matching str.replace. Reject expansion before allocating its result.
        updated_bytes = len(raw) + replaced_count * (len(new.encode("utf-8")) - len(old.encode("utf-8")))
        if updated_bytes > self.max_file_bytes:
            raise _FileFailure(f"Edited file exceeds {self.max_file_bytes} byte write limit.", "denied", truncated=True, truncatedBy="file_limit")
        updated = content.replace(old, new, replaced_count)
        encoded = (b"\xef\xbb\xbf" if bom else b"") + updated.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise _FileFailure(f"Edited file exceeds {self.max_file_bytes} byte write limit.", "denied", truncated=True, truncatedBy="file_limit")
        source = path.relative_to(self.workspace).as_posix()
        diff_budget = min(READ_MAX_BYTES, OUTPUT_LIMIT - len(source) - 1000)
        diff_parts, diff_size, truncated = [], 0, False
        for line in difflib.unified_diff(content.splitlines(), updated.splitlines(), fromfile=f"{source} (before)", tofile=f"{source} (after)", n=2, lineterm=""):
            self._check(context)
            if diff_size + len(line) + 1 > diff_budget:
                truncated = True
                break
            diff_parts.append(line)
            diff_size += len(line) + 1
        publication = self._write_bytes(path, encoded, context)
        diff = "\n".join(diff_parts)
        note = "\n[Diff truncated at output limit.]" if truncated else ""
        return self._result(f"Edited {source}; replaced {replaced_count} match(es).\n```diff\n{diff}\n```{note}", path=source, replacedCount=replaced_count, bytesWritten=len(encoded), truncated=truncated, truncatedBy="diff_limit" if truncated else None,
            **({'publication': publication} if publication is not None else {}))


class ListDirTool(_FileTool):
    definition = ToolDefinition(
        "list_dir", "List actual files and directories one level deep in this role workspace, sorted by name. No recursion or glob expansion; large directories return an explicit bounded partial result.",
        {"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        }, "required": ["path"], "additionalProperties": False},
        source="role", output_limit=OUTPUT_LIMIT, timeout_seconds=15, search_hint='ls 查看目录',
    )

    def _execute(self, path: Path, arguments: Mapping[str, object], context: ToolContext) -> AgentToolResult:
        source = path.relative_to(self.workspace).as_posix() or "."
        entries: list[tuple[str, str]] = []
        scanned = 0
        scan_limited = False
        with os.scandir(path) as iterator:
            for entry in iterator:
                self._check(context)
                if scanned >= MAX_DIRECTORY_SCAN:
                    scan_limited = True
                    break
                scanned += 1
                info = entry.stat(follow_symlinks=False)
                linked = entry.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                prefix = "🔗 " if linked else "📁 " if entry.is_dir(follow_symlinks=False) else "📄 "
                entries.append((entry.name, prefix))
        entries.sort()
        lines, used = [], 0
        for name, prefix in entries[:MAX_DIRECTORY_ENTRIES]:
            line = prefix + name
            if used + len(line) + 1 > READ_MAX_BYTES:
                break
            lines.append(line)
            used += len(line) + 1
        truncated = scan_limited or len(lines) < len(entries)
        note = "\n[Truncated directory listing: bounded scan/output; results are partial.]" if truncated else ""
        body = "\n".join(lines) if entries else "Directory is empty."
        return self._result(f"[{source}]\n{body}{note}", path=source, truncated=truncated, scannedEntries=scanned, outputEntries=len(lines), truncatedBy="scan_limit" if scan_limited else "output_limit" if truncated else None)


FILE_TOOL_NAMES = frozenset({"read_file", "list_dir", "write_file", "edit_file"})


def build_file_tools(role_id: str, workspace: str | Path, *, multimodal: bool = True, is_owner: bool = True,
                     authorized=None, max_file_bytes: int | None = None, write_policy=None):
    skill_versions = {}
    return (
        ReadFileTool(role_id, workspace, multimodal=multimodal, is_owner=is_owner, authorized=authorized, max_file_bytes=max_file_bytes, write_policy=write_policy, skill_versions=skill_versions),
        ListDirTool(role_id, workspace, is_owner=is_owner, authorized=authorized, max_file_bytes=max_file_bytes, write_policy=write_policy, skill_versions=skill_versions),
        WriteFileTool(role_id, workspace, is_owner=is_owner, authorized=authorized, max_file_bytes=max_file_bytes, write_policy=write_policy, skill_versions=skill_versions),
        EditFileTool(role_id, workspace, is_owner=is_owner, authorized=authorized, max_file_bytes=max_file_bytes, write_policy=write_policy, skill_versions=skill_versions),
    )
