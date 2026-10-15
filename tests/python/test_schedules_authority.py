"""The actual host source, rather than model-supplied identity, owns plans."""
import asyncio
from datetime import datetime, timezone
import time

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.channels import ChannelAdapter, ChannelHost, DeliveryResult, DeliveryStatus, TargetKind
from backend.app.channels.models import target_from_chat
from backend.app.models import AgentRun
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from backend.app.model_adapter import ModelTextDelta, ModelToolCall


@pytest.mark.parametrize('change', ['unbind', 'rebind', 'recreate', 'sender', 'thread', 'role', 'guest'])
def test_channel_plan_stays_in_actual_source_and_cannot_survive_revocation(api, monkeypatch, change):  # noqa: F811
    from backend.app import main
    client, roles, sessions, _ = api
    channels = main.multichannel_store
    now = [time.time()]
    monkeypatch.setattr(main, '_schedule_clock', lambda: now[0])
    monkeypatch.setattr(main, '_schedules', None)
    role = client.post('/api/roles', json={'name': '来源计划', 'profile': {'profile': '测试'},
        'agentConfig': {'enabledTools': ['schedule', 'list_schedules', 'cancel_schedule']}}).json()['role']
    rid = role['id']
    account = channels.register_channel_account(rid, 'qq', account_id='local-schedule-source', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread(rid, 'qq', account.account_id, 'group-local', 'group')
    target = target_from_chat(account_id=account.account_id, chat_id=thread.chat_id, kind=TargetKind.GROUP)
    channels.remember_targets(rid, [target])
    class Adapter(ChannelAdapter):
        channel = 'qq'
        account_id = account.account_id
        sent = []
        def parse_event(self, payload):
            return None
        async def list_targets(self):
            return [target]
        async def send(self, message):
            self.sent.append(message)
            return DeliveryResult(DeliveryStatus.SENT, message.target, 'local-receipt')
    adapter = Adapter()
    host = ChannelHost(channels)
    host.register(adapter)
    monkeypatch.setattr(main, 'channel_host', host)
    def pair():
        code = channels.create_pairing_code(rid, 'qq')
        assert channels.consume_pairing_code(code, role_id=rid, channel='qq', scope='platform',
            platform_user_id='owner-local', account_id=account.account_id, chat_id='owner-local')
    pair()
    def turn(run, *, sender='owner-local', source=thread, owner=True):
        previous = sessions.active_run(rid)
        if previous:
            sessions.update_run(previous.runId, status='completed', ended_at=datetime.now(timezone.utc))
        _, message = channels.append_event(source, direction='inbound', content='time request', external_message_id=run,
            metadata={'senderId': sender, 'senderIsOwner': True, 'hostReceivedAt': now[0]})
        sessions.create_run_with_existing_user_message(AgentRun(runId=run, roleId=rid, sessionKey=source.session_key,
            status='running', startedAt=datetime.now(timezone.utc)), message.id)
        tools = {t.definition.name: t for t in main._runtime_tool_candidates(roles.get(rid), is_channel_owner=owner,
            source_thread_id=source.thread_id, source_account_id=account.account_id, include_channel=True)}
        return tools, ToolContext(rid, source.session_key, run, CancellationToken(), 'schedule-call')
    async def run():
        tools, context = turn('initial')
        result = await tools['schedule'].execute({'tier': 'instant', 'trigger': 'after', 'when': '10s', 'message': 'source-private-158'}, context)
        assert not result.is_error, result.content
        identifier = result.details['id']
        later_tools, later = turn('same-source')
        visible = await later_tools['list_schedules'].execute({}, later)
        assert visible.details['schedules'][0]['id'] == identifier
        if change in {'unbind', 'rebind'}:
            channels.unbind_identity(str(channels.list_identities()[0]['identityId']))
            if change == 'rebind':
                pair()
        elif change == 'recreate':
            channels.delete_account(account.account_id)
            channels.register_channel_account(rid, 'qq', account_id=account.account_id, enabled=True, allow_group=True)
        elif change == 'sender':
            later_tools, later = turn('different-sender', sender='visitor-local')
        elif change == 'thread':
            other = channels.ensure_channel_thread(rid, 'qq', account.account_id, 'other-group', 'group')
            later_tools, later = turn('different-thread', source=other)
        elif change == 'role':
            later = ToolContext('different-role', later.session_key, later.run_id, CancellationToken(), 'schedule-call')
        elif change == 'guest':
            later_tools, later = turn('guest-host', owner=False)
        denied = await later_tools['cancel_schedule'].execute({'id': identifier}, later)
        assert denied.is_error and 'source-private-158' not in denied.content
        if change in {'unbind', 'rebind', 'recreate'}:
            now[0] += 10
            await main._schedule_application().tick()
            assert adapter.sent == []
        else:
            # A different caller cannot cancel the original owner's schedule.
            now[0] += 10
            await main._schedule_application().tick()
            assert len(adapter.sent) == 1
    asyncio.run(run())


@pytest.mark.parametrize('change', ['target_rebind', 'unrelated_binding'])
def test_explicit_owner_target_freezes_its_binding_without_freezing_unrelated_identity(api, monkeypatch, change):  # noqa: F811
    from backend.app import main
    client, _, sessions, _ = api
    channels, now = main.multichannel_store, [time.time()]
    monkeypatch.setattr(main, '_schedule_clock', lambda: now[0])
    monkeypatch.setattr(main, '_schedules', None)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            if not any(m['role'] == 'tool' for m in messages):
                yield ModelToolCall('explicit-target', 'schedule', {'tier': 'instant', 'trigger': 'after',
                    'when': '1s', 'message': 'owner-target-158', 'channel': 'qq', 'chat_id': 'owner'})
            else:
                yield ModelTextDelta('已创建')
    monkeypatch.setattr(main, 'model_adapter', Model())
    role = client.post('/api/roles', json={'name': '固定目标', 'profile': {'profile': '测试'},
        'agentConfig': {'enabledTools': ['schedule']}}).json()['role']
    rid = role['id']
    account = channels.register_channel_account(rid, 'qq', account_id='target-local', enabled=True)
    target = target_from_chat(account_id=account.account_id, chat_id='paired-chat', kind=TargetKind.PRIVATE)
    thread = channels.ensure_channel_thread(rid, 'qq', account.account_id, target.chat_id, 'private')
    channels.remember_targets(rid, [target])
    def pair(account_id, channel='qq', sender='paired-sender', chat='paired-chat', role_id=rid):
        code = channels.create_pairing_code(role_id, channel)
        assert channels.consume_pairing_code(code, role_id=role_id, channel=channel, scope='platform',
            platform_user_id=sender, account_id=account_id, chat_id=chat)
    pair(account.account_id)
    class Adapter(ChannelAdapter):
        channel = 'qq'
        account_id = account.account_id
        sent = []
        def parse_event(self, payload):
            return None
        async def list_targets(self):
            return [target]
        async def send(self, message):
            self.sent.append(message)
            return DeliveryResult(DeliveryStatus.SENT, message.target, 'local-target-proof')
    adapter = Adapter()
    host = ChannelHost(channels)
    host.register(adapter)
    monkeypatch.setattr(main, 'channel_host', host)
    response = client.post(f'/api/roles/{rid}/messages', json={'content': '一秒后联系已配对主人'})
    assert 'assistant_completed' in response.text
    if change == 'target_rebind':
        channels.unbind_identity(str(channels.list_identities()[0]['identityId']))
        pair(account.account_id)
    else:
        other_role = client.post('/api/roles', json={'name': '无关角色', 'profile': {'profile': '测试'}}).json()['role']
        other = channels.register_channel_account(other_role['id'], 'qq', account_id='unrelated-local', enabled=True)
        pair(other.account_id, sender='unrelated-sender', chat='unrelated-chat', role_id=other_role['id'])
    now[0] += 1
    asyncio.run(main._schedule_application().tick())
    assert len(adapter.sent) == (0 if change == 'target_rebind' else 1)
    assert len(sessions.runtime_messages(thread.session_key)) == (0 if change == 'target_rebind' else 1)
