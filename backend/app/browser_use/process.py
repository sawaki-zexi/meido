from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import time
from collections.abc import Mapping
from ctypes import wintypes
from typing import BinaryIO


class _BasicJobLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedJobLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicJobLimits),
        ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class WindowsJob:
    """Owns a suspended Windows process tree and kills it when the host exits."""

    def __init__(self, pid: int, *, resume: bool = False) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Browser Use process ownership requires Windows")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel = kernel
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process_handle = None
        try:
            limits = _ExtendedJobLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process_handle or not kernel.AssignProcessToJobObject(
                self._handle, process_handle
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if resume:
                self._resume_initial_thread(pid)
        except BaseException:
            self._close_handle()
            raise
        finally:
            if process_handle:
                kernel.CloseHandle(process_handle)

    def close(self) -> None:
        """Terminates the owned tree and waits until no profile users remain."""
        if not self._handle:
            return
        kernel = self._kernel
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ]
        try:
            if not kernel.TerminateJobObject(self._handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            counters = (wintypes.DWORD * 12)()
            deadline = time.monotonic() + 5
            while True:
                if not kernel.QueryInformationJobObject(
                    self._handle, 1, ctypes.byref(counters), ctypes.sizeof(counters), None
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if counters[10] == 0:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Browser Use process tree did not exit")
                time.sleep(0.01)
        finally:
            self._close_handle()

    def _close_handle(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None

    def _resume_initial_thread(self, pid: int) -> None:
        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("usage", wintypes.DWORD),
                ("thread_id", wintypes.DWORD),
                ("process_id", wintypes.DWORD),
                ("priority", wintypes.LONG),
                ("delta_priority", wintypes.LONG),
                ("flags", wintypes.DWORD),
            ]

        kernel = self._kernel
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
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
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise RuntimeError(f"Suspended process {pid} has no initial thread")
        finally:
            kernel.CloseHandle(snapshot)


async def spawn_process_tree(
    *command: str,
    cwd: str,
    env: Mapping[str, str],
    stdin: int | None = asyncio.subprocess.DEVNULL,
    stdout: int | BinaryIO = asyncio.subprocess.DEVNULL,
    stderr: int | BinaryIO = asyncio.subprocess.DEVNULL,
) -> tuple[asyncio.subprocess.Process, WindowsJob | None]:
    owned = sys.platform == "win32"
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=dict(env),
        # agent-browser image responses are JSON lines whose Base64 payload
        # can exceed the asyncio default 64 KiB StreamReader limit.
        limit=20 * 1024 * 1024,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        creationflags=(0x08000000 | 0x00000004) if owned else 0,
    )
    if not owned:
        return process, None
    try:
        return process, WindowsJob(process.pid, resume=True)
    except BaseException:
        process.kill()
        await process.wait()
        raise


async def release_process_tree_owner(owner: WindowsJob | None) -> None:
    if owner is None:
        return
    closing = asyncio.create_task(asyncio.to_thread(owner.close))
    try:
        await asyncio.shield(closing)
    except asyncio.CancelledError:
        await closing
        raise


async def terminate_process_tree(process: asyncio.subprocess.Process, *, timeout: float = 5) -> None:
    if process.returncode is None and os.name == "nt":
        killer = None
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=timeout)
        except (FileNotFoundError, OSError, asyncio.TimeoutError, ProcessLookupError):
            if killer is not None and killer.returncode is None:
                killer.kill()
                await killer.wait()
    if process.returncode is None:
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
