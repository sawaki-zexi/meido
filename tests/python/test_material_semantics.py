"""Persistent per-image meaning through owner HTTP and real library publications."""
import json
import asyncio
import pytest
from types import SimpleNamespace

from test_material_library_api import api, current, put
from test_chat_meme_library import picture, real_library, upload
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.channels.meme_library import ChatMemeLibrary


def test_blank_collection_survives_restart_but_is_not_an_automatic_candidate(api):
    client, service, chat = api
    response = put(client, description='')
    assert response.status_code == 200, response.text
    item = response.json()['material']
    assert item['description'] == '' and item['semanticStatus'] == 'pending'
    assert item['basis']['kind'] == 'unverified'
    restarted = ChatMemeLibrary(chat.library, chat._attachment_root)
    assert restarted.candidates('alice') == ()
    explicit, = restarted.candidates('alice', explicit=True)
    context = SimpleNamespace(role_id='alice', session_key='owner:material-library', run_id='run',
        tool_call_id='send', signal=CancellationToken())
    assert restarted.reference('alice', explicit, context)[1] == picture()
    from backend.app.channels.meme_tools import ChatMemeTools
    tools = ChatMemeTools(restarted, None, role_id='alice', session_key=context.session_key, run_id='run',
        account_id='a', thread_id='t', target_id='target', attachments=[], authorized=lambda: True,
        permitted=lambda name: True, owner_text='随便聊聊')
    available = {tool.definition.name: tool for tool in tools.tools()}
    listed = asyncio.run(available['chat_meme_list'].execute({}, context))
    rejected = asyncio.run(available['chat_meme_send'].execute(
        {'candidate_id': listed.details['candidates'][0]['id']}, context))
    assert rejected.is_error and '明确选定' in rejected.content


def test_explicit_retry_after_partial_publication_preserves_existing_semantics(api):
    client, service, chat = api
    original = put(client).json()['material']
    (chat.library.root('alice') / 'manifest.json').unlink()
    attachment = upload(chat)
    context = SimpleNamespace(role_id='alice', session_key='thread:one', run_id='later',
        tool_call_id='retry', signal=CancellationToken())
    chat.save('alice', attachment, original['category'], '后来的旧建议', context, lambda: True,
        meaning={'kind': 'model_observation', 'requestId': 'later'},
        source={'kind': 'chat', 'sessionKey': context.session_key})
    latest, = current(client)['materials']
    assert latest['assetId'] == original['assetId'] and latest['description'] == original['description']


def test_owner_can_edit_meaning_and_switch_with_revision_conflict_protection(api):
    client, service, chat = api
    item = put(client).json()['material']
    original = current(client)
    response = client.patch('/api/roles/alice/materials/' + item['id'], json={
        'expectedRevision': original['revision'], 'name': '编译失败', 'description': '写不出代码', 'enabled': False})
    assert response.status_code == 200, response.text
    changed = response.json()['material']
    assert changed['assetId'] == item['assetId']
    assert changed['description'] == '写不出代码' and changed['enabled'] is False
    assert changed['basis']['kind'] == 'owner_confirmed'
    assert changed['semanticRevision'] > item['semanticRevision']
    assert changed['history'][-1]['description'] == '合成表情'
    assert chat.candidates('alice') == ()
    assert client.patch('/api/roles/alice/materials/' + item['id'], json={
        'expectedRevision': original['revision'], 'description': '过时模型输出'}).status_code == 409
    assert current(client)['materials'][0]['description'] == '写不出代码'


def test_replacement_keeps_identity_and_old_meaning_without_recommending_new_pixels(api):
    client, service, chat = api
    old = put(client).json()['material']
    candidate, = chat.candidates('alice')
    candidate.path.write_bytes(picture(color='blue'))
    item, = current(client)['materials']
    assert item['assetId'] == old['assetId']
    assert item['semanticStatus'] == 'content_changed'
    assert item['description'] == ''
    assert any(row['description'] == old['description'] for row in item['history'])
    assert chat.candidates('alice') == ()
    updated = client.patch('/api/roles/alice/materials/' + item['id'], json={
        'expectedRevision': current(client)['revision'], 'description': '新的蓝图'}).json()['material']
    assert updated['assetId'] == old['assetId'] and updated['description'] == '新的蓝图'


