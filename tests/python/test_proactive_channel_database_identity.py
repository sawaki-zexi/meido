"""Identical database contents cannot authorize a split physical channel store."""
import asyncio
from pathlib import Path
import sqlite3

import pytest

from backend.app import main
from backend.app.channels.host import ChannelHost
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from test_proactive_targets_api import api, shell_api, platform, role_target, save_external  # noqa: F401

pytestmark = pytest.mark.usefixtures('local_platform_only')


class MemoryCredentials:
    def get(self, _key):
        return ''

    def set(self, _key, _value):
        pass


@pytest.mark.parametrize('mismatch', ['channel-store', 'channel-session-store'])
def test_split_physical_channel_database_rejects_directory_save_and_new_tick(api, monkeypatch, platform, tmp_path, mismatch):
    now = [1781000000.0]
    monkeypatch.setattr(main, '_proactive_clock', lambda: now[0])
    with role_target(api, monkeypatch, platform) as (client, role, account, original):
        model = client.post('/api/model/configurations', json={'providerId': 'custom', 'provider': 'local',
            'baseUrl': 'http://127.0.0.1:1/v1', 'model': 'local-only', 'apiKey': 'fixture'}).json()['configuration']
        path = f'/api/roles/{role}/autonomy'
        saved, selected = save_external(client, path, account, {
            'activeDecisionEnabled': True, 'decisionModelConfigurationId': model['id'], 'consentRevision': 1,
        })
        assert saved['targetGrants'][0]['current'] is True
        assert saved['runs'] == [] and saved['deliveries'] == []
        binding = original.owner_target_binding(role, account.account_id, '101')
        revision = original.directory_revision(account.account_id, account.channel)
        calls_before = list(platform[0]['calls'])
        assert [item['action'] for item in calls_before] == ['get_friend_list']

        asyncio.run(main._proactive.close())
        asyncio.run(main.channel_host.stop(account.account_id))
        original_path = Path(main.session_store.database_path).resolve()
        foreign_path = tmp_path / 'foreign-channel' / 'sessions.db'
        foreign_path.parent.mkdir()
        # Copy actual fixture data to isolate physical identity from account,
        # directory, paired-owner and saved-grant changes.
        with sqlite3.connect(original_path.as_uri() + '?mode=ro', uri=True) as source_db:
            with sqlite3.connect(foreign_path) as target_db:
                source_db.backup(target_db)
        foreign_sessions = SessionStore(foreign_path)
        injected_path = foreign_path if mismatch == 'channel-store' else original_path
        injected = MultichannelStore(injected_path, foreign_sessions, MemoryCredentials())
        assert injected.owner_target_binding(role, account.account_id, '101') == binding
        assert injected.directory_revision(account.account_id, account.channel) == revision
        assert injected.get_account(account.account_id) == original.get_account(account.account_id)
        assert Path(injected.sessions.database_path).resolve() != original_path
        assert (Path(injected.database_path).resolve() != original_path) is (mismatch == 'channel-store')

        host = ChannelHost(injected)
        host.register(NapCatAdapter(NapCatConfig(account.account_id), transport=platform[1]))
        monkeypatch.setattr(main, 'multichannel_store', injected)
        monkeypatch.setattr(main, 'channel_host', host)
        monkeypatch.setattr(main, '_proactive', None)

        facts = client.get(path + '/target-accounts')
        assert facts.status_code == 200, facts.text
        assert facts.json()['accounts'][0]['accountId'] == account.account_id
        directory = client.post(path + '/target-directory', json={'accountId': account.account_id, 'kind': 'friends'})
        assert directory.status_code == 409, directory.text
        assert directory.json()['complete'] is False and directory.json()['error']
        refused_save = client.put(path, json={'expectedRevision': 1, 'config': saved['config']})
        assert refused_save.status_code == 422, refused_save.text

        application = main._proactive
        now[0] += 1800
        assert asyncio.run(application.on_clock()) == 0
        state = client.get(path).json()
        assert state['config']['revision'] == 1
        assert state['config']['defaultTargetRef'] == selected['targetRef']
        assert state['admissionError'] and state['runs'] == [] and state['deliveries'] == []
        assert platform[0]['calls'] == calls_before and platform[0]['sent'] == []
        assert original.list_deliveries(role) == [] and injected.list_deliveries(role) == []
        with sqlite3.connect((api[3].root / 'tasks.db').as_uri() + '?mode=ro', uri=True) as task_db:
            assert task_db.execute('SELECT COUNT(*) FROM tasks WHERE role_id=?', (role,)).fetchone()[0] == 0
        with sqlite3.connect(original_path.as_uri() + '?mode=ro', uri=True) as business_db:
            assert business_db.execute('SELECT COUNT(*) FROM autonomy_runs WHERE role_id=?', (role,)).fetchone()[0] == 0
            assert business_db.execute('SELECT COUNT(*) FROM autonomy_storage_reservations WHERE role_id=?', (role,)).fetchone()[0] == 0
            assert business_db.execute('SELECT COALESCE(SUM(decisions),0) FROM autonomy_budgets WHERE role_id=?', (role,)).fetchone()[0] == 0
