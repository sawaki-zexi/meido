from __future__ import annotations

import os
import shutil
import subprocess
import re
from pathlib import Path
from urllib.parse import urlsplit
from .errors import CodingFailure


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
    reject_git_filters(root)
    # Owner approval is tied to this actual path, never a movable alias.
    if os.path.normcase(os.path.abspath(original)) != os.path.normcase(str(root)):
        raise ValueError('Git repository aliases and reparse paths require their real root')
    top = Path(git(root, 'rev-parse', '--show-toplevel')).resolve(strict=True)
    if top != root:
        raise ValueError('Git repository must be its exact top-level root')
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve(strict=True)
    head = git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
    return {'root': str(root), 'commonDir': str(common), 'head': head}


def reject_git_filters(root: Path, *, timeout: float = 10) -> None:
    # status/worktree checkout can launch clean/smudge/process filters even
    # with hooks, textconv and external diffs disabled. Those repositories
    # need a separately owned filter implementation before Coding can use them.
    keys = git(root, 'config', '--name-only', '--list', timeout=timeout).splitlines()
    if any(key.lower().startswith('filter.') for key in keys):
        raise CodingFailure('repository_git_filters_unavailable')


def publication_remote(root: Path, remote: str) -> dict[str, str]:
    """Freeze one explicit HTTPS destination; this never contacts the platform."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', remote):
        raise ValueError('publication requires one fixed remote name')
    urls = git(root, 'remote', 'get-url', '--push', '--all', remote).splitlines()
    if len(urls) != 1:
        raise ValueError('publication requires exactly one push URL')
    parsed = urlsplit(urls[0])
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('publication requires a credential-free GitHub HTTPS URL')
    match = re.fullmatch(r'/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?', parsed.path)
    if match is None:
        raise ValueError('publication requires an exact GitHub repository')
    owner, repository = match.groups()
    return {'url': f'https://github.com/{owner}/{repository}.git', 'owner': owner, 'repository': repository}
