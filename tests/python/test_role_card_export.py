import base64
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from backend.app import main
from backend.app import role_card_export as exporter
from backend.app.models import RoleInput, RoleProfile, RoleUpdateInput
from backend.app.role_store import RoleStore
from backend.app.role_card_import import parse_role_card


def image_bytes(color='red', *, metadata=False, size=(4, 3)):
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text('chara', 'PRIVATE_OLD_CARD')
        info.add_text('Comment', 'PRIVATE_IMAGE_NOTE')
    Image.new('RGB', size, color).save(output, format='PNG', pnginfo=info)
    return output.getvalue()


@pytest.fixture
def saved_role(tmp_path, monkeypatch):
    store = RoleStore(tmp_path / 'roles')
    role = store.create(RoleInput(name='爱丽丝', description='雨夜的女仆', profile=RoleProfile(
        profile='{{char}}帮助{{user}}', personality='细心', behaviorRules='先核对事实',
        responseConstraints='保持简洁', nickname='主人')))
    monkeypatch.setattr(main, 'store', store)
    return store, role, TestClient(main.app)


def read_card(raw, format):
    if format == 'json':
        return json.loads(raw)
    if format == 'png':
        with Image.open(io.BytesIO(raw)) as image:
            return json.loads(base64.b64decode(image.info['ccv3']))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return json.loads(archive.read('card.json'))


@pytest.mark.parametrize('format', ['json', 'png', 'charx'])
def test_download_roundtrips_all_saved_fields_and_returns_same_artifact_summary(saved_role, format):
    store, role, client = saved_role
    before = store.manifest_path.read_bytes()
    response = client.get(f'/api/roles/{role.id}/export', params={'format': format})
    assert response.status_code == 200, response.text
    assert 'attachment;' in response.headers['content-disposition']
    assert response.headers['cache-control'] == 'no-store'
    summary = json.loads(base64.b64decode(response.headers['x-meido-export-summary']))
    assert summary['format'] == format
    imported = client.post('/api/role-card-import/preview', content=response.content,
                           headers={'x-role-card-filename': f'export.{format}', 'content-type': 'application/octet-stream'})
    assert imported.status_code == 200, imported.text
    preview = imported.json()
    assert preview['draft'] == {'name': role.name, 'description': role.description, **role.profile.model_dump()}
    card = read_card(response.content, format)
    assert card['spec'] == 'chara_card_v3' and card['spec_version'] == '3.0'
    # SPEC_V3 f3a86af: check the actual standard, not only our permissive parser.
    data = card['data']
    for field in ('name', 'description', 'creator', 'character_version', 'mes_example',
                  'system_prompt', 'post_history_instructions', 'first_mes', 'personality',
                  'scenario', 'creator_notes'):
        assert isinstance(data[field], str), field
    for field in ('tags', 'alternate_greetings', 'group_only_greetings'):
        assert isinstance(data[field], list) and all(isinstance(item, str) for item in data[field])
    assert isinstance(data['extensions'], dict)
    assert card['data'].get('nickname', '') == ''
    assert card['data']['extensions']['meido']['nickname'] == '主人'
    assert store.manifest_path.read_bytes() == before
    if format == 'png':
        assert any('占位' in note for note in summary['notes'])
    else:
        assert preview['assets'] == []
        assert data['assets'] == []


def set_images(store, role):
    store.set_avatar(role.id, image_bytes('red', metadata=True), 'image/png')
    store.set_avatar(role.id, image_bytes('blue', metadata=True), 'image/png', card=True)
    store.set_avatar(role.id, b'PRIVATE_UNCROPPED_ORIGINAL', 'image/png', original=True)


@pytest.mark.parametrize('format', ['json', 'png', 'charx'])
def test_current_images_only_and_sensitive_role_fields_never_leave_saved_store(saved_role, format):
    store, role, client = saved_role
    set_images(store, role)
    store.set_model_configuration(role.id, 'PRIVATE_CONNECTION')
    # Deliberate synthetic future/legacy fields prove the export is an allowlist.
    source = store.get(role.id).model_copy(update={
        'apiKey': 'PRIVATE_CREDENTIAL', 'owner': 'PRIVATE_OWNER',
        'proactiveConfig': {'secret': 'PRIVATE_AUTONOMY'}, 'provenance': 'PRIVATE_PROVENANCE',
        'history': 'PRIVATE_HISTORY', 'memory': 'PRIVATE_MEMORY', 'workspace': 'PRIVATE_WORKSPACE'})
    store._roles[0] = source
    response = client.get(f'/api/roles/{role.id}/export?format={format}')
    assert response.status_code == 200, response.text
    summary = json.loads(base64.b64decode(response.headers['x-meido-export-summary']))
    card = read_card(response.content, format)
    assert set(card['data']['extensions']) == {'meido'}
    assert set(card['data']['extensions']['meido']) == {'description', 'nickname'}
    assert b'PRIVATE_' not in json.dumps(card).encode()
    preview = parse_role_card(f'export.{format}', response.content)
    assert len(preview['assets']) == {'json': 0, 'png': 1, 'charx': 2}[format]
    if format == 'json':
        assert summary['includedAssets'] == [] and summary['omittedAssets'] == ['头像', '卡片图']
        assert card['data']['assets'] == []
    elif format == 'png':
        assert summary['includedAssets'] == ['卡片图'] and summary['omittedAssets'] == ['头像']
        with Image.open(io.BytesIO(response.content)) as image:
            assert image.getpixel((0, 0)) == (0, 0, 255, 255)
            assert set(image.info) == {'ccv3'}
    else:
        assert summary['includedAssets'] == ['头像', '卡片图'] and summary['omittedAssets'] == []
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert set(archive.namelist()) == {'card.json', 'assets/icon/images/avatar.png', 'assets/icon/images/card-image.png'}
            for asset in card['data']['assets']:
                assert set(asset) == {'type', 'uri', 'name', 'ext'}
                assert all(isinstance(value, str) for value in asset.values())
                raw = archive.read(asset['uri'].removeprefix('embeded://'))
                assert b'PRIVATE_' not in raw
                with Image.open(io.BytesIO(raw)) as image:
                    assert image.info == {}


