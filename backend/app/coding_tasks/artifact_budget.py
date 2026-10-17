"""Fixed Coding artifact paths and a combined retained file/byte ceiling."""
import os
from pathlib import Path
import stat
import time

from .errors import CodingDeadline, CodingFailure

FILE_LIMIT = 10000
BYTE_LIMIT = 2 * 1024 * 1024 * 1024


def identity(path):
    path = Path(path)
    info = path.lstat()
    if (path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
            or not stat.S_ISDIR(info.st_mode)
            or os.path.normcase(os.path.abspath(path)) != os.path.normcase(str(path.resolve(strict=True)))):
        raise CodingFailure('publication_artifact_path_redirected')
    return info.st_dev, info.st_ino


def check(publication_root, expected_identity, task_root, deadline):
    if identity(publication_root) != expected_identity:
        raise CodingFailure('publication_artifact_root_changed')
    total = count = 0
    # Publication artifacts have a global ceiling. Counting them together
    # with this task is stricter than giving each task a separate 2 GiB pool.
    for root in (Path(publication_root), Path(task_root)):
        if not root.exists():
            continue
        identity(root)
        for path in root.rglob('*'):
            if time.monotonic() >= deadline:
                raise CodingDeadline()
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise CodingFailure('publication_artifact_path_redirected')
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise CodingFailure('publication_artifact_file_redirected')
                total += info.st_size
                count += 1
                if total > BYTE_LIMIT or count > FILE_LIMIT:
                    raise CodingFailure('publication_artifact_budget_exceeded')
            elif not stat.S_ISDIR(info.st_mode):
                raise CodingFailure('publication_artifact_file_unsupported')
