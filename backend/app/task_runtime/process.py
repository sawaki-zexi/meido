from __future__ import annotations

import codecs
import os
import signal
import subprocess
import threading
from pathlib import Path

from ..browser_use.process import WindowsJob
from .runtime import TaskExecution, TaskOutcome


class ProcessExecutor:
    """A real argv process whose descendants stay owned after its parent exits."""

    def __init__(self, argv: list[str], workspace: Path, env: dict[str, str]):
        self.argv, self.workspace, self.env = argv, workspace, env
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._job: WindowsJob | None = None
        self._cancelled = False

    def run(self, execution: TaskExecution) -> TaskOutcome:
        with self._lock:
            if self._cancelled:
                return TaskOutcome("cancelled")
            self._process = subprocess.Popen(
                self.argv, cwd=self.workspace, env=self.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=(0x08000000 | 0x00000004) if os.name == "nt" else 0,
            )
            try:
                if os.name == "nt":
                    self._job = WindowsJob(self._process.pid, resume=True)
            except BaseException:
                self._process.kill()
                self._process.wait()
                raise
            process = self._process

        def pump(stream, prefix):
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            try:
                while chunk := stream.read1(4096):
                    execution.write(prefix + decoder.decode(chunk))
                tail = decoder.decode(b"", final=True)
                if tail:
                    execution.write(prefix + tail)
            finally:
                stream.close()

        readers = [threading.Thread(target=pump, args=(stream, prefix), daemon=True)
                   for stream, prefix in ((process.stdout, ""), (process.stderr, "stderr: "))]
        try:
            for reader in readers:
                reader.start()
            exit_code = process.wait()
        finally:
            # A successful parent may leave grandchildren holding stdout open.
            # End the entire owned tree before waiting for reader EOF.
            self._terminate()
            for reader in readers:
                reader.join()
        return TaskOutcome("succeeded" if exit_code == 0 else "failed", exit_code)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
        self._terminate()

    def _terminate(self) -> None:
        with self._lock:
            process, job = self._process, self._job
            self._job = None
            if process is None:
                return
            if job is not None:
                job.close()
            elif os.name != "nt":
                try:
                    killpg = getattr(os, "killpg")
                    killpg(process.pid, getattr(signal, "SIGKILL"))
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
