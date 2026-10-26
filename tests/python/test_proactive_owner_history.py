import asyncio
import json

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.conversation_history import ConversationHistoryView
from backend.app.mcp_runtime import McpHost
from backend.app.multichannel import MultichannelStore
from backend.app.proactive_runtime import ProactiveApplication
from backend.app.proactive_runtime.config import AutonomyConfig
from backend.app.session_store import SessionStore
from backend.app.task_runtime import TaskRuntime
from test_background_shell_api import api as shell_api
from test_spawn_api import api
from test_owner_history_view_api import paired_thread
from test_proactive_mcp import source
from test_proactive_snapshot import grant_for
from test_proactive_drift_delivery import prepare, settled

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_active_decision_recent_chat_shares_admission_history_without_later_binding(source, tmp_path):
    _, store, *_ = source
    sessions, tasks = SessionStore(tmp_path / 'sessions.db'), TaskRuntime(tmp_path / 'tasks')
    channels = MultichannelStore(sessions.database_path, sessions)
    thread = paired_thread(channels, 'role')
    sessions.append_message(thread.session_key, 'user', '合法主人私聊原文')
    requests = []
    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            requests.append(messages)
            if len(requests) == 1:
                sessions.append_message('role:role', 'user', '准入之后的未来桌面消息')
                name, arguments = 'get_recent_chat', {'n': 20}
            else:
                result = json.loads(messages[-1].content[0].text)
                assert '合法主人私聊原文' in str(result)
                assert '准入之后的未来桌面消息' not in str(result)
                name, arguments = 'finish_turn', {'decision': 'skip', 'reason': 'other', 'note': 'history verified'}
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock(str(len(requests)), name, arguments),)))
    now, host = [1780000000.0], McpHost(store)
    app = ProactiveApplication(sessions, tasks, clock=lambda: now[0], role_getter=lambda key: {'id': key},
        model_getter=lambda key: {'id': key}, mcp_host=host, adapter_factory=lambda config: Adapter())
    app.history_factory = lambda role_id, snapshot=None: ConversationHistoryView(channels, role_id=role_id, snapshot=snapshot)
    app.configure('role', AutonomyConfig(activeDecisionEnabled=True, decisionModelConfigurationId='fixture',
        consentRevision=1, sourceGrants=[grant_for(source)]), expected_revision=0)
    async def scenario():
        try:
            now[0] += 1800
            state = await settled(app, 'role')
            assert state['runs'][0]['status'] == 'skipped', state
            assert len(requests) == 2
        finally:
            await app.close(); await host.close()
    try:
        asyncio.run(scenario())
    finally:
        tasks.close()


@pytest.mark.parametrize('revoke', ['none', 'tool', 'transport'])
def test_drift_history_tools_share_frozen_private_scope_and_recheck_before_model(api, monkeypatch, revoke):
    client, _, sessions, _ = api
    role, _, now = prepare(client, monkeypatch, extra_tools=('fetch_messages',))
    thread = paired_thread(main.multichannel_store, role)
    original = sessions.append_message(thread.session_key, 'user', 'Drift 可以核验的私聊原文')
    requests = []
    class Reader:
        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            if len(requests) == 2 and revoke == 'transport':
                await asyncio.sleep(0)
                main.multichannel_store.unbind_identity(main.multichannel_store.list_identities()[0]['identityId'])
                if cancel_check():
                    raise asyncio.CancelledError()
            requests.append(messages)
            if len(requests) == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif len(requests) == 2:
                sessions.append_message(thread.session_key, 'user', 'Drift 启动后的未来消息')
                name, arguments = 'fetch_messages', {'source_ref': original.id, 'context': 10}
                if revoke == 'tool':
                    main.multichannel_store.unbind_identity(main.multichannel_store.list_identities()[0]['identityId'])
            else:
                result = json.loads(json.loads(messages[-1].content[0].text)['content'])
                assert result['matched_count'] == 1 and result['messages'][0]['content'] == original.content
                assert 'Drift 启动后的未来消息' not in str(result)
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '核验当前授权',
                    'next_action': '继续阅读', 'message_result': 'silent'}
            yield ModelStreamDone(ModelAssistantMessage((ModelToolCallBlock(f'history-{len(requests)}', name, arguments),)))
    monkeypatch.setattr(main, 'model_adapter', Reader())
    try:
        now[0] += 1800
        state = asyncio.run(settled(main._proactive, role))
        assert len(requests) == (3 if revoke == 'none' else 2)
        if revoke == 'none':
            assert state['runs'][0]['status'] == 'finished_silent', state
    finally:
        asyncio.run(main._proactive.close())
