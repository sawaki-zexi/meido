"""Models discover a nondefault original target from the real provider input."""
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import json

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from test_proactive_targets_api import api, shell_api, platform, source, save_external  # noqa: F401
from test_proactive_channel_guards import decision, run
from test_proactive_channel_delivery import channel_for
from test_proactive_drift_delivery import prepare, settled

pytestmark = pytest.mark.usefixtures('local_platform_only')


class ProviderSelectedTarget:
    """No fixture IDs enter this provider; tool pairs come only from messages."""
    def __init__(self, mode):
        self.mode, self.requests, self.views = mode, [], []
        self.missing_view = False

    async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
        self.requests.append({'messages': [asdict(item) for item in messages], 'tools': tools})
        prefix = '已冻结发送目标：'
        views = [json.loads(block.text[len(prefix):]) for message in messages for block in message.content
                 if getattr(block, 'text', '').startswith(prefix)]
        if not views:
            self.missing_view = True
            raise AssertionError('provider did not receive its frozen allowed target pairs')
        assert len(views) == 1
        view = views[0]
        assert set(view) == {'defaultTargetRef', 'allowedTargets'}
        assert view['defaultTargetRef'] == 'desktop'
        assert 1 <= len(view['allowedTargets']) <= 4
        for item in view['allowedTargets']:
            assert set(item) == ({'ref', 'channel'} if item['channel'] == 'desktop'
                                 else {'ref', 'channel', 'account_id', 'target_id'})
        self.views.append(view)
        external = [item for item in view['allowedTargets'] if item['channel'] != 'desktop']
        assert len(external) == 1
        chosen = external[0]
        index = len(self.requests)
        if index == 1:
            name, arguments = (('get_content_events', {}) if self.mode == 'a01'
                               else ('activate_skill', {'skill_id': 'reading-notes'}))
        elif index == 2:
            arguments = {'message': '从本轮宿主输入选择非默认渠道', 'mood': '', 'thought': '',
                         'account_id': chosen['account_id'], 'target_id': chosen['target_id']}
            if self.mode == 'a01':
                arguments['evidence'] = [json.loads(messages[-1].content[0].text)[0]['id']]
            name = 'message_push'
        else:
            assert index == 3
            name, arguments = (('finish_turn', {'decision': 'reply'}) if self.mode == 'a01' else (
                'finish_drift', {'skill_used': 'reading-notes', 'one_line': '原许可渠道已发送',
                                 'next_action': '下次继续', 'message_result': 'sent'}))
        yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'view-{index}', name, arguments),)))


@contextmanager
def configured(api, monkeypatch, platform, source, mode):
    if mode == 'a01':
        with decision(api, monkeypatch, platform, source) as context:
            yield context
        return
    client, _, _, _ = api
    role, path, now = prepare(client, monkeypatch, extra_tools=('message_push',))
    account = channel_for(main.multichannel_store, role, platform[1])
    config = client.get(path).json()['config']
    _, selected = save_external(client, path, account, config, expected_revision=config['revision'])
    try:
        yield {'client': client, 'role': role, 'account': account, 'selected': selected,
               'path': path, 'now': now, 'app': main._proactive, 'channels': main.multichannel_store}
    finally:
        asyncio.run(main._proactive.close())
        asyncio.run(main.channel_host.stop(account.account_id))


