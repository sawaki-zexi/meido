"""Private native ownership for CodingTask attempts.

The Job contains the approved launch chain. It does not sandbox arbitrary
same-account programs or prevent them from using unrelated OS services.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


class CleanupFailed(RuntimeError):
    pass


class OutputBudgetExceeded(RuntimeError):
    pass


class _BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                ('max_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', _BasicLimits), ('io', ctypes.c_uint64 * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('thread_id', wintypes.DWORD),
                ('process_id', wintypes.DWORD), ('priority', wintypes.LONG),
                ('delta_priority', wintypes.LONG), ('flags', wintypes.DWORD)]


class _OwnedJob:
    def __init__(self, pid: int, job_name: str | None = None):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel = kernel
        for name, arguments, result in (
            ('CreateJobObjectW', [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ('SetInformationJobObject', [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ('QueryInformationJobObject', [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ('AssignProcessToJobObject', [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ('TerminateJobObject', [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ('OpenProcess', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL),
            ('CreateToolhelp32Snapshot', [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            ('Thread32First', [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
            ('Thread32Next', [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
            ('OpenThread', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ('ResumeThread', [wintypes.HANDLE], wintypes.DWORD),
        ):
            function = getattr(kernel, name)
            function.argtypes, function.restype = arguments, result
        ctypes.set_last_error(0)
        self.handle = kernel.CreateJobObjectW(None, job_name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if job_name is not None and ctypes.get_last_error() == 183:
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise CleanupFailed('owned Coding Job name already exists')
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # kill-on-close, no breakaway flags
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel.AssignProcessToJobObject(self.handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel.CloseHandle(process)
            self._resume(pid)
        except BaseException:
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise

    def _resume(self, pid):
        kernel = self.kernel
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            present = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if entry.process_id == pid:
                    thread = kernel.OpenThread(2, False, entry.thread_id)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        kernel.CloseHandle(thread)
                present = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise CleanupFailed('owned suspended process has no initial thread')
        finally:
            kernel.CloseHandle(snapshot)

    def close(self, timeout=5):
        if not self.handle:
            return
        kernel = self.kernel
        if not kernel.TerminateJobObject(self.handle, 1):
            raise CleanupFailed('owned Job termination failed')
        deadline = time.monotonic() + timeout
        counters = (wintypes.DWORD * 12)()
        while True:
            if not kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(counters), ctypes.sizeof(counters), None):
                raise CleanupFailed('owned Job exit could not be observed')
            if counters[10] == 0:
                if not kernel.CloseHandle(self.handle):
                    raise CleanupFailed('owned Job handle release failed')
                self.handle = None
                return
            if time.monotonic() >= deadline:
                # Keep the live handle: the owning attempt must retain and
                # retry cleanup instead of releasing admission optimistically.
                raise CleanupFailed('owned Job still has live processes')
            time.sleep(.01)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class OwnedProcess:
    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str], *, output_bytes=1024 * 1024, job_name=None, journal=None, on_stdout=None):
        self._lock = threading.RLock()
        self.cleanup_state = 'running'
        self._job: _OwnedJob | None = None
        self._limit = output_bytes
        self._output = [bytearray(), bytearray()]
        self._overflow = threading.Event()
        self._read_errors: list[BaseException] = []
        self._write_errors: list[BaseException] = []
        self._writer: threading.Thread | None = None
        self._stop_requested = False
        self._on_stdout = on_stdout
        self.job_name, self._journal = job_name, journal
        if job_name is not None and journal is not None:
            journal(job_name, True)
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=0x08000000 | 0x00000004 if os.name == 'nt' else 0,
                                        start_new_session=os.name != 'nt')
        try:
            if os.name == 'nt':
                self._job = _OwnedJob(self.process.pid, job_name)
        except BaseException:
            self.process.kill()
            self.process.wait()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
            raise
        self._readers = [threading.Thread(target=self._pump, args=(index, stream), daemon=True)
                         for index, stream in enumerate((self.process.stdout, self.process.stderr))]
        for reader in self._readers:
            reader.start()

    @property
    def pid(self):
        return self.process.pid

    def _pump(self, index, stream):
        try:
            while chunk := stream.read1(4096):
                with self._lock:
                    remaining = self._limit - len(self._output[0]) - len(self._output[1])
                    self._output[index].extend(chunk[:max(0, remaining)])
                    if len(chunk) > remaining:
                        self._overflow.set()
                if self._overflow.is_set():
                    # The supervisor polls the overflow flag and owns cleanup.
                    return
                if index == 0 and self._on_stdout is not None:
                    self._on_stdout(chunk)
        except BaseException as error:
            self._read_errors.append(error)
        finally:
            stream.close()

    def _write(self, value):
        try:
            assert self.process.stdin is not None
            self.process.stdin.write(value)
            self.process.stdin.flush()
        except BaseException as error:
            self._write_errors.append(error)
        finally:
            if self.process.stdin is not None:
                self.process.stdin.close()

    def request_stop(self, *, grace_seconds=5) -> bool:
        """Try only this owned child; unsupported console interruption escalates."""
        with self._lock:
            if self._stop_requested or self.process.poll() is not None:
                return False
            self._stop_requested = True
            try:
                self.process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
            except (OSError, ValueError):
                return False
        try:
            self.process.wait(timeout=max(0, min(5, grace_seconds)))
        except subprocess.TimeoutExpired:
            pass
        return True

    def communicate(self, *, timeout: float, input: bytes | None = None, output_bytes: int | None = None, check=None,
                    graceful_stop=False) -> ProcessResult:
        deadline = time.monotonic() + timeout
        try:
            assert self.process.stdin is not None
            with self._lock:
                if input and self.cleanup_state == 'running':
                    self._writer = threading.Thread(target=self._write, args=(input,), daemon=False,
                                                    name='meido-coding-stdin')
                    self._writer.start()
                elif not self.process.stdin.closed:
                    self.process.stdin.close()
            while self.process.poll() is None:
                if check is not None:
                    check()
                if self._read_errors:
                    raise self._read_errors[0]
                if self._overflow.is_set():
                    raise OutputBudgetExceeded('native output exceeded the attempt budget')
                if time.monotonic() >= deadline:
                    raise TimeoutError('native command deadline exceeded')
                time.sleep(.01)
        except BaseException:
            if graceful_stop:
                self.request_stop(grace_seconds=deadline - time.monotonic())
            raise
        finally:
            self.close()
        if self._read_errors:
            raise self._read_errors[0]
        if self._write_errors:
            raise RuntimeError('native stdin could not be delivered') from self._write_errors[0]
        limit = output_bytes or self._limit
        if self._overflow.is_set() or sum(map(len, self._output)) > limit:
            raise OutputBudgetExceeded('native output exceeded the command budget')
        return ProcessResult(self.process.returncode, bytes(self._output[0]), bytes(self._output[1]))

    def output(self) -> tuple[bytes, bytes]:
        with self._lock:
            return bytes(self._output[0]), bytes(self._output[1])

    def close(self):
        with self._lock:
            if self.cleanup_state == 'complete':
                return
            self.cleanup_state = 'stopping'
            try:
                if self._job is not None:
                    self._job.close()
                    self._job = None
                elif os.name != 'nt':
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=2)
            except BaseException:
                self.cleanup_state = 'cleanup_failed'
                raise
        # Readers acquire the output lock; never join them while holding it.
        for reader in self._readers:
            if reader is not threading.current_thread():
                reader.join(timeout=1)
                if reader.is_alive():
                    self.cleanup_state = 'cleanup_failed'
                    raise CleanupFailed('owned output reader did not exit')
        writer = self._writer
        if writer is not None and writer is not threading.current_thread():
            writer.join(timeout=2)
            if writer.is_alive():
                self.cleanup_state = 'cleanup_failed'
                raise CleanupFailed('owned stdin writer did not exit')
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        if self.job_name is not None and self._journal is not None:
            self._journal(self.job_name, False)
        self.cleanup_state = 'complete'


def named_job_active(name: str) -> bool:
    """Read an attempt's persisted private Job identity without adopting it."""
    if os.name != 'nt':
        return False
    if not name.startswith('Local\\Meido.Coding.') or len(name) > 200 or '\x00' in name:
        raise ValueError('invalid owned Coding Job journal')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenJobObjectW(4, False, name)
    if not handle:
        if ctypes.get_last_error() == 2:
            return False
        raise CleanupFailed('persisted Coding Job cannot be queried')
    try:
        counters = (wintypes.DWORD * 12)()
        if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(counters), ctypes.sizeof(counters), None):
            raise CleanupFailed('persisted Coding Job process count unavailable')
        return counters[10] != 0
    finally:
        if not kernel.CloseHandle(handle):
            raise CleanupFailed('persisted Coding Job query handle not released')
