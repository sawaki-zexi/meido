from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def git_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP') if key in os.environ}
    environment.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                        'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '', 'SSH_ASKPASS': '',
                        'GIT_OPTIONAL_LOCKS': '0', 'GIT_CONFIG_COUNT': '4',
                        'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': os.devnull,
                        'GIT_CONFIG_KEY_1': 'credential.helper', 'GIT_CONFIG_VALUE_1': '',
                        'GIT_CONFIG_KEY_2': 'core.fsmonitor', 'GIT_CONFIG_VALUE_2': 'false',
                        'GIT_CONFIG_KEY_3': 'commit.gpgsign', 'GIT_CONFIG_VALUE_3': 'false'})
    return environment


def git(root: Path, *arguments: str, timeout: float = 10) -> str:
    executable = shutil.which('git')
    if executable is None:
        raise ValueError('Git executable is unavailable')
    try:
        result = subprocess.run([executable, '-C', str(root), *arguments], env=git_environment(),
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                creationflags=0x08000000 if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError('Git repository inspection failed') from error
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ValueError('Git repository inspection failed')
    return result.stdout.decode('utf-8', errors='strict').strip()


def repository_identity(value: str) -> dict[str, str]:
    original = Path(value)
    if not original.is_absolute() or not original.is_dir():
        raise ValueError('Git repository must be an absolute existing root')
    root = original.resolve(strict=True)
    # Owner approval is tied to this actual path, never a movable alias.
    if os.path.normcase(os.path.abspath(original)) != os.path.normcase(str(root)):
        raise ValueError('Git repository aliases and reparse paths require their real root')
    top = Path(git(root, 'rev-parse', '--show-toplevel')).resolve(strict=True)
    if top != root:
        raise ValueError('Git repository must be its exact top-level root')
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve(strict=True)
    head = git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
    return {'root': str(root), 'commonDir': str(common), 'head': head}
