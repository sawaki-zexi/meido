"""An owner can adopt the same pending bytes; stale previews never grant trust."""
import asyncio
import shutil

import pytest

from backend.app import main
from test_background_shell_api import api as shell_api  # noqa: F401
from test_spawn_api import api  # noqa: F401
from test_proactive_drift_delivery import prepare

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_owner_can_expand_scope_then_adopt_same_hash_and_old_preview_cannot_survive_directory_aba(api, monkeypatch):
    client, roles, _, _ = api
    role, path, _ = prepare(client, monkeypatch)
    try:
        original = client.get(path + '/drift-skills').json()['skills'][0]
        config = client.get(path).json()['config']
        response = client.put(path, json={'expectedRevision': config['revision'], 'config': {**config,
            'driftAuthoring': {**config['driftAuthoring'], 'allowMcp': True}}})
        assert response.status_code == 200, response.text
        assert client.get(path + '/drift-skills').json()['skills'][0]['status'] == 'untrusted'
        preview = client.post(path + '/drift-artifact-preview', json={'skillId': 'reading-notes'})
        assert preview.status_code == 200, preview.text
        value = preview.json()
        assert value['bundleSha256'] == original['bundleSha256']
        body = {'skillId': value['skillId'], 'bundleSha256': value['bundleSha256'], 'previewId': value['previewId'],
            'authoringScopeId': value['scopeId'], 'expectedScopeRevision': value['scopeRevision'], 'expectedRevision': value['configRevision']}
        directory = roles.workspace_path(role) / 'drift/skills/reading-notes'
        prior = directory.parent.parent / 'preserved-old-reading-notes'
        directory.rename(prior)
        shutil.copytree(prior, directory)
        rejected = client.post(path + '/drift-skill-adoptions', json=body)
        assert rejected.status_code == 409, rejected.text
        assert client.get(path + '/drift-skills').json()['skills'][0]['status'] == 'untrusted'
        # No regeneration is required; owner explicitly previews/adopts these same bytes.
        fresh = client.post(path + '/drift-artifact-preview', json={'skillId': 'reading-notes'}).json()
        body['previewId'] = fresh['previewId']
        adopted = client.post(path + '/drift-skill-adoptions', json=body)
        assert adopted.status_code == 200, adopted.text
        skill = next(item for item in adopted.json()['skills'] if item['id'] == 'reading-notes')
        assert skill['bundleSha256'] == original['bundleSha256'] and skill['status'] == 'available'
        assert skill['provenance']['status'] == 'verified'
        assert client.get(path).json()['runs'] == []
    finally:
        asyncio.run(main._proactive.close())
