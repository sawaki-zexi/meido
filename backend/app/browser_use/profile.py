from pathlib import Path
import sys
from typing import BinaryIO


class ProfileLease:
    def __init__(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        lock_file = (path / "meido.lock").open("a+b")
        self._file: BinaryIO | None = lock_file
        lock_file.seek(0, 2)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            lock_file.close()
            raise RuntimeError("Browser Use 角色配置目录已被另一会话占用") from error

    def close(self) -> None:
        lock_file, self._file = self._file, None
        if lock_file is not None:
            lock_file.close()
