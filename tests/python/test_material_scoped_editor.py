"""Owner scope edits compose meaning without granting original-image access."""
import json
import pytest

from test_material_library_api import api, current, put
from test_material_duplicate_meaning import save, context
from test_chat_meme_library import upload


def edit(client, **changes):
    listing = current(client)
    return client.patch('/api/roles/alice/materials/' + listing['materials'][0]['id'],
        json={'expectedRevision': listing['revision'], **changes})


def test_owner_promotes_meaning_without_promoting_original_and_group_fields_are_isolated(api):
    client, service, chat = api
    save(chat, upload(chat), 'thread:a', '猫捂脸')
    first = current(client)['materials'][0]
    group = next(item for item in first['meaningScopes'] if item['scope'] == 'conversation')
    changed = edit(client, meaningScopeId=group['id'], description='本群表示编译失败',
        aliases=['构建炸了'], tags=['代码'], emotions=['无语'], uses=['编译失败'], avoid=['庆祝成功'])
    assert changed.status_code == 200, changed.text
    assert client.get('/api/roles/alice/materials', params={'query': '构建炸了'}).json()['total'] == 1
    old, = chat.candidates('alice', '构建炸了', context=context('thread:a'))
    promoted = edit(client, meaningScopeId='role', description='猫捂脸，尴尬', uses=['尴尬'])
    assert promoted.status_code == 200, promoted.text
    assert chat.candidates('alice') == ()
    assert chat.candidates('alice', context=context('thread:b')) == ()
    now, = chat.candidates('alice', '尴尬', context=context('thread:a'))
    assert now.public()['generalDescription'] == '猫捂脸，尴尬'
    assert now.public()['groupDescription'] == '本群表示编译失败'
    assert now.public()['generalBasis'] == now.public()['groupBasis'] == 'owner_confirmed'
    assert '编译失败' in now.public()['uses'] and not chat.valid('alice', old)
    save(chat, upload(chat), 'thread:b', 'B群用途')
    other, = chat.candidates('alice', '尴尬', context=context('thread:b'))
    assert '编译' not in json.dumps(other.public(), ensure_ascii=False)
    assert chat.candidates('alice', '构建炸了', context=context('thread:b')) == ()


def test_advanced_edits_search_filters_conflict_and_purpose_uncertainty(api):
    client, service, chat = api
    put(client, description='猫捂脸')
    response = edit(client, aliases=['脸红'], tags=['猫咪'], emotions=['害羞'], uses=['道歉'],
        avoid=['严肃道歉'], uncertainty='品种未知', uncertaintyAffectsUse=False)
    assert response.status_code == 200, response.text
    before = current(client)
    assert chat.candidates('alice', '脸红') and chat.candidates('alice', '道歉')
    found = client.get('/api/roles/alice/materials', params={'query': '道歉', 'source': 'owner_import', 'enabled': 'true'})
    assert found.status_code == 200 and len(found.json()['materials']) == 1
    assert client.get('/api/roles/alice/materials', params={'source': 'chat'}).json()['materials'] == []
    candidate, = chat.candidates('alice')
    response = edit(client, uncertainty='可能带侮辱含义', uncertaintyAffectsUse=True)
    assert response.status_code == 200, response.text
    assert chat.candidates('alice') == () and not chat.valid('alice', candidate)
    assert len(chat.candidates('alice', explicit=True)) == 1
    stale = client.patch('/api/roles/alice/materials/' + before['materials'][0]['id'],
        json={'expectedRevision': before['revision'], 'uses': ['过时建议']})
    assert stale.status_code == 409
    assert current(client)['materials'][0]['uses'] == ['道歉']
    assert edit(client, uncertaintyAffectsUse=False).status_code == 200
    assert chat.candidates('alice')


def test_revoked_group_scope_cannot_be_revised_or_reactivated(api):
    client, service, chat = api
    attachment = upload(chat)
    ctx = context('thread:a')
    allowed = True
    chat.source_validator = lambda authority: allowed
    chat.save('alice', attachment, '收藏', '群内说明', ctx, lambda: True,
        meaning={'kind': 'owner_confirmed'}, source={'kind': 'chat', 'sessionKey': 'thread:a',
            'authority': {'account_id': 'qq-account', 'pairing_generation': 1}})
    scope = next(x for x in current(client)['materials'][0]['meaningScopes'] if x['scope'] == 'conversation')
    allowed = False
    response = edit(client, meaningScopeId=scope['id'], description='试图恢复旧说明')
    assert response.status_code == 403, response.text
    assert edit(client, description='旧客户端无范围参数').status_code == 403
    assert edit(client, meaningScopeId='role', description='通用描述').status_code == 200
    assert chat.candidates('alice', context=ctx) == ()
    assert not next(x for x in current(client)['materials'][0]['meaningScopes'] if x['scope'] == 'conversation')['available']


def test_revocation_during_owner_publication_cannot_commit_group_edit(api, monkeypatch):
    from backend.app.channels.meme_library import _OwnerPublication
    client, service, chat = api
    allowed = True
    chat.source_validator = lambda authority: allowed
    chat.save('alice', upload(chat), '收藏', '原说明', context('thread:a'), lambda: True,
        meaning={'kind': 'owner_confirmed'}, source={'kind': 'chat', 'sessionKey': 'thread:a', 'authority': {'epoch': 1}})
    original = _OwnerPublication.prepare
    def prepare(self, check):
        nonlocal allowed
        original(self, check)
        allowed = False
    monkeypatch.setattr(_OwnerPublication, 'prepare', prepare)
    response = edit(client, description='发布期间撤权')
    assert response.status_code in (403, 409), response.text
    assert current(client)['materials'][0]['description'] == '原说明'


@pytest.mark.parametrize('changes', [{'uses': ['新用途']}, {'enabled': False}, {'uncertaintyAffectsUse': True}])
def test_model_cannot_override_owner_fields_through_alternative_changes(api, changes):
    from backend.app.channels import meme_semantics as meanings
    client, service, chat = api
    put(client)
    candidate, = chat.candidates('alice')
    value = meanings.meaning_for(chat.library, 'alice', candidate.path, candidate.sha256, {})
    with pytest.raises(PermissionError):
        meanings.revise(value, changes, expected_revision=value['revision'],
            basis={'kind': 'model_observation'}, author={'kind': 'model'})


def test_pending_local_guess_cannot_pollute_confirmed_shared_candidate(api):
    from backend.app.channels import meme_semantics as meanings
    client, service, chat = api
    save(chat, upload(chat), 'thread:a', '')
    put(client, description='主人确认的通用说明')
    candidate, = chat.candidates('alice')
    path = meanings.metadata_path(chat.library, 'alice', candidate.sha256)
    value = json.loads(path.read_text(encoding='utf-8'))
    value.update(description='未核验的侮辱猜测', uses=['秘密侮辱'], uncertaintyAffectsUse=True)
    path.write_bytes(meanings.encode(value))
    candidate, = chat.candidates('alice', context=context('thread:a'))
    assert candidate.description == '主人确认的通用说明'
    assert candidate.public()['groupDescription'] == ''
    assert '侮辱' not in json.dumps(candidate.public(), ensure_ascii=False)
    assert chat.candidates('alice', '秘密侮辱', context=context('thread:a')) == ()
