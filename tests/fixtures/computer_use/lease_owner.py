"""Owns only a new idle driver, never enumerates/captures/injects any window."""
import asyncio
import json
from pathlib import Path
import sys

from backend.app.computer_use.driver import PinnedDriverSession
from backend.app.computer_use.lease import DesktopLease


async def main():
    lease = DesktopLease()
    driver = PinnedDriverSession(Path(sys.argv[1]), Path(sys.argv[2]))
    try:
        await driver.connect()
        print(json.dumps({"ready": True, "driver_pid": driver.process_id}), flush=True)
        await asyncio.Event().wait()
    finally:
        await driver.close()
        lease.close()


asyncio.run(main())
