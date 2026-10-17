from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from .git_repository import git_environment
from .owned_process import CleanupFailed, OwnedProcess


class HerdrProtocolError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__('Herdr protocol rejected: ' + code)


def owned_environment(executable: Path, root: Path, *, generation: str, session: str,
                      kind: str, agent_executable: Path, arguments: list[str]) -> dict[str, str]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    environment = git_environment()
    system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    shell = system / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    git = shutil.which('git')
    path = [str(executable.parent), str(agent_executable.parent), str(shell.parent), str(system / 'System32')]
    if git:
        path.append(str(Path(git).parent))
    environment['PATH'] = os.pathsep.join(path)
    for key, directory in {'HOME': 'home', 'USERPROFILE': 'home', 'APPDATA': 'appdata',
                           'LOCALAPPDATA': 'local-appdata', 'XDG_CONFIG_HOME': 'config',
                           'XDG_STATE_HOME': 'state', 'TEMP': 'tmp', 'TMP': 'tmp',
                           'CODEX_HOME': 'home/codex', 'CLAUDE_CONFIG_DIR': 'home/claude'}.items():
        target = root / directory
        target.mkdir(parents=True, exist_ok=True)
        environment[key] = str(target)
    configuration = root / 'config/herdr-owned.toml'
    configuration.write_text('[update]\nversion_check=false\nmanifest_check=false\n[session]\nresume_agents_on_restore=false\n', encoding='utf-8')
    environment.update({'HERDR_CONFIG_PATH': str(configuration), 'MEIDO_HERDR_OWNED': '1',
                        'MEIDO_HERDR_ROOT': str(root), 'MEIDO_HERDR_GENERATION': generation,
                        'MEIDO_HERDR_SESSION': session, 'MEIDO_HERDR_SHELL': str(shell),
                        'MEIDO_HERDR_AGENT_KIND': kind, 'MEIDO_HERDR_AGENT_EXE': str(agent_executable.resolve()),
                        'MEIDO_HERDR_AGENT_ARGS': json.dumps(arguments, ensure_ascii=False),
                        'HERDR_BIN_PATH': str(executable), 'NO_COLOR': '1', 'PYTHONUTF8': '1'})
    return environment


