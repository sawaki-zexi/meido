"""Owner gallery HTTP: real bounded images, isolated filesystem and no model grants."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from test_chat_meme_library import real_library, picture, upload
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.material_library import OwnerMaterialLibrary
from backend.app.material_api import material_router


@pytest.fixture
def api(tmp_path):
    chat = real_library(tmp_path)
    service = OwnerMaterialLibrary(chat, lambda role: object() if role in {'alice', 'bob'} else None)
    (chat.library.root('bob').parent).mkdir(parents=True)
    app = FastAPI()
    app.include_router(material_router(lambda: service))
    with TestClient(app) as client:
        yield client, service, chat


def current(client, role='alice'):
    response = client.get(f'/api/roles/{role}/materials')
    assert response.status_code == 200, response.text
    return response.json()


def put(client, *, raw=None, format='PNG', revision=None, category='开心', description='合成表情'):
    suffix, mime = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'),
                    'WEBP': ('webp', 'image/webp'), 'GIF': ('gif', 'image/gif')}[format]
    return client.post('/api/roles/alice/materials', content=raw if raw is not None else picture(format),
        params={'category': category, 'description': description,
                'expectedRevision': revision or current(client)['revision']},
        headers={'Content-Type': mime, 'x-material-filename': 'example.' + suffix})


@pytest.mark.parametrize('format', ['PNG', 'JPEG', 'WEBP', 'GIF'])
def test_explicit_owner_import_preview_and_stable_listing_without_tool_grants(api, format):
    client, service, chat = api
    original = current(client)
    assert original['materials'] == []
    response = put(client, format=format)
    assert response.status_code == 200, response.text
    result = response.json()
    item = result['material']
    assert item['format'] == format and item['source'] == 'owner_import'
    assert item['width'] == 4 and item['height'] == 3
    assert item['name'] == 'example.' + {'JPEG': 'jpg'}.get(format, format.lower())
    assert item['animated'] is (format == 'GIF')
    assert result['revision'] != original['revision']
    listed = current(client)
    assert listed['materials'] == [item]
    assert str(chat.library.root('alice')) not in json.dumps(listed)
    assert current(client)['revision'] == listed['revision']
    preview = client.get(item['imageUrl'])
    assert preview.status_code == 200 and preview.content == picture(format)
    assert preview.headers['content-type'] == item['mediaType']
    assert preview.headers['cache-control'] == 'private, no-store'
    assert not chat.library._grant('alice').enabled
    assert not (chat.attachment_root('alice') / 'attachments').exists()


def test_legacy_disabled_chat_and_drift_images_share_one_gallery(api):
    client, service, chat = api
    context = SimpleNamespace(role_id='alice', session_key='channel:qq:group', run_id='run',
        tool_call_id='call', signal=CancellationToken())
    chat.save('alice', upload(chat), 'chat', 'QQ 收录', context, lambda: True)
    root = chat.library.root('alice')
    manifest = root / 'manifest.json'
    document = json.loads(manifest.read_text())
    for category, color in [('legacy', 'green'), ('drift', 'blue')]:
        (root / category).mkdir()
        (root / category / 'old.png').write_bytes(picture(color=color))
        document['categories'][category] = {'desc': category, 'aliases': ['别名'], 'enabled': False}
    manifest.write_text(json.dumps(document))
    private = root.parent / '.meido-memes' / 'metadata'
    private.mkdir()
    import hashlib
    (private / 'old.json').write_text(json.dumps({'roleId': 'alice', 'path': 'drift/old.png',
        'sha256': hashlib.sha256(picture(color='blue')).hexdigest()}))
    listing = current(client)
    assert {x['source'] for x in listing['materials']} == {'chat', 'legacy', 'drift'}
    assert len(listing['materials']) == 3
    assert len(client.get('/api/roles/alice/materials', params={'query': '别名'}).json()['materials']) == 2
    assert len(client.get('/api/roles/alice/materials', params={'category': 'legacy'}).json()['materials']) == 1
    assert all(client.get(x['imageUrl']).status_code == 200 for x in listing['materials'])


def test_dedup_preserves_prior_description_category_and_revision(api):
    client, _, _ = api
    first = put(client).json()
    second = put(client, category='不同类别', description='不要覆盖').json()
    assert second['deduplicated'] is True
    assert second['material'] == first['material']
    assert second['revision'] == first['revision']
    assert len(current(client)['materials']) == 1


def test_remove_is_recoverable_invalidates_candidates_and_preserves_history(api):
    client, _, chat = api
    first = put(client).json()
    candidate, = chat.candidates('alice')
    context = SimpleNamespace(role_id='alice', session_key='chat', run_id='run', tool_call_id='ref', signal=CancellationToken())
    attachment, raw = chat.reference('alice', candidate, context)
    response = client.delete('/api/roles/alice/materials/' + first['material']['id'],
        params={'expectedRevision': current(client)['revision']})
    assert response.status_code == 200, response.text
    assert response.json()['recoverable'] is True
    assert current(client)['materials'] == []
    assert not chat.valid('alice', candidate)
    assert Path(attachment['path']).read_bytes() == raw
    assert client.get(first['material']['imageUrl']).status_code == 404
    recovered = list((chat.library.root('alice').parent / '.meido-memes' / 'trash').glob('*/*.*'))
    assert any(path.read_bytes() == raw for path in recovered)


def test_stale_mutations_and_cross_role_or_traversal_ids_are_rejected(api):
    client, _, _ = api
    old = current(client)['revision']
    item = put(client).json()['material']
    assert put(client, raw=picture(color='green'), revision=old).status_code == 409
    assert client.delete('/api/roles/alice/materials/' + item['id'], params={'expectedRevision': old}).status_code == 409
    assert client.get('/api/roles/bob/materials/' + item['id'] + '/image').status_code == 404
    assert client.get('/api/roles/missing/materials').status_code == 404
    for forged in ['a'*32, '%2E%2E%5Csecret', '%2Fetc%2Fpasswd']:
        assert client.get('/api/roles/alice/materials/' + forged + '/image').status_code in {404, 422}
    assert len(current(client)['materials']) == 1


def test_invalid_images_mime_and_categories_never_publish(api):
    client, _, chat = api
    for raw in [b'not an image', b'GIF89a corrupt', b'\x89PNG\r\n\x1a\nbroken']:
        assert put(client, raw=raw).status_code == 415
    for category in ['../outside', '/absolute', 'a\\b', '.', '.hidden']:
        assert put(client, category=category).status_code == 422
    assert put(client, description='').status_code == 422
    assert put(client, raw=b'x' * (10 * 1024 * 1024 + 1)).status_code == 413
    assert current(client)['materials'] == []
    assert not (chat.library.root('alice').parent / 'attachments').exists()


def test_file_and_root_symlinks_never_preview_or_remove_foreign_bytes(api, tmp_path):
    client, _, chat = api
    item = put(client).json()['material']
    candidate, = chat.candidates('alice')
    foreign = tmp_path / 'foreign.png'
    foreign.write_bytes(picture(color='green'))
    candidate.path.unlink()
    candidate.path.symlink_to(foreign)
    assert client.get(item['imageUrl']).status_code == 404
    assert client.delete('/api/roles/alice/materials/' + item['id'],
        params={'expectedRevision': current(client)['revision']}).status_code == 404
    assert foreign.read_bytes() == picture(color='green')
    root = chat.library.root('alice')
    root.rename(root.with_name('original-memes'))
    root.symlink_to(root.with_name('original-memes'), target_is_directory=True)
    assert client.get('/api/roles/alice/materials').status_code == 503
    assert put(client, revision='a'*64).status_code == 503


def test_two_mutations_from_same_observation_cannot_both_publish(api):
    client, _, _ = api
    revision = current(client)['revision']
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda color: put(client, raw=picture(color=color), revision=revision), ['red', 'green']))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(current(client)['materials']) == 1


def test_preview_uses_original_observation_not_a_full_library_rescan(api, monkeypatch):
    client, service, _ = api
    first = put(client).json()['material']
    put(client, raw=picture(color='green'))
    def forbidden(*args, **kwargs):
        raise AssertionError('preview must not decode or scan unrelated images')
    monkeypatch.setattr(service, '_snapshot', forbidden)
    monkeypatch.setattr(service.chat, '_items', forbidden)
    response = client.get(first['imageUrl'])
    assert response.status_code == 200 and response.content == picture()


def test_preview_cache_is_shared_but_never_authority_for_replaced_root(api):
    client, service, chat = api
    item = put(client).json()['material']
    fresh_service = OwnerMaterialLibrary(chat, service.role_getter)
    assert fresh_service.image('alice', item['id'])[0] == picture()
    root = chat.library.root('alice')
    root.rename(root.with_name('previous-memes'))
    root.mkdir()
    (root / '开心').mkdir()
    (root / 'manifest.json').write_bytes((root.with_name('previous-memes') / 'manifest.json').read_bytes())
    (root / '开心' / 'replacement.png').write_bytes(picture())
    assert client.get(item['imageUrl']).status_code == 404
    assert current(client)['materials'][0]['id'] != item['id']


def test_preview_revalidates_original_after_process_observations_are_lost(api):
    client, service, _ = api
    item = put(client).json()['material']
    # An already open gallery retains its URL across a backend restart or cache
    # eviction. Retrying that original must not require an unrelated list call.
    service._observations.clear()
    preview = client.get(item['imageUrl'])
    assert preview.status_code == 200 and preview.content == picture()
    service._observations.clear()
    assert client.get('/api/roles/bob/materials/' + item['id'] + '/image').status_code == 404


def test_delete_rechecks_replaced_file_under_shared_lock(api, monkeypatch):
    from contextlib import contextmanager
    client, service, chat = api
    first = put(client).json()
    candidate, = chat.candidates('alice')
    original = service._locks
    @contextmanager
    def changed(*args, **kwargs):
        candidate.path.write_bytes(picture(color='green'))
        with original(*args, **kwargs):
            yield
    monkeypatch.setattr(service, '_locks', changed)
    response = client.delete('/api/roles/alice/materials/' + first['material']['id'],
        params={'expectedRevision': first['revision']})
    assert response.status_code == 409
    assert candidate.path.read_bytes() == picture(color='green')
    assert not list((chat.library.root('alice').parent / '.meido-memes' / 'trash').glob('*/original.*'))


@pytest.fixture
def managed_api(tmp_path):
    chat = real_library(tmp_path, managed=True)
    service = OwnerMaterialLibrary(chat, lambda role: object() if role == 'alice' else None)
    app = FastAPI()
    app.include_router(material_router(lambda: service))
    with TestClient(app) as client:
        yield client, service, chat


def test_managed_import_remove_share_revision_and_quota_without_grant_changes(managed_api):
    client, _, chat = managed_api
    grant = chat.library._grant('alice')
    first = put(client)
    assert first.status_code == 200, first.text
    with chat.library.app._transaction() as db:
        assert db.execute('SELECT revision FROM autonomy_meme_roots').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM autonomy_meme_reservations').fetchone()[0] == 0
    result = first.json()
    response = client.delete('/api/roles/alice/materials/' + result['material']['id'],
        params={'expectedRevision': result['revision']})
    assert response.status_code == 200, response.text
    with chat.library.app._transaction() as db:
        assert db.execute('SELECT revision FROM autonomy_meme_roots').fetchone()[0] == 2
    assert chat.library._grant('alice') == grant
    assert chat.library.storage.scan('alice')[0] > len(picture())  # trash remains charged


def test_managed_quota_rejects_new_import_before_image_publication(managed_api):
    from backend.app.proactive_runtime.drift_workspace import FileLimits
    client, _, chat = managed_api
    chat.library.app.drift_files.limits = FileLimits(role_bytes=4096)
    response = put(client)
    assert response.status_code == 413, response.text
    assert current(client)['materials'] == []
    assert not list(chat.library.root('alice').rglob('*.png'))


def test_interrupted_removal_retains_original_and_recovery_does_not_replay(managed_api, monkeypatch):
    from backend.app import material_library as module
    client, _, chat = managed_api
    first = put(client).json()
    candidate, = chat.candidates('alice')
    original = module.os.replace
    def interrupted(source, target):
        original(source, target)
        if Path(target).parent.parent.name == 'trash':
            raise OSError('simulated response-lost-after-atomic-move')
    monkeypatch.setattr(module.os, 'replace', interrupted)
    response = client.delete('/api/roles/alice/materials/' + first['material']['id'],
        params={'expectedRevision': first['revision']})
    assert response.status_code == 503
    assert current(client)['materials'] == []
    retained, = (chat.library.root('alice').parent / '.meido-memes' / 'trash').glob('*/original.*')
    assert retained.read_bytes() == picture()
    receipt = json.loads((retained.parent / 'receipt.json').read_text())
    assert receipt['roleId'] == 'alice' and receipt['originalRelativePath'] == candidate.path.relative_to(chat.library.root('alice')).as_posix()
    candidate.path.write_bytes(picture(color='green'))  # later owner edit must survive recovery
    chat.library.reconcile('alice')
    assert candidate.path.read_bytes() == picture(color='green')
    with chat.library.app._transaction() as db:
        assert db.execute('SELECT revision FROM autonomy_meme_roots').fetchone()[0] == 2


def test_actual_main_router_uses_existing_role_workspace_and_no_model_grants(tmp_path, monkeypatch):
    from backend.app import main
    from backend.app.models import RoleInput
    from backend.app.role_store import RoleStore
    roles = RoleStore(tmp_path / 'real-roles')
    role = roles.create(RoleInput(name='本地合成角色', profile={'profile': '本地合成测试'}, agentConfig={'enabledTools': []}))
    chat = real_library(tmp_path / 'internal')
    chat.library.app.drift_skills.workspace = roles.workspace_path
    chat.library.app.role_getter = roles.get
    monkeypatch.setattr(main, 'store', roles)
    monkeypatch.setattr(main, '_proactive_application', lambda: SimpleNamespace(memes=chat.library))
    client = TestClient(main.app)
    url = f'/api/roles/{role.id}/materials'
    revision = client.get(url).json()['revision']
    response = client.post(url, content=picture('GIF'), params={'category': '导入', 'description': 'animated', 'expectedRevision': revision},
        headers={'Content-Type': 'image/gif', 'x-material-filename': 'example.gif'})
    assert response.status_code == 200, response.text
    assert client.get(response.json()['material']['imageUrl']).content == picture('GIF')
    assert len(list((roles.workspace_path(role.id) / 'memes' / '导入').glob('*.gif'))) == 1
    assert roles.get(role.id).agentConfig.enabledTools == []
    assert chat.library._grant(role.id).enabled is False


def test_excessive_animation_frames_are_rejected_without_publication(api):
    client, _, _ = api
    output = io.BytesIO()
    frames = [Image.new('RGB', (2, 2), (frame, 255-frame, frame // 2)) for frame in range(201)]
    frames[0].save(output, 'GIF', save_all=True, append_images=frames[1:], duration=10, loop=0)
    assert Image.open(io.BytesIO(output.getvalue())).n_frames == 201
    response = put(client, raw=output.getvalue(), format='GIF')
    assert response.status_code == 415
    assert current(client)['materials'] == []


def test_managed_remove_quota_checks_before_creating_trash(managed_api):
    from backend.app.proactive_runtime.drift_workspace import FileLimits
    client, _, chat = managed_api
    first = put(client).json()
    chat.library.app.drift_files.limits = FileLimits(role_items=chat.library.storage.scan('alice')[1])
    response = client.delete('/api/roles/alice/materials/' + first['material']['id'],
        params={'expectedRevision': first['revision']})
    assert response.status_code == 413
    assert not (chat.library.root('alice').parent / '.meido-memes' / 'trash').exists()
    assert current(client)['materials'] == [first['material']]


def test_rebuilt_library_reconciles_move_without_replaying_old_removal(managed_api, monkeypatch):
    from backend.app import material_library as module
    from backend.app.proactive_runtime.meme_library import MemeLibrary
    client, _, chat = managed_api
    first = put(client).json()
    candidate, = chat.candidates('alice')
    original = module.os.replace
    def interrupted(source, target):
        original(source, target)
        if Path(target).parent.parent.name == 'trash':
            raise OSError('simulated crash after physical move before journal commit')
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'replace', interrupted)
        patch.setattr(module, 'reconcile_removal', lambda *args: None)
        response = client.delete('/api/roles/alice/materials/' + first['material']['id'],
            params={'expectedRevision': first['revision']})
        assert response.status_code == 503
    with chat.library.app._transaction() as db:
        assert db.execute("SELECT status FROM autonomy_meme_operations WHERE json_extract(planned, '$.kind')='owner_ui_remove'").fetchone()[0] == 'reserved'
    candidate.path.write_bytes(picture(color='green'))
    rebuilt = MemeLibrary(chat.library.app)
    rebuilt.reconcile('alice')
    assert candidate.path.read_bytes() == picture(color='green')
    retained, = (chat.library.root('alice').parent / '.meido-memes' / 'trash').glob('*/original.*')
    assert retained.read_bytes() == picture()
    with rebuilt.app._transaction() as db:
        assert db.execute('SELECT revision FROM autonomy_meme_roots').fetchone()[0] == 2
        assert db.execute("SELECT status FROM autonomy_meme_operations WHERE json_extract(planned, '$.kind')='owner_ui_remove'").fetchone()[0] == 'reconciled'
        assert db.execute('SELECT COUNT(*) FROM autonomy_meme_reservations').fetchone()[0] == 0


@pytest.mark.parametrize('phase', ['prepared', 'first_published'])
def test_recovery_cannot_retire_live_import_stages_or_quota(managed_api, monkeypatch, phase):
    from backend.app.channels.meme_library import _OwnerPublication
    client, _, chat = managed_api
    original = getattr(_OwnerPublication, 'prepare' if phase == 'prepared' else 'publish')
    checked = []
    def intercepted(publication, *args):
        result = original(publication, *args)
        if checked:
            return result
        checked.append(True)
        stages = {path: path.read_bytes() for path in (publication.private / 'staging').glob('*.tmp')}
        chat.library.reconcile('alice')
        assert stages and all(path.read_bytes() == raw for path, raw in stages.items())
        with chat.library.app._transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM autonomy_meme_reservations').fetchone()[0] == 1
            assert db.execute('SELECT status FROM autonomy_meme_operations WHERE operation_id=?', (publication.operation,)).fetchone()[0] not in {'reconciled', 'recorded'}
        return result
    monkeypatch.setattr(_OwnerPublication, 'prepare' if phase == 'prepared' else 'publish', intercepted)
    response = put(client)
    assert response.status_code == 200, response.text
    assert checked and len(current(client)['materials']) == 1
    assert chat.library._owner_material_runs == set()


def test_fresh_owner_retry_adopts_exact_interrupted_original_without_overwrite(managed_api, monkeypatch):
    from backend.app.channels.meme_library import _OwnerPublication
    client, _, chat = managed_api
    original = _OwnerPublication.publish
    def interrupted(publication, path):
        if path.name == 'manifest.json':
            raise OSError('crash before new category manifest publication')
        return original(publication, path)
    with monkeypatch.context() as patch:
        patch.setattr(_OwnerPublication, 'publish', interrupted)
        failed = put(client)
        assert failed.status_code == 503
    root = chat.library.root('alice')
    orphan, = root.rglob('*.png')
    identity = orphan.stat().st_ino
    assert orphan.read_bytes() == picture() and current(client)['materials'] == []
    response = put(client)
    assert response.status_code == 200, response.text
    assert response.json()['deduplicated'] is True
    assert orphan.stat().st_ino == identity and orphan.read_bytes() == picture()
    assert len(current(client)['materials']) == 1
    assert list(root.rglob('*.png')) == [orphan]


def test_owner_retry_never_overwrites_different_unindexed_original(api):
    import hashlib
    client, _, chat = api
    root = chat.library.root('alice')
    (root / '开心').mkdir(parents=True)
    target = root / '开心' / (hashlib.sha256(picture()).hexdigest() + '.png')
    target.write_bytes(picture(color='green'))
    response = put(client)
    assert response.status_code == 409
    assert target.read_bytes() == picture(color='green')
    assert current(client)['materials'] == []


def test_retention_quota_failure_is_bounded_http_error(api, monkeypatch):
    from backend.app.proactive_runtime import retention
    client, _, _ = api
    def exhausted(*args):
        raise retention.StorageLimitError('fixture internal storage location must not leak')
    monkeypatch.setattr(retention, 'check_capacity', exhausted)
    response = put(client)
    assert response.status_code == 413
    assert 'fixture internal' not in response.text
    assert current(client)['materials'] == []


from test_proactive_configuration import system  # noqa: E402,F401


def test_real_application_recover_leaves_paused_owner_publication_owned(system, tmp_path, monkeypatch):
    import asyncio
    import threading
    from backend.app.channels.meme_library import ChatMemeLibrary, _OwnerPublication
    app, sessions, _, _, _ = system
    app.query('role')
    workspace = tmp_path / 'owner-role' / 'workspace'
    workspace.mkdir(parents=True)
    app.drift_skills.workspace_getter = lambda role: workspace
    grant = SimpleNamespace(enabled=True, managedEpoch='test-owner-epoch')
    monkeypatch.setattr(app.drift_skills, 'configuration', lambda role: SimpleNamespace(driftMeme=grant))
    with app._transaction() as db:
        app.memes.adopt(db, 'role', grant)
    service = OwnerMaterialLibrary(ChatMemeLibrary(app.memes, lambda role: workspace), app.role_getter)
    revision = service.list('role')['revision']
    started, release = threading.Event(), threading.Event()
    original = _OwnerPublication.prepare
    def paused(publication, check):
        original(publication, check)
        started.set()
        assert release.wait(10), 'test must release paused owner'
    monkeypatch.setattr(_OwnerPublication, 'prepare', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.import_image, 'role', picture(), 'image.png', 'image/png', 'happy', 'owner', revision)
        try:
            assert started.wait(5)
            assert sessions.active_run('role') is None and app._executors == {}
            stages = {path: path.read_bytes() for path in (workspace / '.meido-memes' / 'staging').glob('*.tmp')}
            assert len(stages) == 3
            asyncio.run(app.recover())
            assert all(path.read_bytes() == raw for path, raw in stages.items())
            with app._transaction() as db:
                assert db.execute('SELECT COUNT(*) FROM autonomy_meme_reservations').fetchone()[0] == 1
                assert db.execute('SELECT status FROM autonomy_meme_operations').fetchone()[0] == 'files_ready'
        finally:
            release.set()
        assert future.result(timeout=5)['material']['source'] == 'owner_import'
    asyncio.run(app.recover())
    assert len(service.list('role')['materials']) == 1
    assert app.memes._owner_material_runs == set()


def test_recovery_cannot_retire_live_remove_receipt_or_reservation(managed_api, monkeypatch):
    from contextlib import contextmanager
    client, service, chat = managed_api
    first = put(client).json()
    original = service._locks
    checked = []
    @contextmanager
    def paused(*args, **kwargs):
        chat.library.reconcile('alice')
        with chat.library.app._transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM autonomy_meme_reservations').fetchone()[0] == 1
            assert db.execute("SELECT status FROM autonomy_meme_operations WHERE json_extract(planned, '$.kind')='owner_ui_remove'").fetchone()[0] == 'reserved'
        checked.append(True)
        with original(*args, **kwargs):
            yield
    monkeypatch.setattr(service, '_locks', paused)
    response = client.delete('/api/roles/alice/materials/' + first['material']['id'],
        params={'expectedRevision': first['revision']})
    assert response.status_code == 200, response.text
    assert checked and chat.library._owner_material_runs == set()
    assert current(client)['materials'] == []