def test_duplicate_sources_append_without_overwriting_owner_meaning(api):
    client, service, chat = api
    first = put(client).json()['material']
    attachment = upload(chat)
    context = SimpleNamespace(role_id='alice', session_key='thread:one', run_id='run',
        tool_call_id='save', signal=CancellationToken())
    chat.save('alice', attachment, 'other', '旧建议', context, lambda: True,
        meaning={'kind': 'model_observation', 'requestId': 'test-evidence'},
        source={'kind': 'chat', 'sessionKey': context.session_key, 'attachmentId': attachment['id']})
    latest, = current(client)['materials']
    assert latest['assetId'] == first['assetId'] and latest['description'] == first['description']
    assert len(latest['sources']) == 2
    assert len(list(chat.library.root('alice').glob('*/*.png'))) == 1
    record = json.loads(next((chat.library.root('alice').parent / '.meido-memes/chat').glob('*.json')).read_text(encoding='utf-8'))
    assert record['sources'][-1]['description'] == '旧建议'


def test_late_model_publication_cannot_overwrite_concurrent_owner_correction(api, monkeypatch):
    from backend.app.channels.meme_library import _OwnerPublication
    client, service, chat = api
    item = put(client).json()['material']
    attachment = upload(chat)
    context = SimpleNamespace(role_id='alice', session_key='thread:one', run_id='run',
        tool_call_id='late-model', signal=CancellationToken())
    original = _OwnerPublication.prepare
    def prepare(self, check):
        if self.context.tool_call_id == 'late-model':
            response = client.patch('/api/roles/alice/materials/' + item['id'], json={
                'expectedRevision': current(client)['revision'], 'description': '主人刚刚纠正'})
            assert response.status_code == 200, response.text
        return original(self, check)
    monkeypatch.setattr(_OwnerPublication, 'prepare', prepare)
    with pytest.raises(PermissionError, match='conflict'):
        chat.save('alice', attachment, 'other', '过时的模型建议', context, lambda: True,
            meaning={'kind': 'model_observation', 'requestId': 'old'},
            source={'kind': 'chat', 'sessionKey': context.session_key})
    latest, = current(client)['materials']
    assert latest['description'] == '主人刚刚纠正'
    assert latest['history'][-1]['description'] == item['description']


def test_legacy_drift_updates_cannot_bypass_confirmed_per_image_meaning(api):
    import hashlib
    client, service, chat = api
    root = chat.library.root('alice')
    path = root / 'legacy' / 'original.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(picture())
    (root / 'manifest.json').write_text(json.dumps({'version': 1, 'categories': {
        'legacy': {'desc': '旧类别', 'aliases': ['旧别名'], 'enabled': True}}}), encoding='utf-8')
    metadata = root.parent / '.meido-memes/metadata/original.json'
    metadata.parent.mkdir(parents=True)
    value = {'roleId': 'alice', 'sha256': hashlib.sha256(picture()).hexdigest(),
        'path': 'legacy/original.png', 'description': '维护路径已有说明'}
    metadata.write_text(json.dumps(value), encoding='utf-8')
    before = current(client)
    item, = before['materials']
    assert item['description'] == value['description'] and item['basis']['kind'] == 'legacy_description'
    edited = client.patch('/api/roles/alice/materials/' + item['id'], json={
        'expectedRevision': before['revision'], 'description': '主人确认的新含义'}).json()['material']
    value['description'] = '随后维护写入的过时说明'
    metadata.write_text(json.dumps(value), encoding='utf-8')
    candidate, = chat.candidates('alice', '旧别名')
    assert candidate.description == edited['description']
    assert candidate.public()['basis'] == 'owner_confirmed'
    assert current(client)['materials'][0]['semanticRevision'] == edited['semanticRevision']


def test_legacy_category_description_update_invalidates_old_candidate(api):
    client, service, chat = api
    root = chat.library.root('alice')
    path = root / 'legacy' / 'old.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(picture())
    manifest = root / 'manifest.json'
    value = {'version': 1, 'categories': {'legacy': {'desc': '原有说明', 'enabled': True}}}
    manifest.write_text(json.dumps(value), encoding='utf-8')
    old, = chat.candidates('alice')
    value['categories']['legacy']['desc'] = '维护更新后的说明'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    assert not chat.valid('alice', old)
    new, = chat.candidates('alice')
    assert new.id != old.id and new.description == '维护更新后的说明'


