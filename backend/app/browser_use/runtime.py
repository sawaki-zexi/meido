from dataclasses import dataclass
from pathlib import Path
import sys


class BrowserRuntimeUnavailable(RuntimeError):
    """The pinned native browser runtime cannot be used on this host."""


@dataclass(frozen=True, slots=True)
class BrowserRuntime:
    agent_browser: Path
    chrome: Path

    @classmethod
    def resolve(cls, resources: Path) -> "BrowserRuntime":
        if sys.platform != "win32":
            raise BrowserRuntimeUnavailable("Browser Use 首版仅支持 Windows x64")
        root = resources / "native" / "browser-use"
        runtime = cls(root / "agent-browser.exe", root / "chrome-win64" / "chrome.exe")
        for path in (runtime.agent_browser, runtime.chrome):
            if not path.is_file():
                raise BrowserRuntimeUnavailable(f"Browser Use 运行组件缺失：{path}")
        return runtime
