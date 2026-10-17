"""Owned Codex exec transport; CodingApplication retains policy, ledger and ACKs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from ..task_runtime import TaskOutcome
from .codex_protocol import CodexProtocol
from .config import CODEX_EXEC_TEMPLATE, CODEX_EXEC_VERSION, executable_identity
from .errors import CodingFailure
from .evidence import attest_snapshot, begin_attempt_budget, task_paths
from .executor import (HerdrExecutor, NativeResult, check_task_files, reject_project_cli_extensions,
                       snapshot_worktree, strict_json)
from .git_repository import git, git_environment, repository_identity
from .owned_process import OwnedProcess

CODEX_VERSION = CODEX_EXEC_VERSION
PERMISSION_TEMPLATE = CODEX_EXEC_TEMPLATE


def codex_arguments(profile, schema: Path, result: Path, session=None):
    arguments = ['exec', *(['resume', session] if session else []), '--model', profile['model'],
        '--json', '--output-schema', str(schema), '--output-last-message', str(result),
        '--ignore-user-config', '--ignore-rules', '--strict-config',
        '-c', 'sandbox_mode="workspace-write"', '-c', 'approval_policy="never"',
        '-c', 'sandbox_workspace_write.network_access=false',
        '-c', 'cli_auth_credentials_store="file"', '-c', 'check_for_update_on_startup=false']
    if not session:
        arguments += ['--sandbox', 'workspace-write']
    return arguments + ['-']


def codex_environment(executable: Path, root: Path):
    environment = git_environment()
    system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    paths = [str(executable.parent), str(system / 'System32/WindowsPowerShell/v1.0'), str(system / 'System32')]
    native_git = shutil.which('git')
    if native_git:
        paths.append(str(Path(native_git).parent))
    environment.update(PATH=os.pathsep.join(paths), NO_COLOR='1', PYTHONUTF8='1')
    for key, relative in {'HOME': 'home', 'USERPROFILE': 'home', 'APPDATA': 'appdata',
            'LOCALAPPDATA': 'local-appdata', 'XDG_CONFIG_HOME': 'config', 'XDG_STATE_HOME': 'state',
            'TEMP': 'tmp', 'TMP': 'tmp', 'CODEX_HOME': 'home/codex'}.items():
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
            raise CodingFailure('native_home_redirected')
        environment[key] = str(directory)
    return environment


class CodexExecutor(HerdrExecutor):
    """Share cancellation/deadline ownership while keeping exec's real wire protocol."""
    def __init__(self, root, record, ledger, *, authorized, process_factory=OwnedProcess, task_directory=None):
        super().__init__(root, record, ledger, authorized=authorized)
        self.process_factory = process_factory
        self._auth_copy = None
        self._task_directory = Path(task_directory) if task_directory is not None else None
        if self._task_directory is not None and not self._task_directory.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('owned task directory must remain inside its host root')

    def _cleanup(self):
        with self._lock:
            process = self._client
        if process is not None:
            process.request_stop(grace_seconds=getattr(self, '_deadline', time.monotonic() + 5) - time.monotonic())
        super()._cleanup()
        if self._auth_copy is not None:
            self._auth_copy.unlink(missing_ok=True)
            self._auth_copy = None

    def run(self, execution):
        deadline = execution.deadline
        self._deadline = deadline
        record, ledger = dict(self.record), self.ledger
        attempt, profile, repository = record['attemptId'], record['profile'], record['repository']
        task_root, worktree, handoff_root = task_paths(self.root, record)
        if self._task_directory is not None:
            task_root = self._task_directory
            worktree, handoff_root = task_root / 'w', task_root / 'a' / attempt[8:]
        result_path, schema_path = handoff_root / 'result.json', handoff_root / 'schema.json'
        native, dispatched, business, cleanup = record.get('nativeSession'), False, 'failed', 'not_started'
        summary, blockers, validation, evidence, secrets = '', [], [], None, []
        auth_copy, protocol, native_terminal = None, None, False
        try:
            budget = begin_attempt_budget(record, deadline)
            deadline = budget['originalMonotonicDeadline']
            self._deadline = deadline
            record.update(budget)
            ledger.update_attempt(attempt, **budget)
            self._check(deadline)
            if profile['kind'] != 'codex' or profile['executor'] != 'codex_exec' or profile['version'] != CODEX_VERSION:
                raise CodingFailure('unsupported_codex_exec_profile')
            if profile.get('sha256'):
                executable_identity(profile['executable'], profile['sha256'])
            task_root.mkdir(parents=True, exist_ok=True)
            handoff_root.mkdir(parents=True, exist_ok=False)
            if repository_identity(repository['root'])['commonDir'] != repository['commonDir']:
                raise CodingFailure('registered_repository_changed')
            reject_project_cli_extensions(repository['root'])
            if native:
                if not worktree.is_dir() or not record.get('baseHead'):
                    raise CodingFailure('original_worktree_unavailable')
                base = record.get('expectedHead', record['baseHead'])
            else:
                if worktree.exists():
                    raise CodingFailure('new_worktree_collision')
                base = git(Path(repository['root']), 'rev-parse', '--verify', repository['baseRef'] + '^{commit}')
                git(Path(repository['root']), 'worktree', 'add', '-b', record['branch'], str(worktree), base)
                record['baseHead'] = base
                ledger.update_task(record['id'], baseHead=base, expectedHead=base)
                ledger.update_attempt(attempt, baseHead=base)
            record['startHead'] = base
            ledger.update_attempt(attempt, startHead=base)
            reject_project_cli_extensions(worktree)
            before = snapshot_worktree(worktree, repository['commonDir'], record['branch'], deadline)
            if before['head'] != base:
                raise CodingFailure('worktree_base_changed')
            env = codex_environment(Path(profile['executable']), task_root / 'native')
            self.native_home = Path(env['CODEX_HOME'])
            auth_source = Path(profile['authDirectory']) / 'auth.json'
            if not auth_source.is_file() or auth_source.is_symlink() or auth_source.stat().st_size > 256 * 1024:
                raise CodingFailure('dedicated_cli_authentication_unavailable')
            auth_copy = Path(env['CODEX_HOME']) / 'auth.json'
            if auth_copy.is_symlink():
                raise CodingFailure('native_authentication_redirected')
            self._auth_copy = auth_copy
            shutil.copyfile(auth_source, auth_copy)
            if hashlib.sha256(auth_copy.read_bytes()).hexdigest() != profile['authenticationRevision']:
                raise CodingFailure('dedicated_cli_authentication_changed')
            def collect(value):
                if isinstance(value, dict):
                    for child in value.values(): collect(child)
                elif isinstance(value, list):
                    for child in value: collect(child)
                elif isinstance(value, str) and value:
                    secrets.append(value)
            collect(strict_json(auth_copy.read_bytes()))
            schema_path.write_text(json.dumps(NativeResult.model_json_schema()), encoding='utf-8')
            handoff = {'taskId': record['id'], 'attemptId': attempt, 'requirement': record['requirement'],
                'validationIds': [item['id'] for item in repository.get('validation', [])],
                'instructions': 'Read this complete requirement. Edit only this approved worktree. Return JSON matching the supplied output schema with task_id=taskId and run_id=attemptId. Do not commit, fetch, push or create PRs. Validation IDs only reference host-approved checks.'}
            if record.get('qualificationChallenges'):
                handoff['qualificationChallenges'] = record['qualificationChallenges']
            prompt = json.dumps(handoff, ensure_ascii=False).encode('utf-8')
            (handoff_root / 'prompt.json').write_bytes(prompt)
            protocol = CodexProtocol(expected_session=native)
            def receive(chunk):
                previous = protocol.session
                protocol.feed(chunk)
                if protocol.session and protocol.session != previous:
                    ledger.update_attempt(attempt, nativeSession=protocol.session)
            self._check(deadline)
            ledger.update_attempt(attempt, status='running', cleanupState='running', dispatch=True, nativeTurnStatus='submitted')
            dispatched, cleanup = True, 'running'
            with self._lock:
                self._check(deadline)
                process = self.process_factory([profile['executable'], *codex_arguments(profile, schema_path, result_path, native)],
                    worktree, env, output_bytes=1024 * 1024, on_stdout=receive,
                    job_name='Local\\Meido.Coding.' + attempt + '.exec',
                    journal=lambda name, active: ledger.resource_job(attempt, name, active))
                self._client = process
            next_file_check = 0.0
            def check():
                nonlocal next_file_check
                self._check(deadline)
                if time.monotonic() >= next_file_check:
                    check_task_files(task_root, deadline - 60)
                    next_file_check = time.monotonic() + 1
            response = process.communicate(timeout=deadline - 60 - time.monotonic(), input=prompt, check=check,
                                           graceful_stop=True)
            native = protocol.finish(returncode=response.returncode)
            native_terminal = True
            ledger.update_attempt(attempt, nativeSession=native)
            if (not result_path.is_file() or result_path.is_symlink() or result_path.resolve().parent != handoff_root.resolve()
                    or result_path.stat().st_size > 256 * 1024):
                raise CodingFailure('result_file_invalid_or_over_budget')
            result_bytes = result_path.read_bytes()
            if len(result_bytes) > 256 * 1024:
                raise CodingFailure('result_file_over_budget')
            result = NativeResult.model_validate(strict_json(result_bytes))
            if result.task_id != record['id'] or result.run_id != attempt:
                raise CodingFailure('result_task_attempt_mismatch')
            summary, blockers = result.summary, result.blockers
            if result.status != 'completed' or blockers:
                business = 'needs_input' if result.status in {'needs_input', 'blocked'} else 'failed'
            else:
                evidence = snapshot_worktree(worktree, repository['commonDir'], record['branch'], deadline)
                if evidence['head'] != before['head']:
                    raise CodingFailure('unapproved_native_commit')
                approved = repository.get('validation', [])
                if not approved or not set(result.validation).issubset({item['id'] for item in approved}):
                    raise CodingFailure('approved_validation_missing_or_mismatched')
                for command in approved:
                    self._check(deadline)
                    cwd = (worktree / command.get('cwd', '.')).resolve(strict=True)
                    if not command['argv'] or not Path(command['argv'][0]).is_absolute() or not cwd.is_relative_to(worktree.resolve()):
                        raise CodingFailure('approved_validation_path_changed')
                    with self._lock:
                        self._validation = OwnedProcess(command['argv'], cwd, git_environment(), output_bytes=65536,
                            job_name='Local\\Meido.Coding.' + attempt + '.validation',
                            journal=lambda name, active: ledger.resource_job(attempt, name, active))
                    checked = self._validation.communicate(timeout=min(60, deadline - 60 - time.monotonic()))
                    validation.append({'id': command['id'], 'status': 'passed' if checked.returncode == 0 else 'failed', 'exitCode': checked.returncode})
                    if checked.returncode:
                        raise CodingFailure('host_validation_failed')
                if snapshot_worktree(worktree, repository['commonDir'], record['branch'], deadline) != evidence:
                    raise CodingFailure('files_changed_during_validation')
                if not evidence['status']:
                    raise CodingFailure('reviewable_diff_missing')
                business = 'validated_diff'
        except BaseException as error:
            business = 'cancelled' if self._cancelled.is_set() else 'timed_out' if isinstance(error, TimeoutError) else 'unknown' if dispatched else 'failed'
            blockers = [getattr(error, 'code', type(error).__name__)]
        finally:
            if protocol is not None and protocol.session:
                native = protocol.session
            try:
                self._cleanup()
                if auth_copy is not None:
                    auth_copy.unlink(missing_ok=True)
                cleanup = 'complete'
            except BaseException:
                cleanup, business, blockers = 'cleanup_failed', 'unknown', ['native_cleanup_failed']
            if cleanup == 'complete' and business == 'validated_diff':
                try:
                    if self._cancelled.is_set() or not self.authorized():
                        raise CodingFailure('attempt_permission_ended')
                    check_task_files(task_root, deadline)
                    attestation = attest_snapshot(worktree,
                        record | {'nativeSession': native, 'cleanupState': 'complete'}, deadline, validation)
                    final = attestation['snapshot']
                    if final != evidence:
                        raise CodingFailure('files_changed_after_validation')
                    ledger.update_attempt(attempt, validatedHead=final['head'], validationEvidence=attestation)
                except BaseException as error:
                    business, blockers = 'unknown', [getattr(error, 'code', type(error).__name__)]
            for secret in secrets:
                summary = summary.replace(secret, '[REDACTED]')
                blockers = [item.replace(secret, '[REDACTED]') for item in blockers]
            ledger.update_attempt(attempt, status=business, summary=summary, blockers=blockers,
                cleanupState=cleanup, nativeSession=native, validation=validation,
                nativeTurnStatus='completed' if native_terminal else 'unknown' if dispatched else 'not_started',
                observedAt=time.time())
        return TaskOutcome('succeeded' if business == 'validated_diff' else 'failed', 0 if business == 'validated_diff' else None,
                           json.dumps({'status': business, 'summary': summary[:4000], 'cleanupState': cleanup}, ensure_ascii=False))
