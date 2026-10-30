import json

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage
from backend.app.models import RoleInput, RoleProfile
from tests.python.test_api import configure_session_api, SuccessAdapter


def test_chat_stream_exposes_model_thinking_and_last_request_usage(tmp_path, monkeypatch):
    from backend.app.ai.model_types import (ModelThinkingDelta, ModelThinkingBlock, ModelTextBlock,
        ModelTextDelta, ModelStreamDone, ModelAssistantMessage, ModelResponseMetadata, ModelUsage)

    clock = [100.0]
    monkeypatch.setattr(main, 'monotonic', lambda: clock[0])

    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools):
            yield ModelThinkingDelta('整理资料')
            clock[0] = 105.0
            yield ModelTextDelta('你好')
            clock[0] = 109.0
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
    assert reply['metadata']['turnMetrics']['totalTokens'] == 1230
    assert reply['metadata']['turnMetrics']['thinkingDurationMs'] == 5000
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
        '先查找', tool_calls=(ToolCall('c1', 'lookup', {'query': '资料', 'nested': {'token': 'private-argument'}}),
                            ToolCall('c2', 'lookup', {})),
        stop_reason='tool_use', metadata={'thinking': [{'text': '查找相关资料', 'signature': 'opaque'}],
                                        'usage': {'input_tokens': 100, 'output_tokens': 10}},
    ), run_id='r1')
    main.session_store.append_agent_message(key, ToolResultMessage('c1', 'lookup', '找到两条', details={'status': 'succeeded'}), run_id='r1')
    final = main.session_store.append_agent_message(key, AssistantMessage('这是回复', metadata={
        'thinking': [{'text': '整理结果', 'signature': 'opaque'}],
        'content_blocks': [{'type': 'thinking', 'text': '整理结果', 'signature': 'opaque'}],
        'usage': {'input_tokens': 200, 'output_tokens': 20},
    }), run_id='r1')[0]
    with TestClient(main.app) as client:
        data = client.get(f'/api/roles/{role.id}/session').json()
    reply = next(item for item in data['messages'] if item['id'] == final.id)
    assert [(item['type'], item.get('text', item.get('name'))) for item in reply['metadata']['process']] == [
        ('thinking', '查找相关资料'), ('text', '先查找'), ('tool', 'lookup'), ('tool', 'lookup'), ('thinking', '整理结果'),
    ]
    assert reply['metadata']['process'][2]['result'] == '找到两条'
    assert '资料' in reply['metadata']['process'][2]['argumentsPreview']
    assert reply['metadata']['turnMetrics']['totalTokens'] == 330
    assert 'opaque' not in json.dumps(data)
    assert 'private-argument' not in json.dumps(data)
    assert len(data['messages']) == 1


def test_history_argument_previews_hide_browser_inputs_and_mail_and_keep_unknown_usage_unknown(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, SuccessAdapter())
    key = sessions.open_role_session(role.id).sessionKey
    sessions.append_agent_message(key, AssistantMessage('', tool_calls=(
        ToolCall('input', 'agent_browser_fill', {'text': 'private-password'}),
        ToolCall('mail', 'mail.manage', {'body': 'private-mail'}),
        ToolCall('empty', 'lookup', {}),
        ToolCall('long', 'lookup', {'query': '很长的参数' * 100}),
        ToolCall('url', 'web_fetch', {'url': 'https://alice:private-password@example.test/docs?key=private-token&lang=zh'}),
    )), run_id='preview')
    sessions.append_agent_message(key, AssistantMessage('完成', metadata={'usage': {'input_tokens': 200, 'output_tokens': 20}}), run_id='preview')
    with client:
        data = client.get(f'/api/roles/{role.id}/session').json()
    serialized = json.dumps(data, ensure_ascii=False)
    assert 'private-password' not in serialized and 'private-mail' not in serialized and 'private-token' not in serialized
    metadata = data['messages'][-1]['metadata']
    assert metadata['process'][2]['argumentsPreview'] == '无参数'
    assert len(metadata['process'][3]['argumentsPreview']) == 120
    assert 'lang=zh' in metadata['process'][4]['argumentsPreview']
    assert 'totalTokens' not in metadata['turnMetrics']


def test_history_intents_stay_in_the_parameter_chip_without_exposing_hidden_business_arguments(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, SuccessAdapter())
    key = sessions.open_role_session(role.id).sessionKey
    calls = (
        ToolCall('long', 'lookup', {**{f'p{i}': '很长的参数' * 100 for i in range(40)}, 'description': '搜索相关配置资料'}),
        ToolCall('mail', 'mail.manage', {'body': 'private-mail', 'description': '查询邮件处理状态'}),
        ToolCall('browser', 'agent_browser_fill', {'text': 'private-password', 'description': '填写页面表单内容'}),
        ToolCall('secret', 'lookup', {'description': '读取配置 access_token=private-token'}),
        ToolCall('old', 'lookup', {'query': '旧消息'}),
    )
    sessions.append_agent_message(key, AssistantMessage(tool_calls=calls), run_id='intent-history')
    sessions.append_agent_message(key, AssistantMessage('完成'), run_id='intent-history')
    with client:
        data = client.get(f'/api/roles/{role.id}/session').json()
    serialized = json.dumps(data, ensure_ascii=False)
    assert all(secret not in serialized for secret in ('private-mail', 'private-password', 'private-token'))
    process = data['messages'][-1]['metadata']['process']
    for call, item in zip(calls[:3], process):
        assert call.arguments['description'] in item['argumentsPreview']
        assert len(item['argumentsPreview']) <= 120
    assert process[-1]['argumentsPreview'] == '{"query":"旧消息"}'
    assert all(item['status'] == 'unknown' for item in process)


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


def test_unaccounted_model_retries_do_not_claim_a_complete_token_total(tmp_path, monkeypatch):
    role, sessions, client = configure_session_api(tmp_path, monkeypatch, SuccessAdapter())
    key = sessions.open_role_session(role.id).sessionKey
    for index, extra in enumerate([
        {'emptyReplyRecovery': {'initialUsage': {'input_tokens': 10, 'output_tokens': 5}}},
        {'naturalReplyCorrected': True},
        {'auxiliaryUsageIncomplete': True},
    ]):
        sessions.append_agent_message(key, AssistantMessage('回复', metadata={
            'usage': {'input_tokens': 200, 'output_tokens': 20}, 'thinkingDurationMs': 1100, **extra,
        }), run_id=f'retry-{index}')
    with client:
        data = client.get(f'/api/roles/{role.id}/session').json()
    for row in data['messages']:
        assert row['metadata']['turnMetrics'] == {'thinkingDurationMs': 1100}