class HerdrClient:
    """One owned foreground Herdr server and bounded short-lived clients."""
    def __init__(self, executable: Path, root: Path, *, generation: str, session: str,
                 agent_kind: str, agent_executable: Path, agent_arguments: list[str], deadline: float, resource_journal=None):
        self.executable, self.root = executable.resolve(), root.resolve()
        self.generation, self.session, self.deadline = generation, session, deadline
        self.env = owned_environment(self.executable, self.root, generation=generation, session=session,
                                     kind=agent_kind, agent_executable=agent_executable, arguments=agent_arguments)
        self._lock = threading.RLock()
        self._close_lock = threading.RLock()
        self._cancelled = False
        self._server: OwnedProcess | None = None
        self._clients: set[OwnedProcess] = set()
        self.cleanup_state = 'not_started'
        self._journal = resource_journal
        self._agent: str | None = None
        self._graceful_requested = False

    def _job(self, suffix):
        return 'Local\\Meido.Coding.' + self.generation + '.' + suffix

    @property
    def server_pid(self):
        return self._server.pid if self._server is not None else None

    def _remaining(self, maximum=15):
        remaining = min(maximum, self.deadline - time.monotonic())
        if remaining <= 0 or self._cancelled:
            raise TimeoutError('owned Herdr attempt has ended')
        return remaining

    def _invoke(self, arguments, *, maximum=15) -> bytes:
        with self._lock:
            remaining = self._remaining(maximum)
            client = OwnedProcess([str(self.executable), '--session', self.session, *arguments],
                                  self.root, self.env, output_bytes=65536,
                                  job_name=self._job('client-' + uuid.uuid4().hex), journal=self._journal)
            self._clients.add(client)
        try:
            result = client.communicate(timeout=remaining)
            if result.returncode != 0:
                # stderr and complete argv may contain credentials/terminal
                # text. Only host-defined error codes reach ordinary callers.
                code = 'command_failed'
                for payload in (result.stdout, result.stderr):
                    try:
                        error = json.loads(payload).get('error', {})
                        candidate = error.get('code', '')
                        if isinstance(candidate, str) and re.fullmatch('[a-z_]{1,80}', candidate):
                            code = candidate
                    except (ValueError, AttributeError, UnicodeDecodeError):
                        pass
                raise HerdrProtocolError(code)
            return result.stdout
        finally:
            with self._lock:
                if client.cleanup_state == 'complete':
                    self._clients.discard(client)

    def start(self) -> dict:
        with self._lock:
            self._remaining()
            if self._server is not None:
                raise HerdrProtocolError('server_already_started')
            self._server = OwnedProcess([str(self.executable), '--session', self.session, 'server'], self.root, self.env,
                job_name=self._job('server'), journal=self._journal)
            self.cleanup_state = 'running'
        startup = min(self.deadline, time.monotonic() + 15)
        while time.monotonic() < startup:
            if self._server.process.poll() is not None:
                raise HerdrProtocolError('server_exited')
            try:
                response = json.loads(self._invoke(['owned-status'], maximum=2))
                status = response['result']
                if status['protocol'] != 1 or status['generation'] != self.generation or status['pid'] != self.server_pid:
                    raise HerdrProtocolError('server_identity_mismatch')
                return status
            except (HerdrProtocolError, json.JSONDecodeError, KeyError):
                time.sleep(.05)
        raise TimeoutError('owned Herdr server did not become ready')

    def call(self, arguments: list[str]) -> dict:
        try:
            value = json.loads(self._invoke(arguments))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HerdrProtocolError('invalid_json') from error
        if not isinstance(value, dict) or 'error' in value or value.get('ok') is False:
            raise HerdrProtocolError('request_rejected')
        result = value.get('result')
        if not isinstance(result, dict):
            raise HerdrProtocolError('invalid_result')
        return result

    def launch_agent(self, name: str, pane: str, kind: str, arguments: list[str]) -> dict:
        self._agent = name
        self.call(['agent', 'start', name, '--kind', kind, '--pane', pane, '--timeout', '10000', '--', *arguments])
        deadline = min(self.deadline, time.monotonic() + 10)
        while time.monotonic() < deadline:
            agent = self.observe_agent(name)
            if agent.get('pane_id') != pane or agent.get('agent') != kind:
                raise HerdrProtocolError('agent_identity_mismatch')
            native = agent.get('agent_session')
            if isinstance(native, dict) and native.get('source') == 'herdr:' + kind and native.get('agent') == kind and native.get('value'):
                try:
                    if str(uuid.UUID(native['value'])) != native['value']:
                        raise ValueError()
                except (ValueError, AttributeError, TypeError):
                    raise HerdrProtocolError('native_session_id_invalid') from None
                return agent
            time.sleep(.05)
        raise HerdrProtocolError('native_session_unavailable')

    def install_integration(self, kind: str):
        if kind not in {'codex', 'claude'}:
            raise ValueError('unsupported fixed Herdr integration')
        self._invoke(['integration', 'install', kind])

    def observe_agent(self, name: str) -> dict:
        agent = self.call(['agent', 'get', name]).get('agent')
        if not isinstance(agent, dict):
            raise HerdrProtocolError('agent_unavailable')
        return agent

    def prompt(self, name: str, text: str) -> dict:
        try:
            return self.call(['agent', 'prompt', name, text, '--wait', '--timeout', '5000'])
        except HerdrProtocolError as error:
            if error.code not in {'timeout', 'prompt_stalled'}:
                raise
            # Herdr may have delivered the complete prompt before its wait
            # acknowledgement was lost. Observe this same agent/result; this
            # method never sends the prompt for a second time.
            return {'dispatch': 'unknown', 'errorCode': error.code}

    def close(self):
        with self._close_lock:
            self._close_owned()

    def _close_owned(self):
        with self._lock:
            self._cancelled = True
            self.cleanup_state = 'stopping'
            graceful = (self._server is not None and self._server.process.poll() is None
                        and self._agent is not None and not self._graceful_requested)
            self._graceful_requested = True
        if graceful:
            try:
                # Closing may begin after the execution deadline. This fixed
                # Ctrl-C operation only stops the owned agent; it cannot submit
                # another model prompt and is bounded by the cleanup reserve.
                stop = OwnedProcess([str(self.executable), '--session', self.session,
                    'agent', 'send-keys', self._agent, 'C-c'], self.root, self.env,
                    output_bytes=65536, job_name=self._job('stop-' + uuid.uuid4().hex), journal=self._journal)
                with self._lock:
                    self._clients.add(stop)
                stop.communicate(timeout=5)
            except (OSError, TimeoutError, RuntimeError, ValueError):
                pass  # Escalate to the owned Job and prove its actual exit.
        with self._lock:
            owned = [*self._clients, *([self._server] if self._server is not None else [])]
        failures = []
        for process in owned:
            try:
                process.close()
            except BaseException as error:
                failures.append(error)
        if failures:
            self.cleanup_state = 'cleanup_failed'
            raise CleanupFailed('owned Herdr resources did not all exit') from failures[0]
        with self._lock:
            self._clients.clear()
            self.cleanup_state = 'complete'
