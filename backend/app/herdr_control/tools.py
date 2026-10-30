"""Four existing-agent tools, adapted from Shiori-Agent 71c8b0e (MIT)."""
import json
from collections.abc import Callable, Mapping
from typing import Any

from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from .cli import CommandError
from .commands import ReportTarget
from .service import HerdrControl, UNCONFIRMED_CODES, require_pane_id

HERDR_AGENT_TOOL_NAMES = frozenset({'herdr_list_agents', 'herdr_read_agent', 'herdr_prompt_agent', 'herdr_send_keys'})
GUIDE = ('操作 Herdr 中已有的 agent 请用 herdr_list_agents / herdr_read_agent / '
         'herdr_prompt_agent / herdr_send_keys，不要用 shell 执行 herdr 命令；'
         '要在新的独立 worktree 开发并完成本地验证，用 herdr_start_task；发布需主人另外批准。')
REPORT_NOTE = '你已接手该 agent：它停下、卡住、关闭或追踪超时时，结果会回报给你和本聊天，不需要轮询。'
HANDOVER_RULE = ('收到该 agent 的回报后，仍是它接手角色的你可以直接用本工具回应，回报继续发往原聊天；'
                 '其他角色只能在用户发起的回合里操作。')
DESCRIPTIONS = {
    'list': '列出 Herdr session 中的全部 coding agent（含用户手动打开的 Claude Code / Codex），按 workspace 分组，附 pane_id、状态、终端标题、工作目录和接手角色。',
    'read': '读取 Herdr 中指定 agent 的状态、终端标题、工作目录、终端最近输出与指令记录。',
    'prompt': '把文字作为 prompt 直接发给 Herdr 中已有的 agent，立即执行；你成为它的接手角色，它停下、卡住、关闭或追踪超时时回报给你。超时或未确认送达时不要重发。',
    'keys': '向 Herdr 中已有的 agent 按逻辑按键（如 enter、esc、ctrl+c、up、down），用于回答确认提示或中断；你成为它的接手角色，结果同样回报给你。',
}


class AgentTool:
    def __init__(self, action: str, service: Callable[[], HerdrControl], *, role_id: str, role_name: str,
                 enabled: Callable[[], bool], capture: Callable[[ToolContext], ReportTarget],
                 report_target: ReportTarget | None = None):
        self.action, self.service, self.role_id, self.role_name = action, service, role_id, role_name
        self.enabled, self.capture, self.report_target = enabled, capture, report_target
        pane = {'type': 'string', 'description': 'agent 的 pane_id，来自 herdr_list_agents'}
        table: dict[str, dict[str, Any]] = {'list': {}, 'read': {'pane_id': pane},
                 'prompt': {'pane_id': pane, 'text': {'type': 'string', 'description': '发给 agent 的完整 prompt'}},
                 'keys': {'pane_id': pane, 'keys': {'type': 'array', 'items': {'type': 'string'},
                           'description': '按顺序按下的 Herdr 逻辑按键名，如 ["enter"]、["ctrl+c"]'}}}
        name = {'list': 'herdr_list_agents', 'read': 'herdr_read_agent', 'prompt': 'herdr_prompt_agent', 'keys': 'herdr_send_keys'}[action]
        properties = table[action]
        self.definition = ToolDefinition(name, DESCRIPTIONS[action] + (HANDOVER_RULE if action in {'prompt', 'keys'} else '') + GUIDE,
            {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False},
            source='builtin:herdr', exposure='deferred', risk='read_only' if action in {'list', 'read'} else 'external',
            # At most six CLI waits (version/session, operation, failure check)
            # at the schema's 300-second maximum, plus cleanup; each CLI keeps
            # its configured, usually 45-second, timeout.
            search_hint='Herdr agent 终端 Claude Code Codex 发消息 按键 中断 推动', timeout_seconds=1805, output_limit=24000)

    async def execute(self, arguments: Mapping[str, object], context: ToolContext,
                      on_update: Callable[[AgentToolResult], None] | None = None) -> AgentToolResult:
        del on_update
        def admitted() -> bool:
            return self.enabled() and context.role_id == self.role_id and not context.signal.is_cancelled()
        try:
            ToolRegistry.validate_definition(self.definition, arguments)
            if not admitted():
                raise PermissionError('Herdr 插件或角色来源已失效')
            service = self.service()
            if self.action == 'list':
                result = await service.list_agents()
            elif self.action == 'read':
                result = await service.read_agent(arguments.get('pane_id'))
            else:
                pane = require_pane_id(arguments.get('pane_id'))
                if self.report_target is not None:
                    handover = service.watcher.handover(service.config.session, pane)
                    if (handover is None or handover.role_id != self.role_id
                            or context.session_key != self.report_target.thread_id):
                        raise PermissionError('只有该 agent 的接手角色可回应原聊天中的回报')
                    target = handover.target
                else:
                    target = self.capture(context)
                def send_admitted() -> bool:
                    if not admitted():
                        return False
                    if self.report_target is not None:
                        current = service.watcher.handover(service.config.session, pane)
                        return current is not None and current.role_id == self.role_id and current.target == target
                    try:
                        return self.capture(context) == target
                    except (PermissionError, ValueError):
                        return False
                command = await service.send(self.action, pane, arguments.get('text' if self.action == 'prompt' else 'keys'),
                                             self.role_name, target, admitted=send_admitted)
                result = {'command': command.as_dict(), 'note': REPORT_NOTE}
            result = {'ok': True, **result}
            return AgentToolResult(json.dumps(result, ensure_ascii=False), result)
        except (CommandError, PermissionError, ValueError, OSError, KeyError, TypeError) as error:
            code = (error.code if isinstance(error, CommandError) else 'not_permitted' if isinstance(error, PermissionError)
                    else 'executable_not_found' if isinstance(error, FileNotFoundError) else 'invalid_request')
            result = {'ok': False, 'error': {'code': code, 'message': str(error)}}
            if code in UNCONFIRMED_CODES:
                result['note'] = REPORT_NOTE
            return AgentToolResult(json.dumps(result, ensure_ascii=False), result, True)


def build_agent_tools(service: Callable[[], HerdrControl], *, role_id: str, role_name: str,
                      enabled: Callable[[], bool], capture: Callable[[ToolContext], ReportTarget],
                      report_target: ReportTarget | None = None) -> tuple[AgentTool, ...]:
    return tuple(AgentTool(action, service, role_id=role_id, role_name=role_name, enabled=enabled,
                           capture=capture, report_target=report_target) for action in ('list', 'read', 'prompt', 'keys'))
