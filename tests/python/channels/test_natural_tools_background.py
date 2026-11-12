"""Natural image handover uses the real bounded TaskRuntime and media outbox."""
import asyncio
from dataclasses import replace
import json

import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
from backend.app import main
from backend.app.channels.host import ChannelHost
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.natural import NaturalLimits
from backend.app.chat_attachments import resolve_attachment
from backend.app.model_adapter import ModelTextDelta, ModelToolCall, ModelResponseDone
from test_natural_scheduler import Clock


@pytest.mark.parametrize('sender', ['11', '22'])
@pytest.mark.parametrize('outcome', ['sent', 'uncertain', 'disabled', 'generation', 'quota'])
@pytest.mark.parametrize('model', ['nai-diffusion-4-5-curated', 'nai-diffusion-5-full'])
def test_background_outlives_short_batch_but_not_source_revocation(novelai_main, monkeypatch, sender, outcome, model):
    _, service = novelai_main
    service.application.settings.save({'allowedModels': [model], 'defaultModel': model},
        expected_revision=service.application.settings.public()['revision'])
    channels = main.multichannel_store
    account = channels.register_channel_account(service.role.id, 'qq', account_id='natural-image', enabled=True, allow_group=True)
    code = channels.create_pairing_code(service.role.id, 'qq')
    assert channels.consume_pairing_code(code, role_id=service.role.id, channel='qq', scope='platform',
        platform_user_id='11', account_id=account.account_id, chat_id='11')
    sends, requests = [], []
    clock = Clock()

    async def transport(operation, params):
        assert operation == 'send_group_msg'
        sends.append(params)
        if outcome == 'uncertain' and any(item['type'] == 'image' for item in params['message']):
            return {}
        return {'message_id': str(100 + len(sends))}

    async def no_avatar(*args):
        return None

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            requests.append((messages, tools))
            if len(requests) == 1:
                yield ModelTextDelta(json.dumps({'action':'reply', 'response':{
                    'message_id':'image-source', 'intent':'根据补充资料画图'}}))
            elif len(requests) == 2:
                assert 'generate_image' in {item['function']['name'] for item in tools}
                yield ModelToolCall('image', 'generate_image', service.request)
                yield ModelResponseDone('tool_use')
            else:
                result = json.loads(next(item['content'] for item in reversed(messages) if item['role'] == 'tool'))
                assert result['status'] == 'accepted'
                yield ModelTextDelta(json.dumps({'text':'已受理，完成后回到本群。'}))

    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
    host = ChannelHost(channels, attachment_resolver=lambda rid, aid: resolve_attachment(service.roles.workspace_path(rid), aid))
    host.register(adapter)
    monkeypatch.setattr(main, 'channel_host', host)
    monkeypatch.setattr(main, 'channel_adapters', {account.account_id:adapter})
    monkeypatch.setattr(main, 'group_repository', MultichannelGroupRepository(channels))
    monkeypatch.setattr(main, 'model_adapter', Model())
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    monkeypatch.setattr(main, '_natural_scheduler', None)
    monkeypatch.setattr(main, '_schedule_clock', clock)
    service.application.send_channel = host.account_send
    scheduler = main._natural_application()
    scheduler.sleep = clock.sleep
    scheduler.limits = replace(NaturalLimits(), coalesce=1, min_role_interval=0,
        **({'max_calls': 2} if outcome == 'quota' else {}))
    channels.listening.clock = clock
    channels.set_group_listening(service.role.id, account.account_id, '100', True)
    thread = channels.get_thread_by_chat(service.role.id, account.account_id, '100', chat_type='group')
    channels.listening.set_natural_enabled(service.role.id, thread.thread_id, True)
    service.peer.block = True

    async def run():
        try:
            await main._dispatch_channel_payload(account.account_id, {
                'post_type':'message', 'message_type':'group', 'self_id':99, 'group_id':100,
                'message_id':'image-source', 'sender':{'user_id':sender, 'nickname':'材料提供者'},
                'message':[{'type':'text','data':{'text':'补充：蓝天。'}}],
            }, source_adapter=adapter)
            await asyncio.sleep(0)
            pending = tuple(scheduler.tasks.values())
            await clock.advance(1)
            await asyncio.wait_for(asyncio.gather(*pending), 10)
            assert len(requests) == (2 if outcome == 'quota' else 3) and len(service.peer.requests) == 1
            assert service.peer.requests[0]['body']['model'] == model
            rows = service.sessions.list_messages(thread.session_key)
            user = next(row for row in rows if row.role == 'user')
            source = service.sessions.get_run(user.runId).modelSnapshot['channelTurnSource']
            assert source['owner_context'] is (sender == '11')
            assert source['connection_generation'] == host.connection_generation(account.account_id)
            assert user.metadata['triggered'] is False
            assert len(sends) == (0 if outcome == 'quota' else 1)
            assert all(part['type'] != 'image' for item in sends for part in item['message'])
            await clock.advance(60)  # Original 45-second batch has ended.
            if outcome == 'disabled':
                channels.listening.set_natural_enabled(service.role.id, thread.thread_id, False)
            elif outcome == 'generation':
                host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=transport))
            service.peer.release.set()
            for _ in range(150):
                await service.application.recover()
                if any(any(part['type'] == 'image' for part in item['message']) for item in sends):
                    break
                await asyncio.sleep(.02)
            image_sends = [item for item in sends if any(part['type'] == 'image' for part in item['message'])]
            assert len(image_sends) == (0 if outcome in {'disabled', 'generation'} else 1)
            assert all(item['group_id'] == 100 for item in sends)
            await service.application.recover()
            assert len(service.peer.requests) == 1
            with service.application.ledger.connect() as db:
                outbox = db.execute('SELECT state,background_accepted FROM novelai_media_outbox').fetchone()
                assert outbox['background_accepted'] == 1
                if outcome in {'sent', 'uncertain', 'quota'}:
                    assert outbox['state'] == ('sent' if outcome == 'quota' else outcome)
            assert not service.sessions.list_messages('role:' + service.role.id)
        finally:
            service.peer.release.set()
            await scheduler.close()
            await service.application.close()
            await channels.avatars.stop_account(account.account_id)
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(run())
