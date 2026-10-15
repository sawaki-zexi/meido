"""Exercise edit expansion through execute in an OS memory-limited process."""
import subprocess
import sys
import textwrap
from pathlib import Path


def test_edit_rejects_multiplicative_utf8_expansion_before_allocating_result(tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"a" * 8192)
    probe = textwrap.dedent(r'''
        import asyncio
        import os
        import sys
        from pathlib import Path
        from backend.app.agent_runtime.file_tools import EditFileTool
        from backend.app.agent_runtime.provider import CancellationToken
        from backend.app.agent_runtime.tools import ToolContext

        # A valid 8 KiB source and 8 Ki-character replacement would expand to
        # 192 MiB UTF-8 (plus the intermediate Unicode string) without a guard.
        # Enforce a real process limit so this regression cannot exhaust the
        # test runner's memory. The Windows job handle remains alive to exit.
        memory_limit = 256 * 1024 * 1024
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD),
                ]
            class Counters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in (
                    "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
            class ExtendedLimits(ctypes.Structure):
                _fields_ = [
                    ("basic", BasicLimits), ("io", Counters),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
                ]
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel.SetInformationJobObject.restype = wintypes.BOOL
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            job = kernel.CreateJobObjectW(None, None)
            assert job, ctypes.WinError(ctypes.get_last_error())
            limits = ExtendedLimits()
            limits.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
            limits.process_memory = memory_limit
            assert kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)), ctypes.WinError(ctypes.get_last_error())
            assert kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()), ctypes.WinError(ctypes.get_last_error())
        else:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))

        workspace = Path(sys.argv[1])
        result = asyncio.run(EditFileTool("role-a", workspace).execute(
            {"path": "notes.txt", "old_text": "a", "new_text": "\u4f60" * 8192, "replace_all": True},
            ToolContext("role-a", "role:role-a", "run-memory-budget", CancellationToken()),
        ))
        assert result.is_error and result.details["status"] == "denied", result
        assert result.details["truncatedBy"] == "file_limit", result
        assert (workspace / "notes.txt").read_bytes() == b"a" * 8192
        assert not list(workspace.glob(".meido-*.tmp"))
        print("bounded-edit-refused")
    ''')
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "bounded-edit-refused" in completed.stdout
