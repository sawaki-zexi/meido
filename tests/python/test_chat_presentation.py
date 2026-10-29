import json

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage
from backend.app.models import RoleInput, RoleProfile
from tests.python.test_api import configure_session_api, SuccessAdapter


def test_chat_stream_exposes_model_thinking_and_last_request_usage(tmp_path, monkeypatch):
    from backend.app.ai.model_types import (ModelThinkingDelta, ModelThinkingBlock, ModelTextBlock,
        ModelTextDelta, ModelStreamDone, ModelAssistantMessage, ModelResponseMetadata, ModelUsage)

    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools):
            yield ModelThinkingDelta('整理资料')
            yield ModelTextDelta('你好')
            yield ModelStreamDone(ModelAssistantMessage(
                (ModelThinkingBlock('整理资料', 'private-signature'), ModelTextBlock('你好')),
                ModelResponseMetadata(stop_reason='stop', usage=ModelUsage(input_tokens=1200, output_tokens=30)),
            ))

    role, _, client = configure_session_api(tmp_path, monkeypatch, Adapter())
    with client:
        response = client.post(f'/api/roles/{role.id}/messages', json={'content': '你好'})
        history = client.get(f'/api/roles/{role.id}/session').json()
    assert response.status_code == 200
    assert 'event: assistant_thinking_delta' in response.text
    reply = history['messages'][-1]
    assert reply['metadata']['process'][0]['text'] == '整理资料'
    assert reply['metadata']['contextUsage']['inputTokens'] == 1200
    assert reply['metadata']['contextUsage']['source'] == 'actual'
    assert 'private-signature' not in response.text


def test_redacted_thinking_stays_private_and_other_runs_do_not_mix(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, SuccessAdapter())
    key = sessions.open_role_session(role.id).sessionKey
    sessions.append_agent_message(key, AssistantMessage('第一轮', metadata={
        'thinking': [{'text': '不可公开', 'signature': 'opaque'}],
        'content_blocks': [{'type': 'thinking', 'text': '不可公开', 'redacted': True, 'signature': 'opaque'}],
    }), run_id='first')
    sessions.append_agent_message(key, AssistantMessage('第二轮', metadata={'thinking': [{'text': '公开摘要'}]}), run_id='second')
    with client:
        messages = client.get(f'/api/roles/{role.id}/session').json()['messages']
    assert messages[0]['metadata']['process'] == []
    assert messages[1]['metadata']['process'][0]['text'] == '公开摘要'
    assert '不可公开' not in json.dumps(messages, ensure_ascii=False)
    assert 'opaque' not in json.dumps(messages)


def test_session_projects_ordered_reasoning_and_tools_without_private_payloads(tmp_path, monkeypatch):
    configure_session_api(tmp_path, monkeypatch, SuccessAdapter())
    role = main.store.create(RoleInput(name='Alice', profile=RoleProfile(profile='设定')))
    session = main.session_store.open_role_session(role.id)
    key = session.sessionKey
    main.session_store.append_agent_message(key, AssistantMessage(
        '先查找', tool_calls=(ToolCall('c1', 'lookup', {'token': 'private-argument'}),),
        stop_reason='tool_use', metadata={'thinking': [{'text': '查找相关资料', 'signature': 'opaque'}]},
    ), run_id='r1')
    main.session_store.append_agent_message(key, ToolResultMessage('c1', 'lookup', '找到两条', details={'status': 'succeeded'}), run_id='r1')
    final = main.session_store.append_agent_message(key, AssistantMessage('这是回复', metadata={
        'thinking': [{'text': '整理结果', 'signature': 'opaque'}],
        'content_blocks': [{'type': 'thinking', 'text': '整理结果', 'signature': 'opaque'}],
    }), run_id='r1')[0]
    with TestClient(main.app) as client:
        data = client.get(f'/api/roles/{role.id}/session').json()
    reply = next(item for item in data['messages'] if item['id'] == final.id)
    assert [(item['type'], item.get('text', item.get('name'))) for item in reply['metadata']['process']] == [
        ('thinking', '查找相关资料'), ('text', '先查找'), ('tool', 'lookup'), ('thinking', '整理结果'),
    ]
    assert reply['metadata']['process'][2]['result'] == '找到两条'
    assert 'opaque' not in json.dumps(data)
    assert 'private-argument' not in json.dumps(data)
    assert len(data['messages']) == 1


def test_previews_skip_tools_and_unfinished_messages_and_support_attachments(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, SuccessAdapter())
    key = sessions.open_role_session(role.id).sessionKey
    sessions.append_message(key, 'assistant', '最近的回复', 'completed')
    sessions.append_message(key, 'assistant', '未完成的内容', 'failed')
    sessions.append_agent_message(key, AssistantMessage('工具内部文字', tool_calls=(ToolCall('c1', 'lookup', {}),)))
    with client:
        preview = client.get('/api/chat/previews').json()['previews'][role.id]
        assert preview['content'] == '最近的回复'
        sessions.append_message(key, 'user', '', 'completed', metadata={'attachments': [{'id': 'f1', 'kind': 'file', 'path': 'private-path'}]})
        preview = client.get('/api/chat/previews').json()['previews'][role.id]
        assert preview['role'] == 'user'
        assert preview['hasFiles'] and not preview['hasImages']
        assert 'private-path' not in json.dumps(preview)
