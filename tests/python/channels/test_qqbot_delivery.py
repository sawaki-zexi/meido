import asyncio
import json
import time

import httpx

from backend.app.channels.host import ChannelHost
from backend.app.channels.models import DeliveryStatus, TargetKind, target_from_chat
from backend.app.channels.qqbot import QQBotAdapter, QQBotConfig
from backend.app.channels.transports import QQBotGatewayTransport
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from test_host import MemoryCredentials


def test_qqbot_markdown_passive_sequence_and_quota_survive_adapter_restart(tmp_path):
    sessions = SessionStore(tmp_path / 'sessions.db')
    store = MultichannelStore(sessions.database_path, sessions, MemoryCredentials())
    account = store.register_channel_account('r', 'qqbot', account_id='a', enabled=True)
    target = target_from_chat(account_id='a', chat_id='user', kind=TargetKind.PRIVATE,
        external_message_id='source', passiveExpiresAt=time.time() + 3600)
    store.remember_targets('r', [target])
    bodies = []
    async def scenario():
        def handle(request):
            if 'getAppAccessToken' in request.url.path:
                return httpx.Response(200, json={'access_token': 'synthetic', 'expires_in': 3600})
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(200, json={'id': str(len(bodies))})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            for _ in range(6):
                host = ChannelHost(store)
                adapter = QQBotAdapter(QQBotConfig('a', 'app'), transport=QQBotGatewayTransport('app', 'secret', http_client=client))
                host.register(adapter)
                result = await host.account_send('r', 'a', target.id, '**中文**', actor='owner')
            assert result.status is DeliveryStatus.FAILED
    asyncio.run(scenario())
    assert len(bodies) == 5
    assert [body['msg_seq'] for body in bodies] == [1, 2, 3, 4, 5]
    assert all(body['msg_id'] == 'source' and body['msg_type'] == 2 and body['markdown']['content'] == '**中文**' for body in bodies)
    assert all(item['status'] == 'sent' for item in store.list_deliveries('r')[1:])
