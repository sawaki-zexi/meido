"""Real QQ intake and runtime with synthetic model and platform services."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


PRIVATE_ERROR = 'synthetic-provider-private-detail'
PARTIAL = '尚未完成的半句话'
RECOVERED = '后续请求正常完成。'


@pytest.fixture
def failure_turn(novelai_main, monkeypatch):
    from backend.app import main
    from backend.app.advanced_settings import AdvancedSettingsStore
    from backend.app.channels.host import ChannelHost
    from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
    from backend.app.channels.group_tools import MultichannelGroupRepository
    from backend.app.models import RoleUpdateInput

    _, service = novelai_main
    channels = main.multichannel_store
    account = channels.register_channel_account(service.role.id, 'qq',
        account_id='failure-qq', enabled=True, allow_group=True)
    code = channels.create_pairing_code(service.role.id, 'qq')
    assert channels.consume_pairing_code(code, role_id=service.role.id, channel='qq',
        scope='platform', platform_user_id='123', account_id=account.account_id, chat_id='123')
    state = SimpleNamespace(service=service, channels=channels, account=account,
        sends=[], receipt='sent', requests=[])

    async def transport(operation, params):
        if operation == 'send_group_msg':
            state.sends.append(deepcopy(params))
            if state.receipt == 'uncertain':
                raise TimeoutError('controlled lost receipt')
        return {'message_id': 1000 + len(state.sends)}

    async def no_avatar(*args):
        return None

    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
    host = ChannelHost(channels)
    host.register(adapter)
    monkeypatch.setattr(main, 'channel_host', host)
    monkeypatch.setattr(main, 'channel_adapters', {account.account_id: adapter})
    monkeypatch.setattr(main, 'group_repository', MultichannelGroupRepository(channels))
    monkeypatch.setattr(main, '_fetch_channel_avatar', no_avatar)
    monkeypatch.setattr(main, '_channel_reply_plans', {})
    monkeypatch.setattr(main, '_channel_reply_locks', {})
    monkeypatch.setattr(main, '_ordinary_reply_runs', {})
    monkeypatch.setattr(main, 'advanced_settings', AdvancedSettingsStore(service.application.root / 'advanced.db'))

    def set_split(split):
        role = service.roles.get(service.role.id)
        service.roles.update(role.id, RoleUpdateInput(name=role.name, profile=role.profile,
            agentConfig=role.agentConfig.model_copy(update={'naturalChatEnabled': split})))

    async def send(identifier, text='请回答当前问题'):
        await main._dispatch_channel_payload(account.account_id, {
            'post_type': 'message', 'message_type': 'group', 'self_id': 999,
            'group_id': 100, 'user_id': 123, 'message_id': identifier,
            'sender': {'user_id': 123, 'nickname': 'synthetic owner'},
            'message': [{'type': 'at', 'data': {'qq': '999'}},
                        {'type': 'text', 'data': {'text': text}}],
        }, source_adapter=adapter)

    def rows():
        thread = channels.get_thread_by_chat(service.role.id, account.account_id, '100', chat_type='group')
        return service.sessions.list_messages(thread.session_key)

    def execute(scenario):
        async def owned():
            try:
                await scenario()
            finally:
                await channels.avatars.stop_account(account.account_id)
                if main._proactive is not None:
                    await main._proactive.close()
        asyncio.run(owned())

    state.send, state.rows, state.execute, state.set_split = send, rows, execute, set_split
    yield state


def text_of(params):
    return ''.join(part.get('data', {}).get('text', '') for part in params['message'] if part['type'] == 'text')


@pytest.mark.parametrize('split', [False, True], ids=['ordinary', 'split'])
@pytest.mark.parametrize('failure', ['empty', 'partial', 'timeout', 'budget'])
def test_failed_turn_reports_once_and_next_message_recovers(failure_turn, monkeypatch, split, failure):
    from backend.app import main
    from backend.app.model_adapter import ModelTextDelta, ModelResponseDone, ModelToolCall
    state = failure_turn
    state.set_split(split)
    if failure == 'budget':
        main.advanced_settings.update(expected_revision=0, operation_id='one-turn', values={'maxIterations': 1})

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            state.requests.append(deepcopy(messages))
            if len(state.requests) == 1:
                if failure == 'budget':
                    yield ModelToolCall('search', 'tool_search', {'query': 'select:list_dir'})
                    yield ModelResponseDone('tool_use')
                    return
                if failure == 'partial':
                    yield ModelTextDelta(PARTIAL)
                if failure == 'timeout':
                    raise TimeoutError(PRIVATE_ERROR)
                raise RuntimeError(PRIVATE_ERROR)
            if split:
                yield ModelToolCall('finish', 'finish_reply', {'messages': [RECOVERED]})
                yield ModelResponseDone('tool_use')
            else:
                yield ModelTextDelta(RECOVERED)
                yield ModelResponseDone('stop')

    monkeypatch.setattr(main, 'model_adapter', Model())
    observed = {}

    async def scenario():
        await state.send(171)
        observed['failed'] = next(row for row in reversed(state.rows()) if row.role == 'assistant')
        await state.send(171)  # Real intake deduplication, not model-side replay.
        observed['first_sends'] = len(state.sends)
        await state.send(172, '请回答后续问题')

    state.execute(scenario)
    failed = observed['failed']
    run = state.service.sessions.get_run(failed.runId)
    assert run.status == ('max_turns' if failure == 'budget' else 'failed')
    assert failed.status == 'failed' and failed.content == main.QQ_REPLY_FAILURE_NOTICE
    assert failed.metadata['channelFailureFeedback'] == ('max_turns' if failure == 'budget' else 'failed')
    assert failed.metadata['deliveryStatus'] == 'sent'
    assert observed['first_sends'] == 1 and len(state.sends) == 2
    assert text_of(state.sends[0]) == main.QQ_REPLY_FAILURE_NOTICE
    assert text_of(state.sends[1]) == RECOVERED
    assert len(state.requests) == 2 and state.service.peer.requests == []
    assert not any(PRIVATE_ERROR in text_of(item) or PARTIAL in text_of(item) for item in state.sends)
    assert any(part['type'] == 'reply' and str(part['data']['id']) == '171' for part in state.sends[0]['message'])
    assert any(part['type'] == 'at' and str(part['data']['qq']) == '123' for part in state.sends[0]['message'])
    assert state.sends[0]['group_id'] == 100
    history = state.service.sessions.runtime_messages(failed.sessionKey)
    assert not any(item.role == 'assistant' and main.QQ_REPLY_FAILURE_NOTICE in item.content for item in history)
    if failure == 'budget':
        assert any(row.runId == run.runId and row.role == 'tool' and not row.isError for row in state.rows())


@pytest.mark.parametrize('withdrawal', ['cancel', 'owner', 'plan'])
def test_cancelled_or_obsolete_turn_does_not_send_failure_notice(failure_turn, monkeypatch, withdrawal):
    from backend.app import main
    from backend.app.model_adapter import ModelTextDelta
    state = failure_turn
    state.set_split(withdrawal == 'plan')

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            state.requests.append(deepcopy(messages))
            if withdrawal == 'owner':
                state.channels.unbind_identity(state.channels.list_identities()[0]['identityId'])
            elif withdrawal == 'plan':
                for plan in main._channel_reply_plans.values():
                    plan.invalidate()
            else:
                active = state.service.sessions.active_run(state.service.role.id)
                main.runtime_manager.cancel_run(active.runId)
                await asyncio.sleep(0)
            if False:
                yield ModelTextDelta('never')
            raise RuntimeError(PRIVATE_ERROR)

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def scenario():
        try:
            await state.send(171)
        except asyncio.CancelledError:
            pass

    state.execute(scenario)
    assert len(state.requests) == 1
    assert state.sends == []
    assert state.service.sessions.active_run(state.service.role.id) is None


def test_unknown_failure_notice_receipt_is_retained_without_resend(failure_turn, monkeypatch):
    from backend.app import main
    from backend.app.model_adapter import ModelTextDelta
    state = failure_turn
    state.receipt = 'uncertain'

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            state.requests.append(deepcopy(messages))
            if False:
                yield ModelTextDelta('never')
            raise RuntimeError(PRIVATE_ERROR)

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def scenario():
        await state.send(171)
        await state.send(171)

    state.execute(scenario)
    reply = next(row for row in reversed(state.rows()) if row.role == 'assistant')
    assert len(state.requests) == 1 and len(state.sends) == 1
    assert reply.status == 'failed' and reply.content == main.QQ_REPLY_FAILURE_NOTICE
    assert reply.metadata['deliveryStatus'] == 'uncertain'
    assert state.service.sessions.get_run(reply.runId).status == 'failed'
