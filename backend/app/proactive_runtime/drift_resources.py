"""Finite read-only resources; model paths never select host filesystem roots."""
import json

from ..agent_runtime.skills import SkillRegistry
from ..agent_runtime.message_history_tools import MessageHistoryTool, ConversationHistory
from .drift_skills import BUILTIN_ROOT

RESOURCES = {'role-profile': '当前角色设定', 'proactive-context': '当前角色主动上下文',
    'builtin-create-drift-skill': '内建长期技能作者说明', 'builtin-meme-manage': '内建表情包维护说明'}


def freeze(app, origin):
    frozen = app.frozen_bindings(origin)
    result = {'role-profile': json.dumps(frozen.get('role', {}).get('profile', {}), ensure_ascii=False),
        'proactive-context': frozen.get('context', {}).get('text', '')}
    registry = SkillRegistry.discover(BUILTIN_ROOT, source='builtin')
    for identifier in ('create-drift-skill', 'meme-manage'):
        skill = next((item for item in registry.skills if item.skill_id == identifier), None)
        if skill is not None:
            result['builtin-' + identifier] = skill.materialize().body
    return result


class ScopedHistory(ConversationHistory):
    """Explicit autonomous desktop grant, without inventing an owner chat identity."""
    def __init__(self, turn):
        self.turn = turn

    def validate(self, context):
        context.signal.raise_if_cancelled()
        if not self.turn.file_authorized(context):
            raise PermissionError('Drift history scope was sealed')

    def read(self, context):
        self.validate(context)
        with self.turn.app._transaction() as db:
            rows = db.execute("""SELECT message_id,session_key,sequence,role,content,status,created_at,message_type,
                tool_call_id,tool_name,tool_arguments_json,tool_result_json,is_error,run_id,metadata_json
                FROM messages WHERE session_key=? AND role IN ('user','assistant') AND status='completed'
                AND message_type='text' AND json_valid(COALESCE(metadata_json,'{}'))
                AND COALESCE(json_extract(metadata_json,'$.internalEvent'),'')=''
                ORDER BY sequence DESC LIMIT 200""", (f'role:{self.turn.origin.role_id}',)).fetchall()
        return [self.turn.app.sessions._message(row) for row in reversed(rows)]


def history_tools(turn):
    history = ScopedHistory(turn)
    return {tool.definition.name: tool for tool in (MessageHistoryTool(history, search=True), MessageHistoryTool(history, search=False))}
