"""Observed owner targets never inherit replacement identities or stale authority."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.proactive_runtime import targets as target_runtime
from test_proactive_targets_api import (  # noqa: F401
    api, platform, role_target, save_external, shell_api,
)

pytestmark = pytest.mark.usefixtures('local_platform_only')


def observe(client, path, account):
    response = client.post(path + '/target-directory', json={
        'accountId': account.account_id, 'kind': 'friends',
    })
    assert response.status_code == 200, response.text
    directory = response.json()
    selected = next(item for item in directory['targets'] if item['chatId'] == '101')
    assert selected['ownerEligible'] and selected['selectionObservationId']
    return directory, selected


def selection_body(directory, selected, account, *, revision=0):
    return {
        'expectedRevision': revision,
        'config': {
            'defaultTargetRef': selected['targetRef'],
            'allowedTargetRefs': [selected['targetRef']],
        },
        'targetSelections': [{
            'accountId': account.account_id,
            'targetId': selected['id'],
            'selectionObservationId': selected['selectionObservationId'],
            'expectedDirectoryRevision': directory['directoryRevision'],
        }],
    }


def repair_owner(client, role_id, account, channels):
    identities = client.get('/api/channels/identities').json()['identities']
    identity = next(item for item in identities if item['platformUserId'] == '101')
    assert client.delete('/api/channels/identities/' + identity['identityId']).status_code == 204
    pairing = client.post(f'/api/roles/{role_id}/channels/qq/pairing')
    assert pairing.status_code == 200, pairing.text
    assert channels.consume_pairing_code(pairing.json()['code'], role_id=role_id,
        channel='qq', scope='platform', platform_user_id='101',
        account_id=account.account_id, chat_id='101')
    replacement = channels.owner_target_binding(role_id, account.account_id, '101')
    assert replacement is not None and replacement['identityId'] != identity['identityId']


def assert_no_run_or_send(client, path, platform, *, revision):
    response = client.get(path)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state['config']['revision'] == revision
    assert state['runs'] == [] and state['deliveries'] == []
    assert platform[0]['sent'] == []
    assert all(item['action'] == 'get_friend_list' for item in platform[0]['calls'])
    return state


def test_repairing_owner_rejects_original_selection_even_when_directory_revision_is_unchanged(api, monkeypatch, platform):
    with role_target(api, monkeypatch, platform) as (client, role, account, channels):
        path = f'/api/roles/{role}/autonomy'
        directory, selected = observe(client, path, account)
        revision = channels.directory_revision(account.account_id, account.channel)
        repair_owner(client, role, account, channels)
        assert channels.directory_revision(account.account_id, account.channel) == revision

        response = client.put(path, json=selection_body(directory, selected, account))
        assert response.status_code == 409, response.text
        state = assert_no_run_or_send(client, path, platform, revision=0)
        assert state['config']['allowedTargetRefs'] == ['desktop']

        saved, fresh = save_external(client, path, account, {})
        assert fresh['selectionObservationId'] != selected['selectionObservationId']
        assert saved['config']['revision'] == 1 and saved['targetGrants'][0]['current'] is True
        assert_no_run_or_send(client, path, platform, revision=1)


def test_saving_other_settings_does_not_rebind_an_invalid_saved_owner_grant(api, monkeypatch, platform):
    with role_target(api, monkeypatch, platform) as (client, role, account, channels):
        path = f'/api/roles/{role}/autonomy'
        saved, selected = save_external(client, path, account, {})
        repair_owner(client, role, account, channels)
        state = assert_no_run_or_send(client, path, platform, revision=1)
        assert state['config']['defaultTargetRef'] == selected['targetRef']
        assert state['targetGrants'][0]['current'] is False
        assert state['targetGrants'][0]['denial']

        response = client.put(path, json={'expectedRevision': 1, 'config': saved['config']})
        assert response.status_code == 409, response.text
        invalid = assert_no_run_or_send(client, path, platform, revision=1)
        assert invalid['config']['defaultTargetRef'] == selected['targetRef']
        assert invalid['targetGrants'][0]['current'] is False

        restored, _ = save_external(client, path, account, saved['config'], expected_revision=1)
        assert restored['config']['revision'] == 2
        assert restored['targetGrants'][0]['current'] is True
        assert_no_run_or_send(client, path, platform, revision=2)


def test_replacing_adapter_requires_a_new_observation_before_first_grant(api, monkeypatch, platform):
    with role_target(api, monkeypatch, platform) as (client, role, account, channels):
        path = f'/api/roles/{role}/autonomy'
        directory, selected = observe(client, path, account)
        revision = channels.directory_revision(account.account_id, account.channel)
        asyncio.run(main.channel_host.stop(account.account_id))
        main.channel_host.unregister(account.account_id)
        main.channel_host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=platform[1]))
        assert channels.directory_revision(account.account_id, account.channel) == revision

        stale = client.put(path, json=selection_body(directory, selected, account))
        assert stale.status_code == 409, stale.text
        assert_no_run_or_send(client, path, platform, revision=0)
        saved, fresh = save_external(client, path, account, {})
        assert fresh['selectionObservationId'] != selected['selectionObservationId']
        assert saved['targetGrants'][0]['current'] is True
        assert_no_run_or_send(client, path, platform, revision=1)


def test_another_role_cannot_reuse_an_observed_owner_target(api, monkeypatch, platform):
    with role_target(api, monkeypatch, platform) as (client, role, account, _):
        path = f'/api/roles/{role}/autonomy'
        directory, selected = observe(client, path, account)
        other = client.post('/api/roles', json={
            'name': '另一角色', 'profile': {'profile': '不继承其他角色的目标选择'},
        }).json()['role']['id']
        other_path = f'/api/roles/{other}/autonomy'

        response = client.put(other_path, json=selection_body(directory, selected, account))
        assert response.status_code == 409, response.text
        assert_no_run_or_send(client, other_path, platform, revision=0)
        assert_no_run_or_send(client, path, platform, revision=0)
        saved = client.put(path, json=selection_body(directory, selected, account))
        assert saved.status_code == 200, saved.text
        assert saved.json()['targetGrants'][0]['current'] is True
        assert_no_run_or_send(client, path, platform, revision=1)


def test_turning_account_off_and_on_does_not_restore_an_old_observation(api, monkeypatch, platform):
    with role_target(api, monkeypatch, platform) as (client, role, account, channels):
        path = f'/api/roles/{role}/autonomy'
        directory, selected = observe(client, path, account)
        revision = channels.directory_revision(account.account_id, account.channel)
        channels.set_account_state(account.account_id, enabled=False)
        channels.set_account_state(account.account_id, enabled=True)
        assert channels.directory_revision(account.account_id, account.channel)[0] > revision[0]

        response = client.put(path, json=selection_body(directory, selected, account))
        assert response.status_code == 409, response.text
        assert_no_run_or_send(client, path, platform, revision=0)
        saved, _ = save_external(client, path, account, {})
        assert saved['targetGrants'][0]['current'] is True
        assert_no_run_or_send(client, path, platform, revision=1)


def test_expired_selection_requires_explicit_directory_observation_again(api, monkeypatch, platform):
    with role_target(api, monkeypatch, platform) as (client, role, account, _):
        path = f'/api/roles/{role}/autonomy'
        directory, selected = observe(client, path, account)
        later = target_runtime.time.monotonic() + 61
        monkeypatch.setattr(target_runtime, 'time', SimpleNamespace(monotonic=lambda: later))

        response = client.put(path, json=selection_body(directory, selected, account))
        assert response.status_code == 409, response.text
        assert_no_run_or_send(client, path, platform, revision=0)
        saved, fresh = save_external(client, path, account, {})
        assert fresh['selectionObservationId'] != selected['selectionObservationId']
        assert saved['targetGrants'][0]['current'] is True
        assert_no_run_or_send(client, path, platform, revision=1)


def test_configuration_revision_change_invalidates_old_selection_without_sending(api, monkeypatch, platform):
    with role_target(api, monkeypatch, platform) as (client, role, account, _):
        path = f'/api/roles/{role}/autonomy'
        directory, selected = observe(client, path, account)
        changed = client.put(path, json={'expectedRevision': 0, 'config': {}})
        assert changed.status_code == 200, changed.text
        assert changed.json()['config']['revision'] == 1

        response = client.put(path, json=selection_body(directory, selected, account, revision=1))
        assert response.status_code == 409, response.text
        assert_no_run_or_send(client, path, platform, revision=1)
        saved, _ = save_external(client, path, account, {}, expected_revision=1)
        assert saved['config']['revision'] == 2 and saved['targetGrants'][0]['current'] is True
        assert_no_run_or_send(client, path, platform, revision=2)
