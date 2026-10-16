"""Read-only Win32 proofs. This module never enumerates UI or injects input."""
import ctypes
from ctypes import wintypes as W
from dataclasses import dataclass
import os
import struct
import sys


@dataclass(frozen=True)
class WindowIdentity:
    pid: int
    window_id: int
    created: int
    bounds: tuple[int, int, int, int]
    dpi: int
    thread_id: int
    class_name: str
    sid: bytes
    session_id: int


class WindowsDesktop:
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Computer Use requires Windows")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        signatures = [
            (self.kernel, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.kernel, "GetCurrentProcess", [], W.HANDLE),
            (self.kernel, "GetCurrentThreadId", [], W.DWORD),
            (self.kernel, "CloseHandle", [W.HANDLE], W.BOOL),
            (self.kernel, "GetProcessTimes", [W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4, W.BOOL),
            (self.kernel, "ProcessIdToSessionId", [W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.kernel, "WTSGetActiveConsoleSessionId", [], W.DWORD),
            (self.advapi, "OpenProcessToken", [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)], W.BOOL),
            (self.advapi, "GetTokenInformation", [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.advapi, "GetLengthSid", [ctypes.c_void_p], W.DWORD),
            (self.user, "OpenInputDesktop", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.user, "CloseDesktop", [W.HANDLE], W.BOOL),
            (self.user, "GetProcessWindowStation", [], W.HANDLE),
            (self.user, "GetThreadDesktop", [W.DWORD], W.HANDLE),
            (self.user, "GetUserObjectInformationW", [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.user, "GetForegroundWindow", [], W.HWND),
            (self.user, "GetWindowThreadProcessId", [W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD),
            (self.user, "GetDpiForWindow", [W.HWND], W.UINT),
            (self.user, "IsIconic", [W.HWND], W.BOOL),
            (self.user, "IsWindowVisible", [W.HWND], W.BOOL),
            (self.user, "IsWindowEnabled", [W.HWND], W.BOOL),
            (self.user, "GetClassNameW", [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user, "SetThreadDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_void_p),
            (self.wts, "WTSQuerySessionInformationW", [W.HANDLE, W.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.wts, "WTSFreeMemory", [ctypes.c_void_p], None),
            (self.dwm, "DwmGetWindowAttribute", [W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD], ctypes.c_long),
        ]
        for dll, name, args, result in signatures:
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result

    def _session(self, pid):
        session = W.DWORD()
        if not self.kernel.ProcessIdToSessionId(pid, ctypes.byref(session)):
            raise OSError("session unavailable")
        return session.value

    def _token(self, process):
        token = W.HANDLE()
        if not self.advapi.OpenProcessToken(process, 8, ctypes.byref(token)):
            raise OSError("token unavailable")
        def info(kind):
            needed = W.DWORD()
            self.advapi.GetTokenInformation(token, kind, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            if not needed.value or not self.advapi.GetTokenInformation(token, kind, buffer, len(buffer), ctypes.byref(needed)):
                raise OSError("token information unavailable")
            return buffer
        try:
            user = info(1)
            sid_ptr = ctypes.c_void_p.from_buffer(user).value
            sid = ctypes.string_at(sid_ptr, self.advapi.GetLengthSid(sid_ptr))
            integrity = info(25)
            il_ptr = ctypes.c_void_p.from_buffer(integrity).value
            il_bytes = ctypes.string_at(il_ptr, self.advapi.GetLengthSid(il_ptr))
            level = int.from_bytes(il_bytes[-4:], "little")
            uiaccess = int.from_bytes(info(26).raw[:4], "little")
            if uiaccess or level >= 0x3000:
                raise OSError("elevated/UIAccess process unsupported")
            groups = info(2)
            count = W.DWORD.from_buffer(groups).value
            pointer_size = ctypes.sizeof(ctypes.c_void_p)
            stride = 16 if pointer_size == 8 else 8
            offset = 8 if pointer_size == 8 else 4
            logon_sids = []
            for index in range(count):
                entry = offset + index * stride
                group_sid = ctypes.c_void_p.from_buffer(groups, entry).value
                attributes = W.DWORD.from_buffer(groups, entry + pointer_size).value
                if attributes & 0xC0000000 == 0xC0000000:
                    logon_sids.append(ctypes.string_at(group_sid, self.advapi.GetLengthSid(group_sid)))
            return sid, level, logon_sids
        finally:
            self.kernel.CloseHandle(token)

    def _object(self, handle, kind):
        needed = W.DWORD()
        self.user.GetUserObjectInformationW(handle, kind, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise OSError("desktop ownership unavailable")
        buffer = ctypes.create_string_buffer(needed.value)
        if not self.user.GetUserObjectInformationW(handle, kind, buffer, len(buffer), ctypes.byref(needed)):
            raise OSError("desktop ownership unavailable")
        return ctypes.wstring_at(buffer) if kind == 2 else buffer.raw

    def validate(self):
        session = self._session(os.getpid())
        if not session or session != self.kernel.WTSGetActiveConsoleSessionId():
            raise OSError("not the active console session")
        sid, level, logon_sids = self._token(self.kernel.GetCurrentProcess())
        data, size = ctypes.c_void_p(), W.DWORD()
        if not self.wts.WTSQuerySessionInformationW(None, session, 25, ctypes.byref(data), ctypes.byref(size)):
            raise OSError("session state unavailable")
        try:
            if size.value < 20 or struct.unpack("<I4xIII", ctypes.string_at(data, 20)) != (1, session, 0, 1):
                raise OSError("session is inactive or locked")
        finally:
            self.wts.WTSFreeMemory(data)
        station = self.user.GetProcessWindowStation()
        if self._object(station, 2) != "WinSta0" or self._object(station, 4) not in [sid, *logon_sids]:
            raise OSError("not the current user's interactive station")
        desktop = self.user.OpenInputDesktop(0, False, 0x101)
        if not desktop:
            raise OSError("input desktop unavailable")
        try:
            own = self.user.GetThreadDesktop(self.kernel.GetCurrentThreadId())
            if self._object(desktop, 2) != "Default" or self._object(own, 2) != "Default":
                raise OSError("secure or foreign desktop unsupported")
        finally:
            self.user.CloseDesktop(desktop)
        return (sid, session, level)

    def inspect(self, pid, window_id):
        if type(pid) is not int or type(window_id) is not int or min(pid, window_id) <= 0:
            raise ValueError("explicit PID/HWND required")
        sid, session, own_level = self.validate()
        actual = W.DWORD()
        thread = self.user.GetWindowThreadProcessId(window_id, ctypes.byref(actual))
        if not thread or actual.value != pid or self.user.IsIconic(window_id) or not self.user.IsWindowVisible(window_id):
            raise ValueError("stale, minimized or hidden window")
        process = self.kernel.OpenProcess(0x1000, False, pid)
        if not process:
            raise OSError("target process unavailable")
        try:
            target_sid, target_level, _ = self._token(process)
            if target_sid != sid or self._session(pid) != session or target_level > own_level:
                raise OSError("foreign or elevated target")
            times = (W.FILETIME * 4)()
            if not self.kernel.GetProcessTimes(process, *(ctypes.byref(times[i]) for i in range(4))):
                raise OSError("process creation identity unavailable")
            created = times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)
        finally:
            self.kernel.CloseHandle(process)
        old = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not old:
            raise OSError("DPI context unavailable")
        try:
            rect, cloaked = W.RECT(), W.DWORD()
            if self.dwm.DwmGetWindowAttribute(window_id, 9, ctypes.byref(rect), ctypes.sizeof(rect)):
                raise OSError("physical bounds unavailable")
            if self.dwm.DwmGetWindowAttribute(window_id, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) or cloaked.value:
                raise ValueError("cloaked window unsupported")
            width, height = self.user.GetSystemMetrics(0), self.user.GetSystemMetrics(1)
            if rect.left < 0 or rect.top < 0 or rect.right > width or rect.bottom > height or rect.right <= rect.left or rect.bottom <= rect.top:
                raise ValueError("window must be entirely on the primary monitor")
            if (rect.right - rect.left) * (rect.bottom - rect.top) > 16777216:
                raise ValueError("window pixel budget exceeded")
            name = ctypes.create_unicode_buffer(256)
            if not self.user.GetClassNameW(window_id, name, len(name)):
                raise OSError("window class unavailable")
            return WindowIdentity(pid, window_id, created, (rect.left, rect.top, rect.right, rect.bottom),
                                  self.user.GetDpiForWindow(window_id), thread, name.value, sid, session)
        finally:
            self.user.SetThreadDpiAwarenessContext(old)

    def input_enabled(self, window_id: int) -> bool:
        """Read input eligibility after the caller verifies window identity."""
        return bool(self.user.IsWindowEnabled(window_id))

    def validate_process(self, pid):
        """Prove the newly spawned driver kept this user's ordinary token."""
        sid, session, level = self.validate()
        process = self.kernel.OpenProcess(0x1000, False, pid)
        if not process:
            raise OSError("owned driver token unavailable")
        try:
            actual_sid, actual_level, _ = self._token(process)
            if actual_sid != sid or self._session(pid) != session or actual_level > level:
                raise OSError("owned driver token does not match desktop owner")
        finally:
            self.kernel.CloseHandle(process)

    def activity(self):
        class LastInput(ctypes.Structure):
            _fields_ = [("size", W.UINT), ("tick", W.DWORD)]
        info = LastInput(ctypes.sizeof(LastInput), 0)
        if not self.user.GetLastInputInfo(ctypes.byref(info)):
            raise OSError("input activity unavailable")
        pressed = any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18, 91, 92))
        return (self.user.GetForegroundWindow(), info.tick, pressed)
