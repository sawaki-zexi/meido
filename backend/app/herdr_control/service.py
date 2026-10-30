"""Session agent reads and one shared, never-replayed input path.

Adapted from Shiori-Agent 71c8b0e agent_control/agents/sessions (MIT).
"""
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from .cli import CommandError, HerdrCli
from .commands import AgentCommand, AgentCommandLog, CommandSource, Handover, ReportTarget
from .config import HerdrConfig
from .watch import AgentWatcher, state_seq

UNCONFIRMED_CODES = frozenset({'timeout', 'agent_prompt_stalled', 'invalid_response'})
REFUSALS = {'agent_not_found': 'Agent 已关闭，未发送。',
            'agent_blocked': 'Agent 正在等待确认或回答，未发送；请先在 Herdr 中处理。'}
T = TypeVar('T')


def require_pane_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError('缺少 agent 的 pane_id')
    return value


def summary(agent: dict[str, Any], task_id: str | None) -> dict[str, Any]:
    return {'pane_id': str(agent.get('pane_id', '')), 'workspace_id': str(agent.get('workspace_id', '')),
            'agent': str(agent.get('agent', '')), 'status': str(agent.get('agent_status', 'unknown')),
            'title': str(agent.get('terminal_title_stripped') or agent.get('terminal_title') or ''),
            'cwd': str(agent.get('cwd', '')), 'task_id': task_id}


class SessionGate:
    def __init__(self, cli: HerdrCli):
        self.cli = cli
        self.verified = False
        self.active = True

    async def run(self, operation: Callable[[], Awaitable[T]], *, keep: frozenset[str] = frozenset()) -> T:
        if not self.active:
            raise PermissionError('Herdr 配置已替换，请重新调用当前工具')
        if not self.verified:
            await self.cli.require_supported_version()
            self.verified = True
        if not self.active:
            raise PermissionError('Herdr 配置已替换，请重新调用当前工具')
        try:
            return await operation()
        except CommandError as error:
            if error.code in keep:
                raise
            self.verified = False
            await self.cli.require_supported_version()
            self.verified = True
            raise


