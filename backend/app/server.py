"""Meido's Uvicorn entry: close chat owners before waiting for HTTP streams."""
from __future__ import annotations

import argparse
import asyncio
import multiprocessing
import os
import sys
import threading

import uvicorn
from uvicorn.supervisors import ChangeReload
from uvicorn._subprocess import get_subprocess


class Server(uvicorn.Server):
    def __init__(self, config, *, shutdown_signal=None):
        super().__init__(config)
        # Optional launcher-owned control; shared with the standard reload child.
        self.shutdown_signal = shutdown_signal

    def run(self, sockets=None):
        if sys.platform == 'win32':
            # Windows reload defaults to Selector in Uvicorn, which cannot run
            # asyncio subprocess tools. The public Python runner works across
            # our supported Uvicorn versions without changing global policies.
            with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
                runner.run(self.serve(sockets=sockets))
        else:
            super().run(sockets=sockets)

    async def serve(self, sockets=None):
        async def watch_launcher():
            while not self.should_exit:
                if self.shutdown_signal.is_set():
                    self.should_exit = True
                    return
                await asyncio.sleep(.05)

        watcher = asyncio.create_task(watch_launcher()) if self.shutdown_signal is not None else None
        try:
            await super().serve(sockets=sockets)
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    async def shutdown(self, sockets=None):
        # Stop intake first. Existing connections can still receive their final
        # reply while the service cancels/joins its exact owners and tool cleanup.
        for listener in self.servers:
            listener.close()
        application = self.config.loaded_app
        while application is not None:
            callback = getattr(getattr(application, 'state', None), 'before_server_drain', None)
            if callback is not None:
                await callback()
                break
            application = getattr(application, 'app', None)
        await super().shutdown(sockets=sockets)


class Reload(ChangeReload):
    """Keep Uvicorn's file watcher/spawn, drain only its owned worker first."""
    def __init__(self, config, target, sockets, *, shutdown_signal):
        super().__init__(config, target=target, sockets=sockets)
        self.shutdown_signal = shutdown_signal

    def restart(self):
        # Match StatReload's bookkeeping (WatchFilesReload has no mtime cache).
        if hasattr(self, 'mtimes'):
            self.mtimes.clear()
        # A shared signal also works for Windows launches without an interactive
        # console, where CTRL_C_EVENT cannot reliably reach the reload child.
        self.shutdown_signal.set()
        self.process.join()
        if self.should_exit.is_set():
            return
        if sys.platform == 'win32':
            # A shared listener remains associated with the retired worker's
            # IOCP while the parent holds it. Only after that worker has drained,
            # close our own handles and bind a fresh listener on the same port.
            # This entry accepts one TCP listener, including --port 0.
            self.config.port = self.sockets[0].getsockname()[1]
            for listener in self.sockets:
                listener.close()
            self.sockets = [self.config.bind_socket()]
        self.shutdown_signal.clear()
        self.process = get_subprocess(config=self.config, target=self.target, sockets=self.sockets)
        self.process.start()

    def shutdown(self):
        self.shutdown_signal.set()
        super().shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', nargs='?', default='backend.app.main:app')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=4288)
    parser.add_argument('--reload', action='store_true')
    parser.add_argument('--reload-dir', action='append', default=None)
    parser.add_argument('--log-level', default='info')
    parser.add_argument('--shutdown-stdin', action='store_true', help='Owned launcher: stop on shutdown line or pipe EOF')
    args = parser.parse_args()
    config = uvicorn.Config(args.app, host=args.host, port=args.port, reload=args.reload,
        reload_dirs=args.reload_dir, log_level=args.log_level)
    control = multiprocessing.get_context('spawn').Event() if args.shutdown_stdin or config.should_reload else None
    server = Server(config, shutdown_signal=control)
    supervisor = Reload(config, target=server.run, sockets=[config.bind_socket()], shutdown_signal=control) if config.should_reload else None

    if args.shutdown_stdin:
        # This private control pipe belongs to the launcher, never the worker.
        # Spawned workers receive devnull, including at Windows interpreter
        # startup before Uvicorn can replace its own stdin wrapper.
        stdin_fd = os.dup(sys.stdin.fileno())
        with open(os.devnull, 'rb') as detached:
            os.dup2(detached.fileno(), sys.stdin.fileno())
        def receive_stop():
            pending = b''
            while True:
                chunk = os.read(stdin_fd, 4096)
                if not chunk:
                    break
                pending += chunk
                lines = pending.split(b'\n')
                pending = lines.pop()
                if any(line.strip() == b'shutdown' for line in lines):
                    break
                pending = pending[-4096:]
            control.set()
            if supervisor is not None:
                supervisor.should_exit.set()
            os.close(stdin_fd)
        threading.Thread(target=receive_stop, daemon=True, name='meido-launcher-stop').start()
    if supervisor is not None:
        supervisor.run()
    else:
        server.run()


if __name__ == '__main__':
    main()
