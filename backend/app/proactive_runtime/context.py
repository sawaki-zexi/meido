"""Read the host-selected context file; this seam grants no file-tool authority."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import threading


MAX_BYTES = 64 * 1024
MAX_CHARACTERS = 16000


def _empty(status: str) -> dict[str, str]:
    return {"text": "", "status": status, "digest": ""}


def freeze_context(reader, role_id: str) -> dict[str, str]:
    if reader is None:
        return _empty("missing")
    try:
        value = reader(role_id)
        text, status, digest = value["text"], value["status"], value["digest"]
        if (not isinstance(text, str) or len(text) > MAX_CHARACTERS or len(text.encode("utf-8")) > MAX_BYTES
                or status not in {"ok", "missing", "error"} or not isinstance(digest, str)
                or (digest and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)))):
            raise ValueError("invalid fixed context result")
        return {"text": text, "status": status, "digest": digest}
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        return _empty("error")


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _check_chain(path: Path) -> os.stat_result:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("an absolute host workspace is required")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("context paths cannot contain links or reparse points")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("context cannot have multiple hard links")
    if _path_key(path.resolve(strict=True)) != _path_key(path):
        raise ValueError("resolved context path differs from host path")
    return info


def _opened_path(descriptor: int) -> Path | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        final_path = kernel.GetFinalPathNameByHandleW
        final_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
        final_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        count = final_path(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
        if not count or count >= len(buffer):
            raise OSError(ctypes.get_last_error(), "cannot verify context file handle")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    descriptor_path = Path(f"/proc/self/fd/{descriptor}")
    return descriptor_path.resolve(strict=True) if descriptor_path.exists() else None


def _version(info: os.stat_result) -> tuple[int, ...]:
    # Windows lstat/fstat expose different legacy ctime semantics; identity,
    # length and modification time are comparable on both handle/path seams.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_fixed(path: Path) -> bytes:
    before = _check_chain(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
        raise ValueError("context must be one bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        actual = _opened_path(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_size > MAX_BYTES
                or _version(opened) != _version(before)
                or (actual is not None and _path_key(actual) != _path_key(path))):
            raise ValueError("context changed while opening")
        chunks, size = [], 0
        while size <= MAX_BYTES:
            chunk = os.read(descriptor, min(8192, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if (size > MAX_BYTES or _version(os.fstat(descriptor)) != _version(opened)
                or _version(_check_chain(path)) != _version(opened)):
            raise ValueError("context changed or exceeded byte limit while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


class ProactiveContextReader:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_good: dict[str, tuple[tuple, dict[str, str]]] = {}

    def forget(self, role_id: str) -> None:
        with self._lock:
            self._last_good.pop(role_id, None)

    def read(self, role_id: str, workspace: Path, *, incarnation: str) -> dict[str, str]:
        with self._lock:
            workspace = Path(workspace)
            try:
                info = _check_chain(workspace)
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("host workspace is not a directory")
                key = (incarnation, os.path.normcase(str(workspace.resolve(strict=True))), info.st_dev, info.st_ino)
            except (OSError, ValueError, RuntimeError):
                self.forget(role_id)
                return _empty("error")
            cached = self._last_good.get(role_id)
            if cached is not None and cached[0] != key:
                self.forget(role_id)
                cached = None
            try:
                raw = _read_fixed(workspace / "PROACTIVE_CONTEXT.md")
                current = _check_chain(workspace)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    self.forget(role_id)
                    cached = None
                    raise ValueError("workspace changed while reading")
                text = raw.decode("utf-8")
                if len(text) > MAX_CHARACTERS:
                    raise ValueError("context exceeds character limit")
            except FileNotFoundError:
                self.forget(role_id)
                return _empty("missing")
            except (OSError, ValueError, RuntimeError):
                return {**cached[1], "status": "error"} if cached else _empty("error")
            value = {"text": text, "status": "ok", "digest": hashlib.sha256(raw).hexdigest()}
            self._last_good[role_id] = (key, value)
            return dict(value)