class HerdrControl:
    def __init__(self, config: HerdrConfig, directory: Path, *,
                 task_for: Callable[[str, str], tuple[str, bool] | None] = lambda session, pane: None):
        self.directory, self.task_for = directory, task_for
        self.gate = SessionGate(HerdrCli(config, directory))
        self.log = AgentCommandLog()
        self.watcher = AgentWatcher(directory)
        self.closed = False

    @property
    def config(self) -> HerdrConfig:
        return self.gate.cli.config

    def reconfigure(self, config: HerdrConfig) -> None:
        self.gate.active = False
        self.gate = SessionGate(HerdrCli(config, self.directory))
        if self.closed:
            self.watcher = AgentWatcher(self.directory)
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.gate.active = False
        await self.watcher.close()

    def refusal(self, pane_id: str, session: str | None = None) -> str:
        task = self.task_for(session or self.config.session, pane_id)
        return ('该 Agent 正在执行插件任务；要停下它请用 herdr_cancel_task 取消任务 ' + task[0]
                if task is not None and task[1] else '')

    def _summary(self, agent: dict[str, Any], session: str) -> dict[str, Any]:
        pane_id = str(agent.get('pane_id', ''))
        task = self.task_for(session, pane_id)
        return summary(agent, task[0] if task else None)

    async def list_agents(self) -> dict[str, Any]:
        gate, session = self.gate, self.config.session
        async def read() -> dict[str, Any]:
            agents = (await gate.cli.call('agent', 'list'))['agents']
            workspaces = (await gate.cli.call('workspace', 'list'))['workspaces']
            labels = {str(item['workspace_id']): str(item.get('label') or item['workspace_id'])
                      for item in sorted(workspaces, key=lambda item: item.get('number', 0))}
            groups: dict[str, list[dict[str, Any]]] = {key: [] for key in labels}
            for agent in agents:
                item = self._summary(agent, session)
                item['handler'] = self.watcher.handler(session, item['pane_id'])
                groups.setdefault(item['workspace_id'], []).append(item)
            return {'session': session, 'workspaces': [
                {'workspace_id': key, 'label': labels.get(key, key), 'agents': members}
                for key, members in groups.items() if members]}
        return await gate.run(read)

    async def read_agent(self, pane_id: object) -> dict[str, Any]:
        pane = require_pane_id(pane_id)
        gate, session = self.gate, self.config.session
        async def read() -> dict[str, Any]:
            agent = (await gate.cli.call('agent', 'get', pane))['agent']
            output = await gate.cli.read(pane, source='recent-unwrapped'
                                        if agent.get('agent_status') in {'idle', 'done'} else 'visible')
            return {'session': session, 'agent': self._summary(agent, session),
                    'handler': self.watcher.handler(session, pane), 'output': output[-4000:],
                    'commands': self.log.records(session, pane)[-20:], 'can_prompt': not self.refusal(pane, session)}
        return await gate.run(read, keep=frozenset({'agent_not_found'}))

    async def send(self, kind: str, pane_id: object, content: object, role_name: str,
                   target: ReportTarget, *, admitted: Callable[[], bool] = lambda: True) -> AgentCommand:
        pane = require_pane_id(pane_id)
        gate, config = self.gate, self.config
        if kind == 'prompt':
            if not isinstance(content, str) or not content.strip():
                raise ValueError('消息不能为空')
            command = AgentCommand('prompt', content, CommandSource('role', target.role_id, role_name))
            args = ('agent', 'prompt', pane, content, '--wait', '--until', 'working', '--until', 'blocked', '--timeout', '10000')
            timeout = max(config.cli_timeout_seconds, 15)
        else:
            if not isinstance(content, list) or not content or not all(isinstance(key, str) and key for key in content):
                raise ValueError('按键不能为空')
            command = AgentCommand('keys', ' '.join(content), CommandSource('role', target.role_id, role_name))
            args = ('agent', 'send-keys', pane, *content)
            timeout = config.cli_timeout_seconds
        handed = False
        baseline = None

        def require_free() -> None:
            if self.closed or not gate.active or not admitted():
                raise PermissionError('Herdr 输入来源或配置已失效')
            reason = self.refusal(pane, config.session)
            if reason:
                raise ValueError(reason)

        async def submit() -> dict[str, Any]:
            nonlocal handed, baseline
            if kind == 'keys':
                try:
                    baseline = state_seq((await gate.cli.call('agent', 'get', pane))['agent'])
                except CommandError:
                    baseline = None
            require_free()
            self.log.add(config.session, pane, command)
            handed = True
            self.watcher.take_over(config.session, pane, Handover(role_name, target))
            return await gate.cli.call(*args, timeout=timeout)

        def follow(working: bool = False) -> None:
            self.watcher.follow(config, pane, command, target, working=working, baseline=baseline)

        require_free()
        try:
            response = await gate.run(submit, keep=UNCONFIRMED_CODES | frozenset(REFUSALS))
        except CommandError as error:
            if handed:
                if error.code in UNCONFIRMED_CODES:
                    command.settle('unconfirmed', '未确认 Agent 收到；输入可能已经送达，未自动重发。')
                    follow()
                else:
                    command.settle('failed', REFUSALS.get(error.code, str(error)))
                raise CommandError(command.error, code=error.code) from error
            raise
        except asyncio.CancelledError:
            if handed:
                command.settle('unconfirmed', 'Meido 已停止等待；输入可能已经送达。')
                follow()
            raise
        except Exception as error:
            if handed:
                command.settle('failed', str(error))
            raise
        command.settle('sent')
        agent = response.get('agent')
        follow(kind == 'prompt' and isinstance(agent, dict) and agent.get('agent_status') == 'working')
        return command
