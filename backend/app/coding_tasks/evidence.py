"""Host-only Git evidence and the original attempt's non-renewable time budget."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from .errors import CodingDeadline, CodingFailure
from .git_repository import git, reject_git_filters

_PROCESS_ID = str(os.getpid()) + ':' + uuid.uuid4().hex
_BUDGETS: dict[str, dict] = {}
_BUDGET_LOCK = threading.Lock()
_BUDGET_FIELDS = ('originalDeadlineAt', 'businessDeadlineAt', 'originalMonotonicDeadline',
                  'businessMonotonicDeadline', 'attestationProcessId')


def task_paths(root, record):
    task = Path(root) / 't' / hashlib.sha256(record['origin']['role_id'].encode()).hexdigest()[:16] / record['id'][7:]
    return task, task / 'w', task / 'a' / record['attemptId'][8:]


def begin_attempt_budget(record, deadline):
    """Persist these fields before launch; a persisted old attempt cannot mint time."""
    attempt = record['attemptId']
    with _BUDGET_LOCK:
        known = _BUDGETS.get(attempt)
        if known is not None:
            if any(record.get(key, value) != value for key, value in known.items()):
                raise CodingFailure('attempt_budget_identity_changed')
            return dict(known)
        if any(key in record for key in _BUDGET_FIELDS):
            raise CodingFailure('original_attempt_budget_unprovable')
        now, wall = time.monotonic(), time.time()
        original = min(deadline, now + 600)
        if original <= now:
            raise CodingDeadline()
        known = {'originalDeadlineAt': wall + original - now,
                 'businessDeadlineAt': wall + original - 60 - now,
                 'originalMonotonicDeadline': original, 'businessMonotonicDeadline': original - 60,
                 'attestationProcessId': _PROCESS_ID}
        _BUDGETS[attempt] = known
        return dict(known)


def remaining_attempt_budget(record, *, business=True):
    """Only this process's original monotonic window proves remaining authority."""
    with _BUDGET_LOCK:
        known = _BUDGETS.get(record.get('attemptId'))
        if known is None or any(record.get(key) != known[key] for key in _BUDGET_FIELDS):
            raise CodingFailure('original_attempt_budget_unprovable')
        prefix = 'business' if business else 'original'
        remaining = min(known[prefix + 'MonotonicDeadline'] - time.monotonic(),
                        known[prefix + 'DeadlineAt'] - time.time())
    if remaining <= 0:
        raise CodingDeadline()
    return remaining


def _git(root, deadline, *arguments):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CodingDeadline()
    return git(root, *arguments, timeout=min(10, remaining))


def _identity(path):
    info = path.stat()
    return {'path': str(path), 'device': info.st_dev, 'inode': info.st_ino}


