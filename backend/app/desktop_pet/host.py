"""The original application owns the process, Job, pipes and in-flight action."""
from collections import deque
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

from ..browser_use.process import WindowsJob


FRAME_BYTES = 16384
PROJECT = Path(__file__).resolve().parents[3]


def installation():
    release = PROJECT / 'desktop-pet/release/MeidoPet.exe'
    if release.is_file():
        return [str(release)]
    executable = PROJECT / 'node_modules/electron/dist/electron.exe'
    entry = PROJECT / 'desktop-pet/dist/main.cjs'
    return [str(executable), str(entry)] if executable.is_file() and entry.is_file() else None


class PetHost:
    def __init__(self, root: Path, *, on_event):
        self.root, self.on_event = root, on_event
        self.closed = False
        self.process = self.job = None
        self._threads = []
        self._condition = threading.Condition(threading.RLock())
        self._nonce = secrets.token_hex(24)
        self._binding = None
        self._status, self._reason = 'stopped', ''
        self._inflight = None
        self._results = {}
        self._stderr = deque(maxlen=16)
        self._geometry = None

    def snapshot(self):
        with self._condition:
            status = self._status
            if self.process is not None and self.process.poll() is not None and status != 'stopped':
                status = 'disconnected'
            return {'status': status, 'reason': self._reason, 'pid': self.process.pid if self.process else None,
                'inflightActionId': self._inflight, 'window': self._geometry}

    def _write(self, message):
        raw = json.dumps({**message, 'nonce': self._nonce, **{key: self._binding[key]
            for key in ('roleId', 'packageId', 'generation')}}, ensure_ascii=False).encode('utf-8') + b'\n'
        if len(raw) > FRAME_BYTES or self.process is None or self.process.stdin is None:
            raise RuntimeError('桌宠桥不可用')
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def start(self, binding):
        if sys.platform != 'win32':
            raise ValueError('桌宠宿主当前仅支持 Windows')
        command = installation()
        if command is None:
            raise ValueError('桌宠宿主尚未构建，请运行 npm run build:pet')
        self._binding = dict(binding)
        self.root.mkdir(parents=True, exist_ok=True)
        # This private profile is never the user's browser/Electron profile.
        env = {key: value for key, value in os.environ.items() if key.upper() in {'SYSTEMROOT', 'WINDIR'}}
        temporary = self.root / 'tmp'
        temporary.mkdir(exist_ok=True)
        env.update({'TEMP': str(temporary), 'TMP': str(temporary), 'PATH': 'C:\\Windows\\System32'})
        with self._condition:
            self._status = 'starting'
        self.process = subprocess.Popen([*command, '--runtime-root', str(self.root)], cwd=PROJECT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004)  # CREATE_SUSPENDED
        try:
            self.job = WindowsJob(self.process.pid, resume=True)
            for stream, function in ((self.process.stdout, self._read), (self.process.stderr, self._read_error)):
                thread = threading.Thread(target=function, args=(stream,), name='meido-pet-pipe', daemon=True)
                self._threads.append(thread)
                thread.start()
            with self._condition:
                self._write({'type': 'bind', 'binding': binding})
                deadline = time.monotonic() + 15
                while self._status == 'starting' and self.process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('桌宠 renderer 未就绪')
                    self._condition.wait(min(remaining, .25))
                if self._status != 'ready':
                    raise RuntimeError(self._reason or '桌宠宿主启动失败')
        except BaseException:
            self.close()
            raise

    def _read_error(self, stream):
        while True:
            raw = stream.read(1024)
            if not raw:
                return
            self._stderr.append(raw.decode('utf-8', errors='replace')[-1024:])

    def _read(self, stream):
        empty_lines = 0
        try:
            while True:
                raw = stream.readline(FRAME_BYTES + 1)
                if not raw:
                    break
                if len(raw) > FRAME_BYTES or not raw.endswith(b'\n'):
                    raise ValueError('桌宠桥帧超额')
                # Windows Electron's GUI launcher may emit an initial CRLF.
                if not raw.strip() and empty_lines < 4:
                    empty_lines += 1
                    continue
                event = json.loads(raw)
                if (not isinstance(event, dict) or event.get('nonce') != self._nonce
                        or any(event.get(key) != self._binding[key] for key in ('roleId', 'packageId', 'generation'))):
                    raise ValueError('桌宠桥身份不匹配')
                kind = event.get('type')
                if kind not in {'ready', 'accepted', 'started', 'finished', 'error', 'closed', 'window'}:
                    raise ValueError('桌宠桥事件无效')
                with self._condition:
                    if kind == 'ready':
                        self._status = 'ready'
                    elif kind in {'closed', 'error'}:
                        self._status = 'disconnected'
                        self._reason = str(event.get('reason', '桌宠窗口已关闭'))[:256]
                    if 'window' in event:
                        self._geometry = event['window']
                    identifier = event.get('actionId')
                    if identifier is not None:
                        if identifier != self._inflight:
                            raise ValueError('桌宠动作回执归属不匹配')
                        self._results[identifier] = event
                        if kind in {'finished', 'error'}:
                            self._inflight = None
                    self.on_event({key: value for key, value in event.items() if key != 'nonce'})
                    self._condition.notify_all()
        except (ValueError, OSError, UnicodeError) as error:
            with self._condition:
                self._reason = str(error)[:256]
        finally:
            with self._condition:
                if self._status != 'stopped':
                    self._status = 'disconnected'
                self._condition.notify_all()

    def action(self, identifier, arguments):
        with self._condition:
            if self._status != 'ready' or self.process.poll() is not None or self._inflight:
                raise ValueError('桌宠未就绪或仍有未决动作')
            self._results.clear()
            self._inflight = identifier
            try:
                self._write({'type': 'action', 'actionId': identifier, 'arguments': arguments})
            except (OSError, ValueError):
                # Dispatch uncertainty is retained, never submitted again.
                self._status = 'disconnected'
                raise
            deadline = time.monotonic() + 12
            while self._inflight == identifier and self._status == 'ready':
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            result = self._results.get(identifier)
            if result and result['type'] in {'finished', 'error'}:
                return {key: value for key, value in result.items() if key != 'nonce'}
            return {'type': 'unknown', 'actionId': identifier, 'roleId': self._binding['roleId'],
                'lastReceipt': result.get('type') if result else None, 'reason': '未确认完成；不会自动重发'}

    def close(self):
        with self._condition:
            if self.closed:
                return
            self._status = 'cleanup_pending'
        # Keep every handle until the original process tree and readers terminate.
        if self.process is not None and self.process.poll() is None and self._binding is not None:
            try:
                self._write({'type': 'shutdown'})
                self.process.wait(timeout=2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass  # The original Job still owns and must reap the whole tree.
        if self.job is not None:
            self.job.close()
        elif self.process is not None and self.process.poll() is None:
            self.process.kill()
        if self.process is not None:
            self.process.wait(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
        for thread in self._threads:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError('桌宠原管道尚未退出')
        with self._condition:
            self.closed = True
            self._status = 'stopped'
            self._condition.notify_all()
