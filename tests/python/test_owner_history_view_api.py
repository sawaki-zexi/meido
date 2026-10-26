"""Owner history is real provider dialogue, with the original UI transcript intact."""
import asyncio
import json

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelTextBlock
from test_background_shell_api import api as shell_api
from test_spawn_api import api
from test_model_image_request_budget import local_model


def paired_thread(channels, role_id, *, account="qq", sender="22"):
    channels.register_channel_account(role_id, "qq", account_id=account, allow_private=True)
    thread = channels.ensure_channel_thread(role_id, "qq", account, sender, "private", title="主人手机")
    code = channels.create_pairing_code()
    assert channels.consume_pairing_code(code, role_id=role_id, channel="qq", scope="platform",
        platform_user_id=sender, account_id=account, chat_id=sender)
    return thread


def test_desktop_model_gets_more_than_24_private_messages_as_dialogue(api, monkeypatch):
    client, _, sessions, _ = api
    requests = []
    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools, **kwargs):
            requests.append(messages)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock("接上了"),)))
    monkeypatch.setattr(main, "model_adapter", Adapter())
    role = client.post("/api/roles", json={"name": "跨端历史", "profile": {"profile": "测试角色"}}).json()["role"]
    configuration = client.post("/api/model/configurations", json={"providerId": "custom", "provider": "local",
        "baseUrl": "http://127.0.0.1:9999/v1", "model": "fixture", "apiKey": "fixture", "contextWindowTokens": 24000}).json()["configuration"]
    client.put(f"/api/roles/{role['id']}/model-configuration", json={"configurationId": configuration["id"]})
    thread = paired_thread(main.multichannel_store, role["id"])
    for index in range(18):
        sessions.append_message(thread.session_key, "user", f"手机第{index}个问题", run_id=f"past-{index}")
        sessions.append_message(thread.session_key, "assistant", f"手机第{index}个回答", run_id=f"past-{index}",
            metadata={"channel": "qq", "deliveryStatus": "sent"})
    response = client.post(f"/api/roles/{role['id']}/messages", json={"content": "继续最开始那个话题"})
    assert "assistant_completed" in response.text
    actual = [(message.role, block.text) for message in requests[0] for block in message.content if isinstance(block, ModelTextBlock)]
    assert any(role == "user" and "手机第0个问题" in text for role, text in actual)
    assert any(role == "assistant" and "手机第0个回答" in text for role, text in actual)
    assert sum("手机第" in text for role, text in actual if role in {"user", "assistant"}) == 36
    assert sum("继续最开始那个话题" in text for _, text in actual) == 1
    assert len(sessions.list_messages(thread.session_key)) == 36
    assert [message.content for message in sessions.list_messages(f"role:{role['id']}")] == ["继续最开始那个话题", "接上了"]


def test_real_local_model_receives_cross_channel_image_quote_and_assistant_sources(api, monkeypatch, local_model):
    import io
    from PIL import Image
    from backend.app.ai import ModelAdapterRegistry, RoutedModelAdapter
    from backend.app.chat_attachments import save_attachment
    client, roles, sessions, _ = api
    monkeypatch.setattr(main, 'model_adapter', RoutedModelAdapter(ModelAdapterRegistry(timeout=3)))
    role = client.post('/api/roles', json={'name': '图片接续', 'profile': {'profile': '测试角色'}}).json()['role']
    config = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
        'api': 'openai-completions', 'baseUrl': local_model.url, 'model': 'fixture', 'apiKey': 'synthetic',
        'contextWindowTokens': 131072}).json()['configuration']
    client.put(f"/api/roles/{role['id']}/model-configuration", json={'configurationId': config['id']})
    thread = paired_thread(main.multichannel_store, role['id'])
    data = io.BytesIO(); Image.new('RGB', (2, 2), 'red').save(data, 'PNG')
    image = save_attachment(roles.workspace_path(role['id']), 'own.png', 'image/png', data.getvalue())
    sessions.append_message(thread.session_key, 'user', '手机图片问题', run_id='image',
        metadata={'platformMessageId': '11', 'attachments': [image]})
    sessions.append_message(thread.session_key, 'assistant', '这是一张红图', run_id='image',
        metadata={'deliveryStatus': 'sent', 'content_blocks': [{'type': 'text', 'text': '这是一张红图'}]})
    sessions.append_message(thread.session_key, 'user', '我引用这张图', metadata={'replyTo': '11'})
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '桌面继续分析这张图'})
    assert 'assistant_completed' in response.text, response.text
    assert len(local_model.bodies) == 1
    payload = json.loads(local_model.bodies[0])
    actual = payload['messages']
    images = [block for message in actual if isinstance(message['content'], list)
        for block in message['content'] if block['type'] == 'image_url']
    assert len(images) == 2
    assert all(block['image_url']['url'].startswith('data:image/png;base64,') for block in images)
    assistant = next(message for message in actual if message['role'] == 'assistant')
    assert '回复来源：qq / 主人手机' in str(assistant)
    assert '引用主人' in str(actual) and str(actual).count('桌面继续分析这张图') == 1
