import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelTextBlock
from backend.app.agent_runtime.meido_provider import MeidoProvider
from test_background_shell_api import api as shell_api
from test_spawn_api import api


@pytest.mark.parametrize('revoke_before_send', [False, True])
def test_web_request_reads_owner_group_background_and_rechecks_before_model(api, monkeypatch, revoke_before_send):
    client, _, sessions, _ = api
    requests = []
    async def no_maintenance(*args, **kwargs):
        pass
    monkeypatch.setattr(main.memory_worker, '_maintain', no_maintenance)
    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools, **kwargs):
            requests.append(messages)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock('记得刚才的群聊'),)))
    monkeypatch.setattr(main, 'model_adapter', Adapter())
    role = client.post('/api/roles', json={'name': '近期群聊', 'profile': {'profile': '测试角色'}}).json()['role']
    channels = main.multichannel_store
    channels.register_channel_account(role['id'], 'qq', account_id='qq', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread(role['id'], 'qq', 'qq', 'group', 'group', title='茶友群')
    code = channels.create_pairing_code()
    channels.consume_pairing_code(code, role_id=role['id'], channel='qq', scope='platform',
        platform_user_id='owner', account_id='qq', chat_id='owner')
    channels.set_group_listening(role['id'], 'qq', 'group', True)
    channels.listening.hear(thread, 'OWNER_RECENT_GROUP_FACT', 'recent', {'senderId': 'owner', 'senderIsOwner': True})
    channels.listening.hear(thread, 'OTHER_MEMBER_SECRET', 'friend', {'senderId': 'friend', 'senderIsOwner': False})
    if revoke_before_send:
        prepare = MeidoProvider.prepare_context
        async def revoke(self, signal, **kwargs):
            context = await prepare(self, signal, **kwargs)
            channels.unbind_identity(channels.list_identities()[0]['identityId'])
            return context
        monkeypatch.setattr(MeidoProvider, 'prepare_context', revoke)
    response = client.post(f"/api/roles/{role['id']}/messages", json={'content': '刚才我在群里说了什么'})
    if revoke_before_send:
        assert requests == []
        assert 'assistant_completed' not in response.text
    else:
        assert 'assistant_completed' in response.text, response.text
        assert 'OWNER_RECENT_GROUP_FACT' in str(requests)
        assert 'OTHER_MEMBER_SECRET' not in str(requests)
        assert len(requests) == 1
    assert 'OWNER_RECENT_GROUP_FACT' not in str(sessions.list_messages('role:' + role['id']))
