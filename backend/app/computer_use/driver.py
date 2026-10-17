import hashlib
import asyncio
import json
import os
from pathlib import Path
import sys

from ..mcp_config import McpServer, minimal_environment
from ..mcp_stdio import McpStdioClient
from .lease import JOB_NAME

PROFILE = "meido-windows-169-v1"
UPSTREAM_COMMIT = "fc188250b4ca8549b8e61f937fdb1fb560770e86"


def resolve_driver(resources: Path) -> Path:
    if sys.platform != "win32" or os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() not in {"amd64", "x86_64"}:
        raise OSError("Computer Use requires Windows x64")
    root = resources / "native" / "computer-use"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
    expected = json.loads(Path(__file__).with_name("runtime.json").read_text(encoding="utf-8"))
    executable = root / "cua-driver.exe"
    if (manifest != expected or manifest.get("profile") != PROFILE or manifest.get("upstreamCommit") != UPSTREAM_COMMIT
            or manifest.get("uiAccess") is not False or manifest.get("executionLevel") != "asInvoker"
            or len(manifest.get("sha256", "")) != 64
            or hashlib.sha256(executable.read_bytes()).hexdigest() != manifest["sha256"]):
        raise OSError("fixed Meido driver identity mismatch")
    return executable


class PinnedDriverSession:
    def __init__(self, resources, directory):
        executable = resolve_driver(resources)
        directory.mkdir(parents=True, exist_ok=True)
        environment = minimal_environment()
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
            environment[key] = str(directory)
        environment.update(CUA_DRIVER_RS_TELEMETRY_ENABLED="0", CUA_DRIVER_RS_UPDATE_CHECK="0",
                           CUA_DRIVER_TELEMETRY_HOME=str(directory / "telemetry"))
        config = McpServer("computer", "Fixed Meido Computer Use", (str(executable), "mcp", "--direct"),
            str(directory), (), (), True, "external", 30, 65536, PROFILE)
        self._client = McpStdioClient(config, environment, job_name=JOB_NAME)

    async def connect(self):
        async with asyncio.timeout(10):
            await self._client.connect()
        from .windows import WindowsDesktop
        if self._client.process_id is None:
            raise OSError("owned driver process identity unavailable")
        WindowsDesktop().validate_process(self._client.process_id)

    async def call(self, name, arguments):
        return await self._client.call(name, arguments)

    @property
    def exit_confirmed(self):
        return self._client.exit_confirmed

    @property
    def process_id(self):
        return self._client.process_id

    async def close(self):
        await self._client.close()
