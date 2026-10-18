"""Select CI checks from NUL-delimited git diff paths; unknown paths run both."""

import os
import sys


frontend = backend = False
frontend_files = {
    "package.json", "package-lock.json", "tsconfig.json",
    "vite.config.ts", "vitest.config.ts",
}
documentation_files = {"README.md", "CONTEXT.md", "AGENTS.md", "LICENSE"}

for raw_path in sys.stdin.buffer.read().split(b"\0"):
    if not raw_path:
        continue
    path = os.fsdecode(raw_path)
    if path.startswith("docs/") or path in documentation_files:
        continue
    if path.startswith(("web/", "desktop-pet/")) or path in frontend_files:
        frontend = True
    elif path.startswith(("backend/", "tests/python/")) or path == "requirements.txt":
        backend = True
    else:
        frontend = backend = True

print(f"frontend={str(frontend).lower()}")
print(f"backend={str(backend).lower()}")