def test_png_uses_avatar_when_no_card_image_and_strips_exif(saved_role):
    store, role, client = saved_role
    raw = io.BytesIO()
    exif = Image.Exif()
    exif[270] = 'PRIVATE_EXIF_NOTE'
    Image.new('RGB', (3, 3), 'red').save(raw, format='JPEG', exif=exif)
    store.set_avatar(role.id, raw.getvalue(), 'image/jpeg')
    response = client.get(f'/api/roles/{role.id}/export?format=png')
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (3, 3)
        assert set(image.info) == {'ccv3'} and not image.getexif()
    summary = json.loads(base64.b64decode(response.headers['x-meido-export-summary']))
    assert summary['includedAssets'] == ['头像']


def test_json_never_reads_images_or_follows_urls(saved_role, monkeypatch):
    store, role, client = saved_role
    store._roles[0] = role.model_copy(update={'avatarUrl': 'https://invalid.test/private', 'avatarMediaType': 'image/png'})
    monkeypatch.setattr(exporter, '_read_image', lambda *args: pytest.fail('JSON must not read any image'))
    assert client.get(f'/api/roles/{role.id}/export?format=json').status_code == 200


@pytest.mark.parametrize('format', ['png', 'charx'])
def test_reencoding_applies_exif_orientation_before_removing_metadata(saved_role, format):
    store, role, client = saved_role
    raw = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6  # Rotate 90 degrees clockwise, as browsers display it.
    Image.new('RGB', (4, 2), 'red').save(raw, format='JPEG', exif=exif)
    store.set_avatar(role.id, raw.getvalue(), 'image/jpeg')
    response = client.get(f'/api/roles/{role.id}/export?format={format}')
    assert response.status_code == 200
    data = response.content
    if format == 'charx':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            data = archive.read('assets/icon/images/avatar.png')
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (2, 4)
        assert not image.getexif()


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'media', 'bytes', 'pixels', 'converted'])
def test_bad_image_returns_recoverable_error_without_paths_and_json_still_works(saved_role, monkeypatch, kind):
    store, role, client = saved_role
    store.set_avatar(role.id, image_bytes(), 'image/jpeg' if kind == 'media' else 'image/png')
    path = store.root / role.id / 'avatar'
    if kind == 'missing':
        path.unlink()
    if kind == 'corrupt':
        path.write_bytes(b'PRIVATE_CORRUPT')
    if kind == 'bytes':
        monkeypatch.setattr(exporter, 'MAX_IMAGE_BYTES', 10)
    if kind == 'pixels':
        monkeypatch.setattr(exporter, 'MAX_IMAGE_PIXELS', 10)
    if kind == 'converted':
        monkeypatch.setattr(exporter.limits, 'MAX_ASSET_PREVIEW_BYTES', 10)
    response = client.get(f'/api/roles/{role.id}/export?format=png')
    assert response.status_code == 422
    assert 'JSON' in response.json()['detail']
    assert str(store.root) not in response.text and 'PRIVATE_' not in response.text
    assert client.get(f'/api/roles/{role.id}/export?format=json').status_code == 200


def test_external_hard_link_is_rejected_before_reading(saved_role, tmp_path):
    store, role, client = saved_role
    store.set_avatar(role.id, image_bytes(), 'image/png')
    path = store.root / role.id / 'avatar'
    path.unlink()
    outside = tmp_path / 'outside.png'
    outside.write_bytes(image_bytes(metadata=True))
    os.link(outside, path)
    response = client.get(f'/api/roles/{role.id}/export?format=charx')
    assert response.status_code == 422
    assert '链接' in response.json()['detail']


