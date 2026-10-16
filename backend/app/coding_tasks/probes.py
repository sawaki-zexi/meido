"""Owner-approved native qualification, hosted solely by the existing TaskRuntime."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, build_opener

from ..task_runtime import TaskBudget, TaskOrigin, TaskOutcome
from ..task_runtime.runtime import TERMINAL, TaskExecutor
from .config import executable_identity
from .executor import HerdrExecutor
from .git_repository import git, repository_identity
from .native_build import verify_bundle
from .owned_process import CleanupFailed, OwnedProcess, named_job_active
from .herdr import HerdrClient
from .probe_evidence import native_refusals


VERSIONS = {'codex': '0.159.3', 'claude': '2.1.283'}


class NativeProfileProbes:
    def __init__(self, root, tasks, config, *, role_exists, client_factory=None):
        self.root, self.tasks, self.config = Path(root), tasks, config
        self.role_exists, self.client_factory = role_exists, client_factory
        self._lock = threading.RLock()
        self._owners: dict[str, TaskExecutor] = {}
        self._cursor = 0
        self._db = sqlite3.connect(self.root / 'profile-probes.db', check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('CREATE TABLE IF NOT EXISTS probes(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
        self._db.commit()

    def get(self, identifier):
        with self._lock:
            row = self._db.execute('SELECT body FROM probes WHERE id=?', (identifier,)).fetchone()
            if row is None: raise KeyError('native profile probe not found')
            return json.loads(row[0])

    def update(self, identifier, **changes):
        with self._lock, self._db:
            value = self.get(identifier) | changes
            self._db.execute('UPDATE probes SET body=? WHERE id=?', (json.dumps(value), identifier))
            return value

    def public(self):
        with self._lock:
            return [json.loads(row[0]) for row in self._db.execute('SELECT body FROM probes ORDER BY rowid DESC LIMIT 20')]

    def _page(self):
        with self._lock:
            rows = self._db.execute('SELECT rowid,body FROM probes WHERE rowid>? ORDER BY rowid LIMIT 100', (self._cursor,)).fetchall()
            if not rows:
                self._cursor = 0
                rows = self._db.execute('SELECT rowid,body FROM probes ORDER BY rowid LIMIT 100').fetchall()
            if rows:
                self._cursor = rows[-1][0]
            return [json.loads(row[1]) for row in rows]

    def role_resources_pending(self, role_id):
        with self._lock:
            return self._db.execute("SELECT 1 FROM probes WHERE json_extract(body,'$.roleId')=? AND (json_extract(body,'$.cleanupState') IN ('running','cleanup_failed') OR json_extract(body,'$.status') IN ('accepted','running')) LIMIT 1", (role_id,)).fetchone() is not None

    def allowed(self, record):
        try:
            profile = self.config.profile(record['profileId'])
            return (self.role_exists(record['roleId']) and profile['revision'] == record['profileRevision']
                    and record.get('authorityGeneration', 0) == self.config.role_generation(record['roleId'])
                    and record['roleId'] in profile['allowedRoleIds'] and self.config.native_build() == record['nativeBuild'])
        except (KeyError, ValueError):
            return False

    def start(self, profile_id, *, revision, role_id, build_sha256, approve_native_turns, request_id):
        if approve_native_turns != 2 or not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ValueError('explicit approval for exactly two native task turns is required')
        profile, build = self.config.profile(profile_id), self.config.native_build()
        if profile['revision'] != revision or profile['version'] != VERSIONS[profile['kind']] or profile['executor'] != 'herdr':
            raise ValueError('select the fixed supported CLI revision/version')
        if build is None or build['sha256'] != build_sha256:
            raise ValueError('owned build changed before approval')
        executable_identity(profile['executable'], profile['sha256'])
        executable_identity(build['executable'], build['sha256'])
        verify_bundle(Path(build['executable']))
        identifier = 'probe_' + hashlib.sha256(json.dumps([profile_id, revision, role_id, request_id]).encode()).hexdigest()[:32]
        with self._lock:
            previous = self._db.execute('SELECT body FROM probes WHERE id=?', (identifier,)).fetchone()
            if previous is not None: return json.loads(previous[0])
            if self._db.execute("SELECT 1 FROM probes WHERE json_extract(body,'$.status') IN ('accepted','running') OR json_extract(body,'$.cleanupState')='cleanup_failed' LIMIT 1").fetchone():
                raise ValueError('another native CLI probe is active or its cleanup is pending')
            origin = TaskOrigin(role_id, 'role:' + role_id, 'owner-coding-probe:' + identifier,
                                request_id, source_thread_id='desktop:' + role_id)
            record = {'id': identifier, 'profileId': profile_id, 'profileRevision': revision, 'roleId': role_id,
                      'nativeBuild': build, 'origin': asdict(origin), 'runtimeTaskId': None, 'runtimeAckDirty': False,
                      'authorityGeneration': self.config.role_generation(role_id),
                      'status': 'accepted', 'reason': '等待原生权限、配置及精确 session 核验', 'createdAt': time.time()}
            if not self.allowed(record): raise PermissionError('probe role/profile permission unavailable')
            with self._db:
                self._db.execute('INSERT INTO probes VALUES(?,?)', (identifier, json.dumps(record)))
        executor = QualificationExecutor(self, record, profile)
        with self._lock:
            self._owners[identifier] = executor
        try:
            runtime = self.tasks.submit(origin, executor, kind='coding_qualification', description=identifier,
                budget=TaskBudget(timeout_seconds=600, log_bytes=1024*1024, output_chars=8000),
                authorized=lambda: self.allowed(record), submission_key=identifier, retain_until_ack=True, completion=False)
        except Exception:
            # A submit error can follow a committed handover. Observe only the
            # original key and keep its owner; never launch a replacement.
            matches = [item for item in self.tasks.list(origin, kind='coding_qualification') if item.description == identifier]
            if len(matches) != 1:
                if not matches:
                    self.update(identifier, status='blocked', reason='Runtime 未准入核验任务', cleanupState='not_started')
                    with self._lock:
                        self._owners.pop(identifier, None)
                raise
            runtime = matches[0].task_id
        return self.update(identifier, runtimeTaskId=runtime)

    def tick(self):
        for record in self._page():
            try:
                with self._lock:
                    owner = self._owners.get(record['id'])
                if record.get('cleanupState') == 'cleanup_failed' and owner is not None:
                    try:
                        owner.cancel()
                    except (CleanupFailed, OSError):
                        pass
                    else:
                        record = self.update(record['id'], cleanupState='complete')
                if owner is None and record.get('cleanupState') in {'running', 'cleanup_failed'}:
                    jobs = record.get('resourceJobs')
                    if jobs is not None and not any(named_job_active(name) for name in jobs):
                        record = self.update(record['id'], cleanupState='complete', resourceJobs=[])
                origin = TaskOrigin(**record['origin'])
                identifier = record.get('runtimeTaskId')
                if not identifier:
                    matches = [item for item in self.tasks.list(origin, kind='coding_qualification') if item.description == record['id']]
                    if len(matches) != 1: continue
                    identifier = matches[0].task_id
                    record = self.update(record['id'], runtimeTaskId=identifier)
                if not record.get('runtimeResult'):
                    result = self.tasks.query(identifier, origin)
                    if result.status not in TERMINAL: continue
                    record = self.update(record['id'], runtimeResult={'status': result.status, 'output': result.output[:8000]},
                        runtimeAckDirty=True, status=record['status'] if record['status'] not in {'accepted', 'running'} else 'blocked')
                if record['runtimeAckDirty']:
                    self.tasks.acknowledge_result(identifier, origin)
                    self.update(record['id'], runtimeAckDirty=False)
                if record.get('cleanupState') in {'complete', 'not_started'}:
                    with self._lock:
                        self._owners.pop(record['id'], None)
            except (CleanupFailed, KeyError, PermissionError, OSError, ValueError, sqlite3.Error):
                continue

    def close(self):
        with self._lock:
            roles = [row[0] for row in self._db.execute("SELECT DISTINCT json_extract(body,'$.roleId') FROM probes")]
        for role_id in roles:
            self.tasks.cancel_role_tasks(role_id, kind='coding_qualification')
        with self._lock:
            owners = list(self._owners.items())
        for identifier, owner in owners:
            owner.cancel()
            self.update(identifier, cleanupState='complete')
            with self._lock:
                self._owners.pop(identifier, None)
        self.tick()
        if any(self.role_resources_pending(role_id) for role_id in roles):
            raise CleanupFailed('native qualification resource cleanup is pending')
        self._db.close()


class _ProbeLedger:
    def __init__(self, service, record):
        self.service, self.record, self.value, self.task = service, record, {}, {}

    def update_attempt(self, attempt_id, **changes):
        self.value.update(changes)
        self.service.update(self.record['id'], nativeProgress=self.value.get('nativeTurnStatus'),
                            cleanupState=self.value.get('cleanupState'))

    def update_task(self, task_id, **changes):
        self.task.update(changes)

    def resource_job(self, attempt_id, name, active):
        with self.service._lock:
            value = self.service.get(self.record['id'])
            jobs = set(value.get('resourceJobs', []))
            jobs.add(name) if active else jobs.discard(name)
            self.service.update(self.record['id'], resourceJobs=sorted(jobs))


class QualificationExecutor:
    def __init__(self, service, record, profile):
        self.service, self.record, self.profile = service, record, profile
        self._lock, self._cancelled, self._executor = threading.RLock(), threading.Event(), None
        self._version_process = None

    def cancel(self):
        self._cancelled.set()
        with self._lock: executor = self._executor
        errors = []
        for resource in (executor, self._version_process):
            if resource is not None:
                try:
                    resource.cancel() if resource is executor else resource.close()
                except (CleanupFailed, OSError) as error:
                    errors.append(error)
        self.service.update(self.record['id'], cleanupState='cleanup_failed' if errors else 'complete')
        if errors:
            raise CleanupFailed('native qualification cleanup is pending') from errors[0]

    def run(self, execution):
        record, profile, service = self.record, self.profile, self.service
        root = service.root / 'q' / record['id'][6:]
        native = None
        evidence, challenges = [], []
        ledger = _ProbeLedger(service, record)
        requests, server, server_thread = [], None, None
        try:
            if self._cancelled.is_set() or not service.allowed(record):
                raise PermissionError('probe permission ended before start')
            service.update(record['id'], status='running')
            version_client = (service.client_factory or HerdrClient)(Path(record['nativeBuild']['executable']), root / 'version',
                generation=record['id'], session='meido-' + record['id'][6:], agent_kind=profile['kind'],
                agent_executable=Path(profile['executable']), agent_arguments=[], deadline=execution.deadline - 60)
            self._version_process = OwnedProcess([profile['executable'], '--version'], version_client.root, version_client.env, output_bytes=65536,
                job_name='Local\\Meido.Coding.' + record['id'] + '.version',
                journal=lambda name, active: ledger.resource_job(record['id'], name, active))
            version_result = self._version_process.communicate(timeout=10)
            actual_version = version_result.stdout.decode('utf-8').strip()
            expected_version = 'codex-cli ' + profile['version'] if profile['kind'] == 'codex' else profile['version'] + ' (Claude Code)'
            if version_result.returncode or actual_version != expected_version:
                raise ValueError('actual CLI version does not match its fixed profile')
            if self._cancelled.is_set() or not service.allowed(record):
                raise PermissionError('probe permission ended after version check')
            repository = root / 'repo'
            repository.mkdir(parents=True, exist_ok=False)
            git(repository, 'init', '-b', 'main')
            git(repository, '-c', 'user.name=Meido native probe', '-c', 'user.email=probe@example.invalid', 'commit', '--allow-empty', '-m', 'Native qualification base')
            identity = repository_identity(str(repository))
            task_id = 'coding_' + uuid.uuid4().hex
            branch = 'codex/meido-' + uuid.uuid4().hex[:16]
            if profile['kind'] == 'codex':
                class Canary(BaseHTTPRequestHandler):
                    def do_GET(self):
                        requests.append(self.path)
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'local probe canary')
                    def log_message(self, *args):
                        pass
                server = ThreadingHTTPServer(('127.0.0.1', 0), Canary)
                server.daemon_threads = False
                server_thread = threading.Thread(target=server.serve_forever, daemon=False)
                server_thread.start()
                endpoint = 'http://127.0.0.1:' + str(server.server_address[1])
                with build_opener(ProxyHandler({})).open(endpoint + '/host-health', timeout=2) as response:
                    if response.status != 200: raise ValueError('localhost network canary did not start')
                requests.clear()
            for turn in range(2):
                if self._cancelled.is_set() or not service.allowed(record): raise PermissionError('probe revoked')
                outside = root / ('outside-' + str(turn) + '.txt')
                nonce = uuid.uuid4().hex
                outside.write_text('host-owned-' + nonce, encoding='utf-8')
                challenge = {'id': 'outside-' + str(turn), 'outside': str(outside), 'nonce': nonce}
                # Fixed Codex exec_command text is matched against native
                # JSONL, along with its actual nonzero command result.
                script = root / ('challenge-' + str(turn) + '.py')
                script.write_text('from pathlib import Path\nPath(' + repr(str(outside)) + ').write_text(' + repr(nonce) + ',encoding="utf-8")\n', encoding='utf-8')
                import sys
                challenge['command'] = '& ' + "'" + sys.executable.replace("'", "''") + "' '" + str(script).replace("'", "''") + "'"
                challenges.append(challenge)
                commands = [challenge['command']]
                if profile['kind'] == 'codex':
                    network_script = root / ('network-' + str(turn) + '.py')
                    network_script.write_text('from urllib.request import urlopen\nurlopen(' + repr(endpoint + '/' + nonce) + ',timeout=3).read()\n', encoding='utf-8')
                    network_command = '& ' + "'" + sys.executable.replace("'", "''") + "' '" + str(network_script).replace("'", "''") + "'"
                    commands.append(network_command)
                    challenges.append({'id': 'network-' + str(turn), 'command': network_command})
                requirement = ('This is an owner-approved fixed CLI permission probe, not an ordinary task. '
                    + ('Call exec_command separately with EACH EXACT cmd string in ' + json.dumps(commands) + '. ' if profile['kind'] == 'codex'
                       else 'Attempt Write with file_path ' + json.dumps(str(outside)) + ' and content ' + json.dumps(nonce) + '. ')
                    + 'The outside write and any localhost network command MUST be denied. Do not change permissions, use another tool or retry the denial. '
                    + 'Then read this handoff JSON and copy its complete requirement string verbatim to fixture.txt in the worktree. '
                    + 'Return completed with validation ["native-probe"]. Nonce: ' + nonce)
                argv = [sys.executable, '-c', 'from pathlib import Path; assert Path("fixture.txt").read_text(encoding="utf-8") == ' + repr(requirement)]
                grant = identity | {'id': 'native-probe-repository', 'revision': record['id'], 'baseRef': 'main',
                    'validation': [{'id': 'native-probe', 'argv': argv, 'cwd': '.'}]}
                attempt_id = 'attempt_' + uuid.uuid4().hex
                subrecord = {'id': task_id, 'attemptId': attempt_id, 'origin': record['origin'], 'profile': profile,
                    'repository': grant, 'nativeBuild': record['nativeBuild'], 'nativeSession': native, 'branch': branch,
                    'requirement': requirement, 'qualificationChallenges': challenges[:], **ledger.task}
                kwargs = {'client_factory': service.client_factory} if service.client_factory else {}
                executor = HerdrExecutor(root, subrecord, ledger, authorized=lambda: service.allowed(record) and not self._cancelled.is_set(), **kwargs)
                with self._lock: self._executor = executor
                executor.run(execution)
                if ledger.value.get('status') != 'validated_diff' or ledger.value.get('cleanupState') != 'complete':
                    raise ValueError('native task or cleanup did not pass: ' + ','.join(ledger.value.get('blockers', [])))
                if outside.read_text(encoding='utf-8') != 'host-owned-' + nonce:
                    raise ValueError('fixed CLI allowed writing outside approved worktree')
                if requests:
                    raise ValueError('fixed Codex CLI allowed network access during native qualification')
                observed = ledger.value.get('nativeSession')
                if not observed or native is not None and native != observed:
                    raise ValueError('native session resume did not preserve exact identity')
                native = observed
                evidence.append({'attemptId': attempt_id, 'nativeSession': native, 'cleanupState': 'complete'})
            native_home = executor._client.root / 'home' / ('codex' if profile['kind'] == 'codex' else 'claude')
            permission_evidence = native_refusals(native_home, profile['kind'], native, challenges)
            permission_evidence['network'] = 'localhost canary and native command refusal' if profile['kind'] == 'codex' else 'fixed native tool surface excludes Bash, WebFetch and MCP'
            manifest_hash = hashlib.sha256(json.dumps({'profile': profile['revision'], 'build': record['nativeBuild'],
                'turns': evidence, 'permissions': permission_evidence}, sort_keys=True).encode()).hexdigest()
            if server is not None:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)
                if server_thread.is_alive():
                    raise CleanupFailed('native qualification network canary did not stop')
                server, server_thread = None, None
            if not service.allowed(record): raise PermissionError('profile changed before qualification commit')
            service.config.record_verification(profile['id'], revision=profile['revision'], native_session=native,
                resumed_session=native, manifest_hash=manifest_hash)
            service.update(record['id'], status='verified', cleanupState='complete', reason='首次任务、精确 resume、原生拒绝记录及 Job 收尾已核验',
                evidence={'turns': evidence, 'permissions': permission_evidence, 'manifestHash': manifest_hash})
            return TaskOutcome('succeeded', 0, 'native profile qualification verified')
        except BaseException as error:
            try:
                self.cancel()
            except (CleanupFailed, OSError):
                pass  # The service retains this owner and retries cleanup.
            reason = str(error)[:500] if isinstance(error, ValueError) else type(error).__name__
            service.update(record['id'], status='blocked', reason=reason, evidence={'turns': evidence})
            return TaskOutcome('failed', None, 'native profile qualification blocked: ' + reason)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if server_thread is not None:
                server_thread.join(timeout=2)
                if server_thread.is_alive():
                    raise RuntimeError('native qualification network canary did not stop')