@pytest.mark.parametrize('kind', ['chat', 'drift'])
def test_replaced_legacy_metadata_keeps_prior_meaning_as_history_not_recommendation(api, kind):
    import hashlib
    client, service, chat = api
    root = chat.library.root('alice')
    digest = hashlib.sha256(picture()).hexdigest()
    path = root / 'legacy' / (digest + '.png')
    path.parent.mkdir(parents=True)
    path.write_bytes(picture())
    (root / 'manifest.json').write_text(json.dumps({'version': 1, 'categories': {
        'legacy': {'desc': '旧类别说明', 'enabled': True}}}), encoding='utf-8')
    metadata = root.parent / '.meido-memes' / ('chat' if kind == 'chat' else 'metadata') / (digest + '.json')
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({'roleId': 'alice', 'sha256': digest, 'description': '原图的说明',
        'path': path.relative_to(root).as_posix()}), encoding='utf-8')
    original, = current(client)['materials']
    path.write_bytes(picture(color='blue'))
    assert chat.candidates('alice') == ()
    changed, = current(client)['materials']
    assert changed['assetId'] == original['assetId'] and changed['semanticStatus'] == 'content_changed'
    assert changed['history'][-1]['description'] == '原图的说明'


def test_collected_group_scope_survives_source_loss_but_not_revocation_or_cross_group(tmp_path):
    chat = real_library(tmp_path)
    attachment = upload(chat, 'GIF')
    context = SimpleNamespace(role_id='alice', session_key='thread:one', run_id='run',
        tool_call_id='save', signal=CancellationToken())
    live = {'allowed': True}
    chat.source_validator = lambda authority: live['allowed'] and authority['epoch'] == 1
    chat.save('alice', attachment, 'group', '本群的表情', context, lambda: True,
        meaning={'kind': 'owner_confirmed'}, source={'kind': 'chat', 'sessionKey': context.session_key,
            'attachmentId': attachment['id'], 'authority': {'epoch': 1}})
    assert chat.candidates('alice') == ()
    other = SimpleNamespace(**{**context.__dict__, 'session_key': 'thread:two'})
    assert chat.candidates('alice', context=other) == ()
    candidate, = chat.candidates('alice', context=context)
    from pathlib import Path
    Path(attachment['path']).unlink()  # The local original no longer depends on remote/input availability.
    assert chat.reference('alice', candidate, context)[1] == picture('GIF')
    live['allowed'] = False
    assert chat.candidates('alice', context=context) == ()
    assert not chat.valid('alice', candidate)


def test_same_bytes_in_two_groups_preserve_both_sources_without_other_scope_access(tmp_path):
    chat = real_library(tmp_path)
    attachment = upload(chat)
    for session in ('thread:one', 'thread:two'):
        context = SimpleNamespace(role_id='alice', session_key=session, run_id=session,
            tool_call_id='save', signal=CancellationToken())
        chat.save('alice', attachment, 'group', 'same', context, lambda: True,
            source={'kind': 'chat', 'sessionKey': session, 'attachmentId': attachment['id']})
    restarted = ChatMemeLibrary(chat.library, chat._attachment_root)
    assert len(restarted.candidates('alice', context=context)) == 1
    context.session_key = 'thread:one'
    assert len(restarted.candidates('alice', context=context)) == 1
    context.session_key = 'thread:three'
    assert restarted.candidates('alice', context=context) == ()
    records = list((chat.library.root('alice').parent / '.meido-memes/chat').glob('*.json'))
    assert len(records) == 1 and len(json.loads(records[0].read_text(encoding='utf-8'))['sources']) == 2


