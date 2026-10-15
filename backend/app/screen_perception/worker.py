"""Fixed short-lived capture entry point. No role data, network or file output."""

from __future__ import annotations

import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

MAX_RAW_PIXELS = 16_777_216
MAX_SENT_PIXELS = 4_194_304
MAX_SIDE = 2048
MAX_PNG_BYTES = 4 * 1024 * 1024


async def launch_capture_worker() -> asyncio.subprocess.Process:
    if sys.platform != "win32" or getattr(sys, "frozen", False):
        raise OSError("capture unavailable")
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP"}}
    # Isolated interpreter + absolute trusted entry; no cwd/PYTHONPATH hooks.
    return await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-B", str(Path(__file__).resolve()),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _primary_size() -> tuple[int, int]:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    session = wintypes.DWORD()
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or session.value == 0:
        raise OSError("session unavailable")
    wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    data = ctypes.c_void_p()
    length = wintypes.DWORD()
    # WTSINFOEX: Level DWORD, 8-byte-aligned Level1 union; the first three
    # Level1 DWORDs are SessionId, SessionState and SessionFlags. Windows 10+.
    if not wts.WTSQuerySessionInformationW(None, session.value, 25, ctypes.byref(data), ctypes.byref(length)):
        raise OSError("session unavailable")
    try:
        if length.value < 20:
            raise OSError("session unavailable")
        level, session_id, state, flags = struct.unpack("<I4xIII", ctypes.string_at(data, 20))
        if (level, session_id, state, flags) != (1, session.value, 0, 1):
            raise OSError("session inactive or locked")
    finally:
        wts.WTSFreeMemory(data)
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    desktop = user.OpenInputDesktop(0, False, 0x101)
    if not desktop:
        raise OSError("desktop unavailable")
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)) or name.value != "Default":
            raise OSError("secure desktop")
    finally:
        user.CloseDesktop(desktop)
    return user.GetSystemMetrics(0), user.GetSystemMetrics(1)


def _main() -> int:
    if sys.platform != "win32" or sys.getwindowsversion().major < 10:
        sys.stderr.buffer.write(b"capture_unavailable")
        return 1
    try:
        import ctypes
        from PIL import Image, ImageGrab

        user = ctypes.WinDLL("user32", use_last_error=True)
        user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if not user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            raise OSError("DPI context unavailable")
        width, height = _primary_size()
        if width <= 0 or height <= 0:
            raise OSError("invalid dimensions")
        if width * height > MAX_RAW_PIXELS:
            sys.stderr.buffer.write(b"frame_too_large")
            return 1
        with ImageGrab.grab(all_screens=False) as raw:
            if raw.size != (width, height) or _primary_size() != (width, height):
                raise OSError("primary screen changed")
            with raw.convert("RGB") as image:
                for side in (MAX_SIDE, 1536, 1024):
                    image.thumbnail((side, side), Image.Resampling.LANCZOS)
                    with BytesIO() as buffer:
                        image.save(buffer, format="PNG")
                        if buffer.tell() <= MAX_PNG_BYTES:
                            metadata = json.dumps({"width": image.width, "height": image.height, "bytes": buffer.tell()}).encode("ascii")
                            sys.stdout.buffer.write(struct.pack(">I", len(metadata)))
                            sys.stdout.buffer.write(metadata)
                            sys.stdout.buffer.write(buffer.getbuffer())
                            return 0
        sys.stderr.buffer.write(b"frame_too_large")
    except Exception:
        sys.stderr.buffer.write(b"capture_unavailable")
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
