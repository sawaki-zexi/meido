"""Only creates/reads/terminates this fixture's own Windows process."""
import json
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4


class FixtureWindow:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True)
        self.title = "Meido169 " + uuid4().hex
        executable = self.directory / "Fixture.exe"
        compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        subprocess.run([str(compiler), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll",
                        "/reference:System.Drawing.dll", "/reference:System.Web.Extensions.dll", "/out:" + str(executable),
                        "/win32manifest:" + str(Path(__file__).with_name("Fixture.manifest")),
                        str(Path(__file__).with_name("Fixture.cs"))], check=True, capture_output=True)
        self.process = subprocess.Popen([str(executable), str(self.directory), self.title], creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.state()
        except BaseException:
            self.close()
            raise

    def state(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                value = json.loads((self.directory / "state.json").read_text(encoding="utf-8-sig"))
                assert value["pid"] == self.process.pid
                assert value["a"]["title"] == self.title + " A"
                return value
            except (OSError, ValueError):
                time.sleep(.03)
        raise TimeoutError("own fixture did not publish identity")

    def close(self):
        block = self.directory / "block-click"
        try:
            block.unlink(missing_ok=True)
            (self.directory / "stop").touch()
        finally:
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)
            assert self.process.poll() is not None

    def maximize(self):
        (self.directory/"maximize-a").touch()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=self.state()
            if state["a"]["maximized"]:
                return state
            time.sleep(.03)
        raise TimeoutError("own fixture did not maximize")

    def native_flags(self):
        state = self.state()
        hwnd = state["a"]["hwnd"]
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.IsWindowEnabled.argtypes = [wintypes.HWND]
        user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        return bool(user.IsWindowEnabled(hwnd)), user.GetWindowLongPtrW(hwnd, -20)

    def prepare_assistance_candidate(self):
        """One owned setup; success proves only disabled A and foreground B."""
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetForegroundWindow.restype = wintypes.HWND
        (self.directory / "assist-candidate").touch()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.state()
            if not state["a"]["enabled"] and user.GetForegroundWindow() == state["b"]["hwnd"]:
                return state
            time.sleep(.03)
        raise TimeoutError("owned assistance candidate prerequisites were not observed")