def test_tool_uses_host_text_or_current_matching_receipt_not_model_claim(tmp_path):
    from backend.app.channels.meme_tools import ChatMemeTools
    from backend.app.agent_runtime.tools import ToolContext
    chat = real_library(tmp_path)
    attachment = upload(chat)
    context = ToolContext('alice', 'thread:one', 'run', CancellationToken(), 'save')
    receipts = []
    service = ChatMemeTools(chat, None, role_id='alice', session_key=context.session_key, run_id='run',
        account_id='account', thread_id='one', target_id='target', attachments=[attachment],
        authorized=lambda: True, permitted=lambda name: True, owner_text='保存这张表情，用来表示编译失败',
        media_receipts=lambda: receipts)
    tool = next(tool for tool in service.tools() if tool.definition.name == 'chat_meme_save')
    assert asyncio.run(tool.execute({'description': '模型猜测有猫'}, context)).is_error
    assert asyncio.run(tool.execute({'description': '模型猜测有猫', 'basis': 'owner_confirmed'}, context)).is_error
    assert not asyncio.run(tool.execute({'description': '编译失败'}, context)).is_error
    # A new image can receive a model observation only when the host receipt
    # matches its original digest and actually submitted pixels this run.
    other = upload(chat, color='blue')
    service = ChatMemeTools(chat, None, role_id='alice', session_key=context.session_key, run_id='run',
        account_id='account', thread_id='one', target_id='target', attachments=[other],
        authorized=lambda: True, permitted=lambda name: True, owner_text='保存这张表情', media_receipts=lambda: receipts)
    tool = next(tool for tool in service.tools() if tool.definition.name == 'chat_meme_save')
    digest = service.attachments[other['id']]['_memeSha256']
    receipts.append({'requestId': 'old', 'images': [{'status': 'not_submitted', 'contentSha256': digest}]})
    assert asyncio.run(tool.execute({'description': '蓝色图片'}, context)).is_error
    receipts.append({'requestId': 'current', 'images': [{'status': 'pixels_submitted', 'contentSha256': digest, 'coverage': 'complete'}]})
    result = asyncio.run(tool.execute({'description': '蓝色图片'}, context))
    assert not result.is_error and result.details['basis']['requestId'] == 'current'


def test_metadata_publication_failure_retains_confirmed_edit_on_restart(api, monkeypatch):
    from backend.app.channels.meme_library import _OwnerPublication
    client, service, chat = api
    item = put(client).json()['material']
    original = _OwnerPublication.record
    failed = False
    def record(self, status):
        nonlocal failed
        if status == 'files_published' and not failed:
            failed = True
            raise OSError('fixture interrupted after atomic publication')
        return original(self, status)
    monkeypatch.setattr(_OwnerPublication, 'record', record)
    response = client.patch('/api/roles/alice/materials/' + item['id'], json={
        'expectedRevision': current(client)['revision'], 'description': '主人后来纠正'})
    assert response.status_code == 503
    chat.library.reconcile('alice')
    from backend.app.material_library import OwnerMaterialLibrary
    restarted = OwnerMaterialLibrary(ChatMemeLibrary(chat.library, chat._attachment_root), lambda role: object())
    latest, = restarted.list('alice')['materials']
    assert latest['description'] == '主人后来纠正' and latest['history'][-1]['description'] == '合成表情'


def test_desktop_message_collection_rejects_wrong_role_and_attachment(api, tmp_path):
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    from backend.app.material_library import message_attachment_source
    client, service, chat = api
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data' / 'sessions.db')
    sessions.open_role_session('alice')
    sessions.open_role_session('bob')
    attachment = upload(chat)
    message = sessions.append_message('role:alice', 'user', '保存这张', metadata={'attachments': [attachment]})
    foreign = sessions.append_message('role:bob', 'user', '其他角色', metadata={'attachments': [attachment]})
    roles = SimpleNamespace(workspace_path=chat.attachment_root)
    service.message_source = lambda rid, mid, aid: message_attachment_source(roles, sessions, None, rid, mid, aid)
    endpoint = '/api/roles/alice/materials/from-message'
    assert client.post(endpoint, json={'messageId': foreign.id, 'attachmentId': attachment['id']}).status_code == 404
    assert client.post(endpoint, json={'messageId': message.id, 'attachmentId': 'b' * 32}).status_code == 404
    response = client.post(endpoint, json={'messageId': message.id, 'attachmentId': attachment['id']})
    assert response.status_code == 200, response.text
    assert response.json()['material']['semanticStatus'] == 'pending'
    assert response.json()['material']['sources'] == [{'kind': 'chat', 'scope': '当前角色'}]
