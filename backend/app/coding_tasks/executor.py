from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..task_runtime import TaskOutcome
from .git_repository import git, git_environment, repository_identity
from .herdr import HerdrClient
from .owned_process import CleanupFailed, OwnedProcess
from .errors import CodingFailure, CodingDeadline


class NativeResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    task_id: str
    run_id: str
    status: Literal['completed', 'needs_input', 'failed', 'blocked']
    summary: str = Field(max_length=8000)
    validation: list[Annotated[str, Field(max_length=100)]] = Field(max_length=100)
    blockers: list[Annotated[str, Field(max_length=1000)]] = Field(max_length=100)


def strict_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def _edit_rule(directory):
    path = Path(directory).resolve().as_posix()
    drive = Path(directory).drive
    if drive:
        path = '/' + drive[0].lower() + path[len(drive):]
    path = re.sub(r'([*?\[\]\\])', r'\\\1', path)
    return 'Edit(/' + path + '/**)'


def agent_arguments(profile, worktree, output_directory, native_session=None):
    if profile['kind'] == 'codex':
        arguments = ['--model', profile['model'], '--add-dir', str(output_directory), '--sandbox', 'workspace-write', '--ask-for-approval', 'never',
                     '--config', 'sandbox_workspace_write.network_access=false']
        if native_session:
            arguments = ['resume', native_session, *arguments]
    else:
        arguments = ['--model', profile['model'], '--add-dir', str(output_directory), '--permission-mode', 'dontAsk', '--tools', 'Read,Glob,Grep,Edit,Write',
                     '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--setting-sources', 'user',
                     '--settings', json.dumps({'permissions': {'allow': [_edit_rule(worktree), _edit_rule(output_directory)],
                         'deny': ['Bash', 'PowerShell', 'WebFetch', 'mcp__*']}})]
        if native_session:
            arguments += ['--resume', native_session]
    return arguments


def snapshot_worktree(worktree, common_dir, branch, deadline):
    root = Path(worktree).resolve(strict=True)
    actual_common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve(strict=True)
    if actual_common != Path(common_dir).resolve(strict=True) or git(root, 'branch', '--show-current') != branch:
        raise CodingFailure('worktree_identity_changed')
    # Cached files include staged-only edits; worktree-only --modified would
    # miss a validator changing an already staged file.
    files = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0')
    if len(files) > 10000:
        raise CodingFailure('worktree_file_count_budget')
    hashes, total = {}, 0
    for name in files:
        if not name:
            continue
        path = root / name
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or path.is_symlink():
            raise CodingFailure('worktree_file_redirected')
        if not path.exists():
            hashes[name] = 'deleted'
            continue
        total += path.stat().st_size
        if total > 2 * 1024 * 1024 * 1024:
            raise CodingFailure('task_file_budget_exceeded')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise CodingDeadline()
                digest.update(chunk)
        hashes[name] = digest.hexdigest()
    return {'head': git(root, 'rev-parse', 'HEAD'), 'status': git(root, 'status', '--porcelain'), 'files': hashes}


def check_task_files(root, deadline):
    total, count = 0, 0
    for path in root.rglob('*'):
        if time.monotonic() >= deadline:
            raise CodingDeadline()
        if path.is_symlink():
            raise CodingFailure('task_file_redirection_unavailable')
        if path.is_file():
            count += 1
            total += path.stat().st_size
            if count > 10000 or total > 2 * 1024 * 1024 * 1024:
                raise CodingFailure('task_file_budget_exceeded')


def reject_project_cli_extensions(*roots):
    checked = set()
    for root in roots:
        current = Path(root).resolve()
        for directory in (current, *current.parents):
            if directory in checked:
                break
            checked.add(directory)
            for name in ('.codex/config.toml', '.codex/hooks.json', '.claude/settings.json', '.claude/settings.local.json', '.mcp.json'):
                if (directory / name).exists():
                    raise CodingFailure('project_cli_extensions_unavailable')
            if (directory / '.git').exists():
                break