@contextmanager
def unselected_targets(context, platform):
    client, channels, role = context['client'], context['channels'], context['role']
    created = client.post('/api/roles', json={'name': '另一角色目录', 'profile': {'profile': '仅本地目录隔离'}})
    assert created.status_code == 201, created.text
    other_role = created.json()['role']['id']
    observed = client.post(context['path'] + '/target-directory',
                           json={'accountId': context['account'].account_id, 'kind': 'friends'})
    assert observed.status_code == 200, observed.text
    accounts = []
    targets = [item for item in observed.json()['targets'] if item['chatId'] != '101']
    assert len(targets) == 2 and all(not item['ownerEligible'] for item in targets)
    try:
        for owner, account_id, chat in ((other_role, 'other-role-account', '303'),):
            account = channels.register_channel_account(owner, 'qq', account_id=account_id, enabled=True)
            accounts.append(account)
            code = channels.create_pairing_code()
            assert channels.consume_pairing_code(code, role_id=owner, channel='qq', scope='platform',
                platform_user_id=chat, account_id=account_id, chat_id=chat)
            main.channel_host.register(NapCatAdapter(NapCatConfig(account_id), transport=platform[1]))
            response = client.post(f'/api/roles/{owner}/autonomy/target-directory',
                                   json={'accountId': account_id, 'kind': 'friends'})
            assert response.status_code == 200, response.text
            selected = next(item for item in response.json()['targets'] if item['chatId'] == chat)
            assert selected['ownerEligible'] and selected['selectionObservationId']
            targets.append(selected)
        yield accounts, targets
    finally:
        for account in accounts:
            asyncio.run(main.channel_host.stop(account.account_id))


@pytest.mark.parametrize('mode', ['a01', 'drift'])
def test_provider_selects_original_nondefault_pair_without_fixture_id_injection(api, monkeypatch, platform, source, mode):
    _, _, sessions, _ = api
    with configured(api, monkeypatch, platform, source, mode) as context:
        client, path = context['client'], context['path']
        config = client.get(path).json()['config']
        saved = client.put(path, json={'expectedRevision': config['revision'], 'config': {
            **config, 'defaultTargetRef': 'desktop',
            'allowedTargetRefs': ['desktop', context['selected']['targetRef']]}})
        assert saved.status_code == 200, saved.text
        with unselected_targets(context, platform) as (outside_accounts, outside_targets):
            directory_calls = sum(item['action'] == 'get_friend_list' for item in platform[0]['calls'])
            provider = ProviderSelectedTarget(mode)
            monkeypatch.setattr(main, 'model_adapter', provider)
            if mode == 'a01':
                final = asyncio.run(run(context))
            else:
                context['now'][0] += 1800
                final = asyncio.run(settled(context['app'], context['role']))
            assert not provider.missing_view, 'provider did not receive its frozen allowed target pairs'
            assert final['runs'][0]['status'] == ('sent' if mode == 'a01' else 'finished_sent'), final
            assert len(provider.requests) == 3 and provider.views == [provider.views[0]] * 3
            view = provider.views[0]
            assert view['allowedTargets'] == [
                {'ref': 'desktop', 'channel': 'desktop'},
                {'ref': context['selected']['targetRef'], 'channel': 'qq',
                 'account_id': context['account'].account_id, 'target_id': context['selected']['id']}]
            encoded = json.dumps(view, ensure_ascii=False)
            assert all(account.account_id not in encoded for account in outside_accounts)
            assert all(item['id'] not in encoded and (not item['selectionObservationId']
                       or item['selectionObservationId'] not in encoded) for item in outside_targets)
            with context['app']._transaction() as db:
                frozen = json.loads(db.execute('SELECT targets FROM autonomy_target_runs WHERE tick_id=?',
                                               (final['runs'][0]['id'],)).fetchone()[0])
            original = frozen[context['selected']['targetRef']]
            assert original['adapterGeneration'] not in encoded
            assert original['ownerBinding']['identityId'] not in encoded
            assert all(key not in encoded for key in ('ownerBinding', 'pairedAt', 'contextSince', 'credential', 'token', 'adapterGeneration'))
            receipt = final['deliveries'][0]
            assert receipt['status'] == 'sent' and receipt['attemptId'] and receipt['platformMessageId'] == '8301'
            assert receipt['target']['ref'] == context['selected']['targetRef']
            assert len(platform[0]['sent']) == 1 and platform[0]['sent'][0]['user_id'] == 101
            assert sum(item['action'] == 'get_friend_list' for item in platform[0]['calls']) == directory_calls
            thread = context['channels'].list_threads(context['role'])[0]
            messages = context['channels'].list_messages(context['role'], thread.thread_id)[1]
            assert len(messages) == 1 and messages[0].metadata['autonomyIntentId'] == receipt['intentId']
            assert not any(item.content == '从本轮宿主输入选择非默认渠道'
                           for item in sessions.list_messages(f"role:{context['role']}"))