@pytest.mark.parametrize('target', ['file', 'directory'])
def test_windows_reparse_points_are_rejected_before_open(saved_role, monkeypatch, target):
    store, role, client = saved_role
    store.set_avatar(role.id, image_bytes(), 'image/png')
    path = store.root / role.id / 'avatar' if target == 'file' else store.root / role.id
    original = Path.lstat
    def lstat(candidate, *args, **kwargs):
        result = original(candidate, *args, **kwargs)
        if candidate == path:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(exporter.os, 'open', lambda *args, **kwargs: pytest.fail('must reject before opening'))
    assert client.get(f'/api/roles/{role.id}/export?format=png').status_code == 422


def test_final_symlink_to_other_role_is_rejected(saved_role, monkeypatch):
    store, role, client = saved_role
    store.set_avatar(role.id, image_bytes(), 'image/png')
    other = store.create(RoleInput(name='其他角色', profile=RoleProfile(profile='other')))
    store.set_avatar(other.id, image_bytes(metadata=True), 'image/png')
    path = store.root / role.id / 'avatar'
    path.unlink()
    try:
        path.symlink_to(store.root / other.id / 'avatar')
    except OSError as error:
        pytest.skip(f'OS cannot create test symlink: {error.errno}')
    monkeypatch.setattr(exporter.os, 'open', lambda *args, **kwargs: pytest.fail('must reject before opening'))
    assert client.get(f'/api/roles/{role.id}/export?format=png').status_code == 422


def test_traversal_role_id_never_opens_file(tmp_path):
    with pytest.raises(exporter.RoleCardExportError, match='路径'):
        exporter._read_image(tmp_path, '../outside', 'avatar')


@pytest.mark.parametrize(('format', 'limit'), [
    ('json', 'MAX_JSON_BYTES'), ('png', 'MAX_METADATA_BYTES'), ('png', 'MAX_SOURCE_BYTES'),
    ('charx', 'MAX_CHARX_MEMBERS'), ('charx', 'MAX_CHARX_MEMBER_BYTES'),
    ('charx', 'MAX_CHARX_UNCOMPRESSED_BYTES'), ('charx', 'MAX_ASSET_PREVIEW_TOTAL_BYTES')])
def test_output_respects_existing_import_limits(saved_role, monkeypatch, format, limit):
    store, role, client = saved_role
    set_images(store, role)
    monkeypatch.setattr(exporter.limits, limit, 1)
    response = client.get(f'/api/roles/{role.id}/export?format={format}')
    assert response.status_code == 422


def test_saved_role_change_during_export_requires_retry(saved_role, monkeypatch):
    store, role, client = saved_role
    store.set_avatar(role.id, image_bytes(), 'image/png')
    original = exporter._read_image
    def changing_read(*args):
        raw = original(*args)
        store.update(role.id, RoleUpdateInput(name='改名', profile=role.profile))
        return raw
    monkeypatch.setattr(exporter, '_read_image', changing_read)
    response = client.get(f'/api/roles/{role.id}/export?format=png')
    assert response.status_code == 422 and '重试' in response.json()['detail']


def test_filename_and_summary_headers_are_bounded_and_safe(saved_role):
    store, role, client = saved_role
    store.update(role.id, RoleUpdateInput(name='角色\r\n"/\\:?\x01' + '长' * 1000, profile=role.profile))
    response = client.get(f'/api/roles/{role.id}/export?format=json')
    assert response.status_code == 200
    disposition = response.headers['content-disposition']
    name = unquote(disposition.split("UTF-8''")[1])
    assert len(name) <= 85
    assert not any(char in name for char in '\r\n"/\\:?\x01')
    assert len(disposition) < 1000
    assert len(response.headers['x-meido-export-summary']) < 4096


def test_missing_role_and_invalid_format_report_clear_errors(saved_role):
    _, role, client = saved_role
    assert client.get('/api/roles/missing/export?format=json').status_code == 404
    response = client.get(f'/api/roles/{role.id}/export?format=zip')
    assert response.status_code == 422 and 'CHARX' in response.json()['detail']


@pytest.mark.parametrize('meido', [{'description': '', 'nickname': ''}, {'description': '新简介', 'nickname': '新称呼'}, {'description': {}, 'nickname': ['bad']}])
def test_import_only_recognizes_string_meido_fields_and_keeps_legacy_fallback(meido):
    card = {'name': 'Alice', 'description': '设定', 'nickname': '旧称呼', 'extensions': {
        'shiori': {'description': '旧简介'}, 'meido': {**meido, 'modelConfig': {'apiKey': 'PRIVATE_KEY'}, 'permissions': ['all']}}}
    draft = parse_role_card('test.json', json.dumps(card).encode())['draft']
    assert draft['description'] == (meido['description'] if isinstance(meido['description'], str) else '旧简介')
    assert draft['nickname'] == (meido['nickname'] if isinstance(meido['nickname'], str) else '旧称呼')
    assert set(draft) == {'name', 'description', 'profile', 'personality', 'behaviorRules', 'responseConstraints', 'nickname'}