class HerdrExecutor:
    def __init__(self, root, record, ledger, *, authorized, client_factory=HerdrClient):
        self.root, self.record, self.ledger = Path(root), record, ledger
        self.authorized, self.client_factory = authorized, client_factory
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self._client = None
        self._validation = None

    def _check(self, deadline):
        if self._cancelled.is_set() or not self.authorized():
            raise CodingFailure('attempt_permission_ended')
        if time.monotonic() >= deadline - 60:
            raise CodingDeadline()

    def run(self, execution):
        record = self.record
        attempt = record['attemptId']
        business, summary, blockers = 'failed', '', []
        dispatched, native, cleanup = False, record.get('nativeSession'), 'not_started'
        validation, evidence = [], None
        secrets, auth_copy = [], None
        task_root = self.root / 't' / hashlib.sha256(record['origin']['role_id'].encode()).hexdigest()[:16] / record['id'][7:]
        worktree, handoff_root = task_root / 'w', task_root / 'a' / attempt[8:]
        result_path = handoff_root / 'result.json'
        profile, repository = record['profile'], record['repository']
        try:
            task_root.mkdir(parents=True, exist_ok=True)
            handoff_root.mkdir(parents=True, exist_ok=False)
            self._check(execution.deadline)
            identity = repository_identity(repository['root'])
            if identity['commonDir'] != repository['commonDir']:
                raise CodingFailure('registered_repository_changed')
            reject_project_cli_extensions(repository['root'])
            if worktree.exists():
                reject_project_cli_extensions(worktree)
            arguments = agent_arguments(profile, worktree, handoff_root, native)
            client = self.client_factory(Path(record['nativeBuild']['executable']), task_root / 'native',
                generation=attempt, session='meido-' + record['id'][7:39], agent_kind=profile['kind'],
                agent_executable=Path(profile['executable']), agent_arguments=arguments, deadline=execution.deadline - 60,
                resource_journal=lambda name, active: self.ledger.resource_job(attempt, name, active))
            with self._lock:
                if self._cancelled.is_set():
                    client.close()
                    raise CodingFailure('cancelled_before_native_start')
                self._client = client
            self.ledger.update_attempt(attempt, status='running', cleanupState='running', nativeTurnStatus='starting')
            cleanup = 'running'
            auth_name = 'auth.json' if profile['kind'] == 'codex' else '.credentials.json'
            auth_source = Path(profile['authDirectory']) / auth_name
            native_home = Path(client.env['CODEX_HOME' if profile['kind'] == 'codex' else 'CLAUDE_CONFIG_DIR'])
            if not auth_source.is_file() or auth_source.is_symlink() or auth_source.stat().st_size > 256 * 1024:
                raise CodingFailure('dedicated_cli_authentication_unavailable')
            shutil.copyfile(auth_source, native_home / auth_name)
            auth_copy = native_home / auth_name
            expected_auth = profile.get('authenticationRevision')
            if expected_auth is not None and hashlib.sha256(auth_copy.read_bytes()).hexdigest() != expected_auth:
                raise CodingFailure('dedicated_cli_authentication_changed')
            try:
                auth_payload = strict_json(auth_copy.read_bytes())
                def collect(value):
                    if isinstance(value, dict):
                        for item in value.values(): collect(item)
                    elif isinstance(value, list):
                        for item in value: collect(item)
                    elif isinstance(value, str) and value:
                        secrets.append(value)
                collect(auth_payload)
            except ValueError:
                raise CodingFailure('dedicated_cli_authentication_invalid') from None
            client.install_integration(profile['kind'])
            status = client.start()
            self.ledger.update_attempt(attempt, serverGeneration=status['generation'], serverPid=status['pid'])
            self._check(execution.deadline)
            if native:
                if not worktree.is_dir() or not record.get('baseHead'):
                    raise CodingFailure('original_worktree_unavailable')
                workspace = client.call(['worktree', 'open', '--cwd', repository['root'], '--path', str(worktree), '--label', record['id'], '--no-focus'])
                base = record['baseHead']
            else:
                if worktree.exists():
                    raise CodingFailure('new_worktree_collision')
                base = git(Path(repository['root']), 'rev-parse', '--verify', repository['baseRef'] + '^{commit}')
                workspace = client.call(['worktree', 'create', '--cwd', repository['root'], '--branch', record['branch'],
                    '--base', base, '--path', str(worktree), '--label', record['id'], '--no-focus'])
                self.ledger.update_task(record['id'], baseHead=base)
                self.ledger.update_attempt(attempt, baseHead=base)
            reject_project_cli_extensions(worktree)
            before = snapshot_worktree(worktree, repository['commonDir'], record['branch'], execution.deadline)
            if before['head'] != base:
                raise CodingFailure('worktree_base_changed')
            pane = workspace['root_pane']['pane_id']
            agent_name = 'meido-' + attempt[8:24]
            agent = client.launch_agent(agent_name, pane, profile['kind'], arguments)
            observed_native = agent['agent_session']['value']
            if native and observed_native != native:
                raise CodingFailure('exact_native_session_mismatch')
            native = observed_native
            binding = {key: agent[key] for key in ('terminal_id', 'workspace_id', 'pane_id', 'agent')}
            self.ledger.update_attempt(attempt, nativeSession=native, binding=binding, nativeTurnStatus='ready')
            handoff = {'taskId': record['id'], 'attemptId': attempt, 'requirement': record['requirement'],
                       'resultFile': str(result_path), 'resultSchema': NativeResult.model_json_schema(),
                       'instructions': 'Read the complete requirement. Edit only this approved worktree. Write a fresh JSON result matching resultSchema. Do not commit, fetch, push or create PRs. Validation names are references to host-approved checks.'}
            if record.get('qualificationChallenges'):
                handoff['qualificationChallenges'] = record['qualificationChallenges']
            path = handoff_root / 'handoff.json'
            path.write_text(json.dumps(handoff, ensure_ascii=False), encoding='utf-8')
            self._check(execution.deadline)
            self.ledger.update_attempt(attempt, dispatch=True, nativeTurnStatus='submitted')
            dispatched = True
            client.prompt(agent_name, 'Read and follow the complete UTF-8 task handoff file, including its requirement, boundaries and resultSchema; write the fresh resultFile it specifies. MEIDO_HANDOFF ' + str(path))
            next_file_check = 0
            while not result_path.exists():
                self._check(execution.deadline)
                if time.monotonic() >= next_file_check:
                    check_task_files(task_root, execution.deadline - 60)
                    next_file_check = time.monotonic() + 1
                observed = client.observe_agent(agent_name)
                if any(observed.get(key) != value for key, value in binding.items()) or observed.get('agent_session', {}).get('value') != native:
                    raise CodingFailure('native_agent_identity_changed')
                time.sleep(.05)
            if result_path.is_symlink() or result_path.resolve().parent != handoff_root.resolve() or result_path.stat().st_size > 256 * 1024:
                raise CodingFailure('result_file_invalid_or_over_budget')
            with result_path.open('rb') as stream:
                result_bytes = stream.read(256 * 1024 + 1)
            if len(result_bytes) > 256 * 1024:
                raise CodingFailure('result_file_over_budget')
            observed = client.observe_agent(agent_name)
            if (any(observed.get(key) != value for key, value in binding.items())
                    or observed.get('agent_session', {}).get('value') != native):
                raise CodingFailure('native_result_binding_changed')
            result = NativeResult.model_validate(strict_json(result_bytes))
            check_task_files(task_root, execution.deadline - 60)
            if result.task_id != record['id'] or result.run_id != attempt:
                raise CodingFailure('result_task_attempt_mismatch')
            summary, blockers = result.summary, result.blockers
            if result.status != 'completed' or blockers:
                business = 'needs_input' if result.status in {'needs_input', 'blocked'} else 'failed'
            else:
                evidence = snapshot_worktree(worktree, repository['commonDir'], record['branch'], execution.deadline)
                if evidence['head'] != before['head']:
                    raise CodingFailure('unapproved_native_commit')
                approved = repository.get('validation', [])
                if not approved or not set(result.validation).issubset({item['id'] for item in approved}):
                    raise CodingFailure('approved_validation_missing_or_mismatched')
                for command in approved:
                    self._check(execution.deadline)
                    argv = command['argv']
                    cwd = (worktree / command.get('cwd', '.')).resolve(strict=True)
                    if not argv or not Path(argv[0]).is_absolute() or not cwd.is_relative_to(worktree.resolve()):
                        raise CodingFailure('approved_validation_path_changed')
                    with self._lock:
                        self._validation = OwnedProcess(argv, cwd, git_environment(), output_bytes=65536,
                            job_name='Local\\Meido.Coding.' + attempt + '.validation',
                            journal=lambda name, active: self.ledger.resource_job(attempt, name, active))
                        process = self._validation
                    checked = process.communicate(timeout=min(60, execution.deadline - 60 - time.monotonic()))
                    validation.append({'id': command['id'], 'status': 'passed' if checked.returncode == 0 else 'failed', 'exitCode': checked.returncode})
                    if checked.returncode:
                        raise CodingFailure('host_validation_failed')
                after = snapshot_worktree(worktree, repository['commonDir'], record['branch'], execution.deadline)
                if after != evidence:
                    raise CodingFailure('files_changed_during_validation')
                if not after['status']:
                    raise CodingFailure('reviewable_diff_missing')
                business = 'validated_diff'
        except BaseException as error:
            business = 'cancelled' if self._cancelled.is_set() else 'timed_out' if isinstance(error, TimeoutError) else 'unknown' if dispatched else 'failed'
            blockers = [getattr(error, 'code', type(error).__name__)]
        finally:
            try:
                self._cleanup()
                if auth_copy is not None:
                    auth_copy.unlink(missing_ok=True)
                cleanup = 'complete'
            except BaseException:
                cleanup, business = 'cleanup_failed', 'unknown'
                blockers = ['native_cleanup_failed']
            if cleanup == 'complete' and business == 'validated_diff':
                try:
                    if self._cancelled.is_set() or not self.authorized():
                        raise CodingFailure('attempt_permission_ended')
                    # The agent may still write after its result file or the
                    # last validator returns. Only a snapshot after every
                    # owned process has exited can preserve that evidence.
                    check_task_files(task_root, execution.deadline)
                    final = snapshot_worktree(worktree, repository['commonDir'], record['branch'], execution.deadline)
                    if final != evidence:
                        raise CodingFailure('files_changed_after_validation')
                    self.ledger.update_attempt(attempt, validatedHead=final['head'])
                except BaseException as error:
                    business = 'cancelled' if self._cancelled.is_set() else 'timed_out' if isinstance(error, TimeoutError) else 'unknown'
                    blockers = [getattr(error, 'code', type(error).__name__)]
            for secret in secrets:
                summary = summary.replace(secret, '[REDACTED]')
                blockers = [item.replace(secret, '[REDACTED]') for item in blockers]
            self.ledger.update_attempt(attempt, status=business, summary=summary, blockers=blockers,
                cleanupState=cleanup, nativeSession=native, validation=validation,
                nativeTurnStatus='completed' if business == 'validated_diff' else 'unknown' if dispatched else 'not_started',
                observedAt=time.time())
        return TaskOutcome('succeeded' if business == 'validated_diff' else 'failed', 0 if business == 'validated_diff' else None,
                           json.dumps({'status': business, 'summary': summary[:4000], 'cleanupState': cleanup}, ensure_ascii=False))

    def _cleanup(self):
        with self._lock:
            validation, client = self._validation, self._client
        if validation is not None:
            validation.close()
        if client is not None:
            client.close()

    def cancel(self):
        self._cancelled.set()
        self._cleanup()
