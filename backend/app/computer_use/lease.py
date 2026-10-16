"""The dedicated owner thread is the only thread allowed to release its mutex."""
import ctypes
from ctypes import wintypes as W
import queue
import threading

from ..browser_use.process import WindowsJob

JOB_NAME = "Local\\Meido.ComputerUse.InputJob"
MUTEX_NAME = "Local\\Meido.ComputerUse.Desktop"


class LeaseError(OSError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DesktopLease:
    def __init__(self):
        self._ready, self._release, self._done = queue.Queue(), threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._own, name="meido-computer-lease", daemon=True)
        self._thread.start()
        try:
            result = self._ready.get(timeout=5)
            if isinstance(result, BaseException):
                raise result
        except BaseException:
            # The OS check can complete after readiness times out. Do not
            # forget an owner that may then acquire the mutex and wait forever.
            self._release.set()
            self._thread.join()
            raise

    def _own(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, W.BOOL, W.LPCWSTR]
        kernel.CreateMutexW.restype = W.HANDLE
        kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        kernel.ReleaseMutex.argtypes = [W.HANDLE]
        kernel.CloseHandle.argtypes = [W.HANDLE]
        handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
        acquired = False
        try:
            if not handle:
                raise OSError("desktop mutex unavailable")
            status = kernel.WaitForSingleObject(handle, 0)
            if status not in (0, 0x80):
                raise LeaseError("busy")
            acquired = True
            if WindowsJob.named_job_active(JOB_NAME):
                raise LeaseError("cleanup_unconfirmed")
            self._ready.put(True)
            self._release.wait()
        except BaseException as error:
            self._ready.put(error)
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            if handle:
                kernel.CloseHandle(handle)
            self._done.set()

    def close(self):
        self._release.set()
        if not self._done.wait(5):
            raise OSError("desktop mutex release unconfirmed")
        self._thread.join()
