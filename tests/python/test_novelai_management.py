"""Source-local image management through real runtime and isolated HTTP fixtures."""
import asyncio
from datetime import datetime, timezone
import json
import threading
from types import SimpleNamespace

import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


@pytest.fixture
def management(novelai_main, monkeypatch):
    from backend.app import main
    from backend.app.agent_runtime import CancellationToken, ToolContext
    from backend.app.channels.group_tools import MultichannelGroupRepository
    from backend.app.channels.host import ChannelHost
    from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
    from backend.app.chat_attachments import resolve_attachment
    from backend.app.models import AgentRun
    client, service = novelai_main
    channels = main.multichannel_store
    account = channels.register_channel_account(service.role.id, 'qq', account_id='image-management', enabled=True, allow_group=True)
    sends, state = [], SimpleNamespace(uncertain=False)
    async def transport(operation, params):
        if operation == 'send_group_msg':
            sends.append(params)
            if state.uncertain and any(part.get('type') == 'image' for part in params['message']):
                raise TimeoutError('socket timeout after dispatch')
        return {'message_id': 172}
    async def no_avatar(*args):
        return None
    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
    host = ChannelHost(channels, attachment_resolver=lambda rid, aid: resolve_attachment(service.roles.workspace_path(rid), aid))
    host.register(adapter)
    monkeypatch.setattr(main, 'channel_host', host)
    monkeypatch.setattr(main, 'channel_adapters', {account.account_id: adapter})
    monkeypatch.setattr(main, 'group_repository', MultichannelGroupRepository(channels))
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    service.application.send_channel = host.account_send
    service.application.lock_for = main._runtime_role_lock
    def pair(sender='123'):
        code = channels.create_pairing_code(service.role.id, 'qq')
        assert channels.consume_pairing_code(code, role_id=service.role.id, channel='qq', scope='platform',
            platform_user_id=sender, account_id=account.account_id, chat_id=sender)
    pair()
    thread = channels.ensure_channel_thread(service.role.id, 'qq', account.account_id, '456', 'group')
    def turn(identifier, *, target=thread, sender='123', owner=True):
        active = service.sessions.active_run(target.role_id)
        if active:
            service.sessions.update_run(active.runId, status='completed', ended_at=datetime.now(timezone.utc))
        _, message = channels.append_event(target, direction='inbound', content='manage my image', external_message_id=identifier,
            metadata={'senderId': sender, 'senderIsOwner': owner})
        service.sessions.create_run_with_existing_user_message(AgentRun(runId=identifier, roleId=target.role_id,
            sessionKey=target.session_key, status='running', startedAt=datetime.now(timezone.utc)), message.id)
        context = ToolContext(target.role_id, target.session_key, identifier, CancellationToken(), identifier + '-call')
        tools = {tool.definition.name: tool for tool in main._runtime_tool_candidates(service.roles.get(target.role_id),
            is_channel_owner=owner, source_thread_id=target.thread_id, source_account_id=target.account_id,
            include_channel=True, preview=True)}
        return tools, context
    async def inbound(identifier, text, sender=123):
        await main._dispatch_channel_payload(account.account_id, {
            'post_type': 'message', 'message_type': 'group', 'self_id': 999, 'group_id': 456,
            'user_id': sender, 'message_id': identifier, 'sender': {'user_id': sender, 'nickname': 'fixture'},
            'message': [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': text}}],
        }, source_adapter=adapter)
    yield SimpleNamespace(main=main, client=client, service=service, channels=channels, account=account,
        thread=thread, sends=sends, state=state, pair=pair, turn=turn, inbound=inbound)
    asyncio.run(channels.avatars.stop_account(account.account_id))
    if main._proactive is not None:
        asyncio.run(main._proactive.close())


def test_owner_qq_image_is_discovered_queried_and_stopped_in_later_real_turns(management, monkeypatch):
    from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall
    m, observed, offered = management, {}, {}
    m.service.peer.block = True
    class Model:
        phase = 'generate'
        counts = {}
        async def stream_structured_messages(self, messages, configuration, tools):
            phase = self.phase
            count = self.counts[phase] = self.counts.get(phase, 0) + 1
            offered.setdefault(phase, []).append({tool['function']['name'] for tool in tools})
            observed.setdefault(phase, []).extend(item for item in messages if item['role'] == 'tool')
            if phase == 'generate' and count == 1:
                yield ModelToolCall('create-image', 'generate_image', {**m.service.request, 'background': True})
            elif phase == 'get' and count == 1:
                yield ModelToolCall('find-image-management', 'tool_search', {'query': 'select:manage_image'})
            elif (phase == 'get' and count == 2) or (phase in {'cancel', 'after'} and count == 1):
                yield ModelToolCall('manage-' + phase, 'manage_image', {**ids, 'action': 'cancel' if phase == 'cancel' else 'get'})
            else:
                yield ModelTextDelta('受控模型已读取真实工具结果。')
                return
            yield ModelResponseDone('tool_use')
    model = Model()
    monkeypatch.setattr(m.main, 'model_adapter', model)
    ids = {}
    def result(phase, call_id):
        return json.loads(next(item['content'] for item in reversed(observed[phase]) if item['tool_call_id'] == call_id))
    async def scenario():
        try:
            await m.inbound(171, '请后台画一片天空')
            accepted = result('generate', 'create-image')
            assert accepted['status'] == 'accepted'
            ids.update(generation_id=accepted['generationId'], attempt_id=accepted['attemptId'])
            assert await asyncio.to_thread(m.service.peer.accepted.wait, 2)
            assert m.service.sessions.active_run(m.service.role.id) is None
            model.phase = 'get'
            await m.inbound(172, '刚才那张图现在怎么样')
            assert 'manage_image' in offered['get'][0]
            queried = result('get', 'manage-get')
            assert queried['generationId'] == accepted['generationId']
            assert queried['taskId'] == accepted['taskId']
            assert queried['taskStatus'] in {'running', 'succeeded'}
            assert 'manage_image' in offered['get'][-1]
            assert result('get', 'find-image-management')['already_loaded'] == ['manage_image']
            config = m.service.application.settings.public()
            m.service.application.settings.save({'enabled': False}, expected_revision=config['revision'])
            model.phase = 'cancel'
            await m.inbound(173, '停止刚才那次生成')
            cancelled = result('cancel', 'manage-cancel')
            assert cancelled['attemptId'] == accepted['attemptId']
            assert cancelled['taskStatus'] in {'cancelled', 'succeeded'}
            assert cancelled['deliveryStatus'] == 'suppressed'
            assert cancelled['consumption'] == 'possible'
            assert 'generate_image' not in offered['cancel'][0] and 'manage_image' in offered['cancel'][0]
            model.phase = 'after'
            await m.inbound(174, '再确认这次任务的状态')
            assert result('after', 'manage-after')['deliveryStatus'] == 'suppressed'
            m.service.peer.release.set()
            await m.service.application.recover()
            assert len(m.service.peer.requests) == 1
            assert not any(part.get('type') == 'image' for send in m.sends for part in send['message'])
            assert sum(row.role == 'user' for row in m.service.sessions.list_messages(m.thread.session_key)) == 4
            text = json.dumps(observed, ensure_ascii=False)
            assert 'fixture-secret-171' not in text
            for phase in ('get', 'cancel', 'after'):
                payload = result(phase, 'manage-' + phase)
                assert not {'origin', 'sourceAuthority', 'logPath', 'parameters', 'prompt'} & payload.keys()
        finally:
            m.service.peer.release.set()
    asyncio.run(scenario())


async def accepted_image(m):
    from backend.app.novelai_generation.context import capture_tool_context
    tools, context = m.turn('create-image')
    source = capture_tool_context(m.service.sessions, m.channels, context, role_id=m.service.role.id, is_owner=True,
        source_thread_id=m.thread.thread_id, source_account_id=m.account.account_id)
    result = await tools['generate_image'].execute({**m.service.request, 'background': True}, context)
    assert not result.is_error and result.details['status'] == 'accepted'
    assert result.details['management'] == {'tool': 'manage_image', 'generation_id': result.details['generationId'],
        'attempt_id': result.details['attemptId']}
    return {'generation_id': result.details['generationId'], 'attempt_id': result.details['attemptId']}, source, result.details


@pytest.mark.parametrize('change', ['sender', 'group', 'role', 'account_generation', 'unbind', 'rebind', 'directory', 'attempt', 'missing'])
def test_management_denies_cross_source_and_revoked_attempts_without_disclosing_them(management, change):
    from backend.app.models import RoleInput
    m = management
    m.pair('124')
    async def scenario():
        ids, source, accepted = await accepted_image(m)
        assert (await asyncio.to_thread(m.service.tasks.query, accepted['taskId'], source.origin, wait_seconds=3)).ended_at is not None
        target, sender = m.thread, '123'
        if change == 'sender':
            sender = '124'
        elif change == 'group':
            target = m.channels.ensure_channel_thread(m.service.role.id, 'qq', m.account.account_id, '457', 'group')
        elif change == 'role':
            other = m.service.roles.create(RoleInput(name='other image role', profile={'profile': 'fixture'}))
            account = m.channels.register_channel_account(other.id, 'qq', account_id='other-image-account', enabled=True, allow_group=True)
            target = m.channels.ensure_channel_thread(other.id, 'qq', account.account_id, '456', 'group')
        elif change == 'account_generation':
            m.channels.delete_account(m.account.account_id)
            m.channels.register_channel_account(m.service.role.id, 'qq', account_id=m.account.account_id, enabled=True, allow_group=True)
            target = m.channels.ensure_channel_thread(m.service.role.id, 'qq', m.account.account_id, '456', 'group')
        elif change in {'unbind', 'rebind'}:
            binding = next(item for item in m.channels.list_identities() if item['platformUserId'] == '123')
            m.channels.unbind_identity(binding['identityId'])
            if change == 'rebind':
                m.pair()
        elif change == 'directory':
            m.channels.set_account_state(m.account.account_id, enabled=False)
            m.channels.set_account_state(m.account.account_id, enabled=True)
        elif change == 'attempt':
            ids['attempt_id'] = 'another-attempt'
        else:
            ids['generation_id'] = 'missing-generation'
        tools, current = m.turn('manage-image', target=target, sender=sender)
        for action in ('get', 'cancel'):
            denied = await tools['manage_image'].execute({**ids, 'action': action}, current)
            assert denied.is_error
            assert denied.details == {'status': 'unavailable', 'errorCode': 'generation_management_unavailable'}
            assert accepted['taskId'] not in denied.content
        original = m.service.application.owner_generation(m.service.role.id, accepted['generationId'], attempt_id=accepted['attemptId'])
        assert original['deliveryStatus'] == 'pending'
        assert len(m.service.peer.requests) == 1 and not m.sends
    asyncio.run(scenario())


def test_guest_cannot_discover_or_force_management_and_schema_cannot_choose_authority(management):
    from backend.app.agent_runtime.capabilities import CapabilityRegistry
    m = management
    async def scenario():
        ids, _, _ = await accepted_image(m)
        tools, context = m.turn('guest-management', sender='999', owner=False)
        resolution = CapabilityRegistry(list(tools.values())).resolve(role_id=m.service.role.id,
            session_key=context.session_key, run_id=context.run_id, external_only=True)
        names = {definition.name for definition in resolution.snapshot.tools}
        assert 'generate_image' in names
        assert not {'manage_image', 'task_output', 'task_stop', 'shell'} & names
        forced = await tools['manage_image'].execute({**ids, 'action': 'get'}, context)
        assert forced.is_error and 'generationId' not in forced.details
        tools, context = m.turn('owner-management')
        for invalid in ({'action': 'get', 'generation_id': ids['generation_id']},
                {**ids, 'action': 'get', 'source_scope': 'desktop'}, {**ids, 'action': 'get', 'role_id': m.service.role.id}):
            assert (await tools['manage_image'].execute(invalid, context)).is_error
        catalog = m.client.get(f'/api/roles/{m.service.role.id}/tools').json()['tools']
        assert next(tool for tool in catalog if tool['name'] == 'manage_image')['modelVisible']
        m.service.application.settings.clear_token()
        assert not (await tools['manage_image'].execute({**ids, 'action': 'get'}, context)).is_error
    asyncio.run(scenario())


@pytest.mark.parametrize('delivery', ['pending', 'sent', 'uncertain'])
def test_cancel_completed_image_suppresses_only_unpublished_media_and_keeps_real_receipts(management, delivery):
    m = management
    async def scenario():
        ids, source, accepted = await accepted_image(m)
        finished = await asyncio.to_thread(m.service.tasks.query, accepted['taskId'], source.origin, wait_seconds=3)
        assert finished.status == 'succeeded'
        if delivery != 'pending':
            adapter = m.main.channel_adapters[m.account.account_id]
            event = adapter.parse_event({'post_type': 'message', 'message_type': 'group', 'self_id': 999,
                'group_id': 456, 'user_id': 123, 'message_id': 170, 'message': [{'type': 'text', 'data': {'text': 'fixture'}}]})
            assert event is not None
            # Include the real observed sender target: background delivery
            # carries its frozen @mention as well as the group destination.
            m.channels.remember_targets(m.service.role.id, adapter.inbound_targets(event))
            m.state.uncertain = delivery == 'uncertain'
            await m.service.application.recover()
        before = m.service.application.owner_generation(m.service.role.id, accepted['generationId'], attempt_id=accepted['attemptId'])
        assert before['deliveryStatus'] == delivery
        sends_before = len(m.sends)
        assert sends_before == (0 if delivery == 'pending' else 1)
        tools, context = m.turn('cancel-completed')
        cancelled = await tools['manage_image'].execute({**ids, 'action': 'cancel'}, context)
        assert not cancelled.is_error
        assert cancelled.details['status'] == 'succeeded' and cancelled.details['taskStatus'] == 'succeeded'
        assert cancelled.details['deliveryStatus'] == ('suppressed' if delivery == 'pending' else delivery)
        assert cancelled.details['consumption'] == 'possible'
        await m.service.application.recover()
        assert len(m.sends) == sends_before and len(m.service.peer.requests) == 1
        queried = await tools['manage_image'].execute({**ids, 'action': 'get'}, context)
        assert not queried.is_error and queried.details['deliveryStatus'] == cancelled.details['deliveryStatus']
        ui = m.client.get(f'/api/roles/{m.service.role.id}/generations/{ids["generation_id"]}',
            params={'attemptId': ids['attempt_id']})
        assert ui.status_code == 200 and ui.json()['generation']['deliveryStatus'] == queried.details['deliveryStatus']
    asyncio.run(scenario())


@pytest.mark.parametrize('revoke_during_cleanup', [False, True])
def test_cancel_running_image_joins_and_rechecks_authority_after_cleanup(management, monkeypatch, revoke_during_cleanup):
    m = management
    m.service.peer.block = True
    entered, release = threading.Event(), threading.Event()
    original_stop = m.service.tasks.stop
    def paused_stop(*args):
        result = original_stop(*args)
        entered.set()
        if not release.wait(5):
            raise AssertionError('controlled cleanup not released')
        return result
    async def scenario():
        ids, source, accepted = await accepted_image(m)
        assert await asyncio.to_thread(m.service.peer.accepted.wait, 2)
        assert m.service.application.settings.public()['enabled']
        assert m.service.tasks.query(accepted['taskId'], source.origin).status == 'running'
        tools, context = m.turn('cancel-then-revoke')
        monkeypatch.setattr(m.service.tasks, 'stop', paused_stop)
        cancelling = asyncio.create_task(tools['manage_image'].execute({**ids, 'action': 'cancel'}, context))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            if revoke_during_cleanup:
                binding = next(item for item in m.channels.list_identities() if item['platformUserId'] == '123')
                m.channels.unbind_identity(binding['identityId'])
            release.set()
            result = await cancelling
            assert result.is_error is revoke_during_cleanup
            if revoke_during_cleanup:
                assert 'generationId' not in result.details
            else:
                assert result.details['taskStatus'] == 'cancelled'
                assert result.details['status'] == 'unknown' and result.details['consumption'] == 'possible'
            assert m.service.tasks.query(accepted['taskId'], source.origin).status == 'cancelled'
            assert m.service.application.owner_generation(m.service.role.id, ids['generation_id'],
                attempt_id=ids['attempt_id'])['deliveryStatus'] == 'suppressed'
            assert len(m.service.peer.requests) == 1 and not m.sends
        finally:
            release.set()
            m.service.peer.release.set()
            await cancelling
    asyncio.run(scenario())


def test_exact_attempt_cancel_does_not_stop_a_new_attempt_in_the_same_generation(management):
    from backend.app.novelai_generation.context import GenerationContext
    m = management
    async def scenario():
        ids, source, accepted = await accepted_image(m)
        assert (await asyncio.to_thread(m.service.tasks.query, accepted['taskId'], source.origin, wait_seconds=3)).status == 'succeeded'
        first = m.service.application.owner_generation(m.service.role.id, ids['generation_id'], attempt_id=ids['attempt_id'])
        parameters = {key: value for key, value in m.service.request.items() if key != 'seed'}
        parameters['background'] = True
        replacement = m.service.application.regeneration_source(m.service.role.id, ids['generation_id'],
            source_attempt_id=ids['attempt_id'], request_id='34634634634634634634634634634634', parameters=parameters)
        m.service.peer.accepted.clear()
        m.service.peer.block = True
        newer = await m.service.application.regenerate(replacement, ids['generation_id'], source_attempt_id=ids['attempt_id'],
            expected_attachment_id=first['attachments'][0]['id'], arguments=parameters,
            expected_revision=m.service.application.settings.public()['revision'], delivery_mode='history_only')
        assert newer['backgroundAccepted'] and newer['attemptId'] != ids['attempt_id']
        assert await asyncio.to_thread(m.service.peer.accepted.wait, 2)
        newer_source = GenerationContext.from_dict(json.loads(m.service.application.ledger.attempt(newer['attemptId'])['origin']))
        tools, context = m.turn('stop-exact-old-attempt')
        try:
            stopped = await tools['manage_image'].execute({**ids, 'action': 'cancel'}, context)
            assert not stopped.is_error and stopped.details['attemptId'] == ids['attempt_id']
            assert m.service.tasks.query(newer['taskId'], newer_source.origin).status == 'running'
            new_result = await tools['manage_image'].execute({'action': 'cancel', 'generation_id': ids['generation_id'],
                'attempt_id': newer['attemptId']}, context)
            assert not new_result.is_error and new_result.details['taskStatus'] == 'cancelled'
            assert len(m.service.peer.requests) == 2 and not m.sends
        finally:
            m.service.peer.release.set()
            await m.service.application.cancel(m.service.role.id, ids['generation_id'], attempt_id=newer['attemptId'])
    asyncio.run(scenario())


def test_host_management_availability_cannot_be_restored_by_old_enabled_tools(management):
    m = management
    role = m.service.role.model_copy(update={'agentConfig': m.service.role.agentConfig.model_copy(
        update={'enabledTools': [*m.service.role.agentConfig.enabledTools, 'manage_image']})})
    assert 'manage_image' in m.main._runtime_capability_policy(role, include_channel=True).enabled_tools
    asyncio.run(m.service.application.close())
    assert 'manage_image' not in m.main._runtime_capability_policy(role, include_channel=True).enabled_tools