def _redirected(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def _tree(entries, algorithm):
    nodes: dict = {}
    for name, item in entries.items():
        if item.get('status') == 'deleted':
            continue
        current = nodes
        parts = name.split('/')
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = (item['mode'], item['gitBlob'])

    def digest(node):
        values = []
        for name, value in node.items():
            directory = isinstance(value, dict)
            mode, identifier = ('40000', digest(value)) if directory else value
            values.append((name.encode('utf-8') + (b'/' if directory else b''),
                           mode.encode() + b' ' + name.encode('utf-8') + b'\0' + bytes.fromhex(identifier)))
        data = b''.join(value for _, value in sorted(values))
        return hashlib.new(algorithm, b'tree ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    return digest(nodes)


def snapshot_worktree(worktree, common_dir, branch, deadline):
    original = Path(worktree)
    root = original.resolve(strict=True)
    reject_git_filters(root, timeout=min(10, max(.001, deadline - time.monotonic())))
    if _redirected(original) or os.path.normcase(os.path.abspath(original)) != os.path.normcase(str(root)):
        raise CodingFailure('worktree_identity_changed')
    actual_common = Path(_git(root, deadline, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve(strict=True)
    actual_git = Path(_git(root, deadline, 'rev-parse', '--absolute-git-dir')).resolve(strict=True)
    if actual_common != Path(common_dir).resolve(strict=True) or _git(root, deadline, 'branch', '--show-current') != branch:
        raise CodingFailure('worktree_identity_changed')
    identity = {'worktree': _identity(root), 'commonDir': _identity(actual_common), 'gitDir': _identity(actual_git)}
    head = _git(root, deadline, 'rev-parse', '--verify', 'HEAD^{commit}')
    head_tree = _git(root, deadline, 'rev-parse', 'HEAD^{tree}')
    algorithm = _git(root, deadline, 'rev-parse', '--show-object-format')
    if algorithm not in {'sha1', 'sha256'}:
        raise CodingFailure('unsupported_git_object_format')
    entries = _git(root, deadline, 'ls-files', '--stage', '-z')
    modes = {}
    for entry in entries.split('\0'):
        if not entry:
            continue
        description, name = entry.split('\t', 1)
        mode, _, stage = description.split(' ')
        if stage != '0' or mode not in {'100644', '100755'}:
            raise CodingFailure('worktree_index_unmerged_or_unsupported')
        modes[name] = mode
    filemode = _git(root, deadline, 'config', '--type=bool', '--default=false', '--get', 'core.filemode') == 'true'
    names = set(_git(root, deadline, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0')) - {''}
    if len(names) > 10000:
        raise CodingFailure('worktree_file_count_budget')
    files, total = {}, 0
    for name in sorted(names):
        if time.monotonic() >= deadline:
            raise CodingDeadline()
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise CodingFailure('worktree_file_redirected')
        for part in (path, *path.parents):
            if part == root:
                break
            if part.is_symlink() or (part.exists() and _redirected(part)):
                raise CodingFailure('worktree_file_redirected')
        if not path.exists():
            files[name] = {'status': 'deleted'}
            continue
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise CodingFailure('worktree_file_redirected_or_unsupported')
        total += before.st_size
        if total > 2 * 1024 * 1024 * 1024:
            raise CodingFailure('task_file_budget_exceeded')
        digest, blob = hashlib.sha256(), hashlib.new(algorithm)
        blob.update(b'blob ' + str(before.st_size).encode() + b'\0')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
                raise CodingFailure('worktree_file_changed_during_snapshot')
            while chunk := stream.read(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise CodingDeadline()
                digest.update(chunk)
                blob.update(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode)
        if signature(before) != signature(after) or signature(before) != signature(current):
            raise CodingFailure('worktree_file_changed_during_snapshot')
        mode = ('100755' if before.st_mode & 0o111 else '100644') if filemode else modes.get(name, '100644')
        files[name] = {'sha256': digest.hexdigest(), 'gitBlob': blob.hexdigest(), 'mode': mode,
                       'size': before.st_size, 'statMode': before.st_mode, 'device': before.st_dev,
                       'inode': before.st_ino, 'links': before.st_nlink}
    status = _git(root, deadline, 'status', '--porcelain=v1', '--untracked-files=all')
    raw_diff = _git(root, deadline, '-c', 'core.quotePath=true', 'diff', '--binary', '--full-index', '--no-ext-diff', '--no-textconv', 'HEAD', '--')
    index = Path(_git(root, deadline, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    if _redirected(index) or index.resolve().parent != actual_git:
        raise CodingFailure('worktree_index_redirected')
    if index.stat().st_size > 16 * 1024 * 1024:
        raise CodingFailure('worktree_index_over_budget')
    index_bytes = index.read_bytes()
    flags = _git(root, deadline, 'ls-files', '-v', '-z')
    if (entries != _git(root, deadline, 'ls-files', '--stage', '-z') or
            index_bytes != index.read_bytes() or head != _git(root, deadline, 'rev-parse', 'HEAD')):
        raise CodingFailure('worktree_index_or_head_changed_during_snapshot')
    return {'head': head, 'headTree': head_tree, 'tree': _tree(files, algorithm), 'status': status,
            'identity': identity, 'files': files, 'index': {'entries': entries, 'flags': flags,
                'sha256': hashlib.sha256(index_bytes).hexdigest(), 'path': str(index)},
            'diff': {'sha256': hashlib.sha256(raw_diff.encode()).hexdigest(), 'bytes': len(raw_diff.encode())}}


def attest_snapshot(worktree, record, deadline, validation):
    remaining_attempt_budget(record, business=False)
    approved = record['repository'].get('validation', [])
    if (record.get('cleanupState') != 'complete' or not record.get('nativeSession') or not approved or
            len(validation) != len(approved)):
        raise CodingFailure('host_attestation_incomplete')
    checks = []
    for command, result in zip(approved, validation, strict=True):
        if result.get('id') != command['id'] or result.get('status') != 'passed' or result.get('exitCode') != 0:
            raise CodingFailure('host_attestation_validation_incomplete')
        checks.append({'id': command['id'], 'argv': list(command['argv']), 'cwd': command.get('cwd', '.'),
                       'status': 'passed', 'exitCode': 0})
    snapshot = snapshot_worktree(worktree, record['repository']['commonDir'], record['branch'], deadline)
    if snapshot['head'] != record['startHead'] or not snapshot['status']:
        raise CodingFailure('host_attestation_head_or_diff_changed')
    return {'schema': 1, 'taskId': record['id'], 'attemptId': record['attemptId'],
            'nativeSession': record['nativeSession'], 'baseHead': record['baseHead'], 'startHead': record['startHead'],
            'branch': record['branch'], 'worktree': str(Path(worktree).resolve()),
            'commonDir': str(Path(record['repository']['commonDir']).resolve()), 'snapshot': snapshot,
            'validation': checks, 'cleanupState': 'complete', 'attestedAt': time.time(),
            **{key: record[key] for key in _BUDGET_FIELDS}}


def verify_attestation(worktree, record, deadline):
    evidence = record.get('validationEvidence')
    if not isinstance(evidence, dict) or evidence.get('schema') != 1:
        raise CodingFailure('host_attestation_unavailable')
    expected = {'taskId': record['id'], 'attemptId': record['attemptId'], 'nativeSession': record.get('nativeSession'),
                'baseHead': record.get('baseHead'), 'startHead': record.get('startHead'), 'branch': record['branch'],
                'worktree': str(Path(worktree).resolve()), 'commonDir': str(Path(record['repository']['commonDir']).resolve()),
                'cleanupState': 'complete', **{key: record.get(key) for key in _BUDGET_FIELDS}}
    if record.get('cleanupState') != 'complete' or any(evidence.get(key) != value for key, value in expected.items()):
        raise CodingFailure('host_attestation_identity_changed')
    approved = [{'id': value['id'], 'argv': list(value['argv']), 'cwd': value.get('cwd', '.'),
                 'status': 'passed', 'exitCode': 0} for value in record['repository'].get('validation', [])]
    if not approved or evidence.get('validation') != approved:
        raise CodingFailure('host_attestation_validation_changed')
    current = snapshot_worktree(worktree, record['repository']['commonDir'], record['branch'], deadline)
    if current != evidence.get('snapshot'):
        raise CodingFailure('host_attestation_snapshot_changed')
    # Return a plain JSON copy so callers cannot mutate the retained evidence.
    return json.loads(json.dumps(evidence, ensure_ascii=False))
