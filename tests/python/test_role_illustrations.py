import hashlib
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from backend.app.models import RoleInput
from backend.app.role_store import RoleStore
from backend.app.role_assets import role_assets_router


def image_bytes(color='blue'):
    output = io.BytesIO()
    Image.new('RGB', (64, 64), color).save(output, format='PNG')
    return output.getvalue()


def test_illustration_library_upload_classify_background_and_recoverable_removal(tmp_path):
    store = RoleStore(tmp_path / 'roles')
    role = store.create(RoleInput(name='绘图角色', profile={'profile': 'test'}))
    other = store.create(RoleInput(name='other', profile={'profile': 'test'}))
    app = FastAPI()
    app.include_router(role_assets_router(lambda: store))
    with TestClient(app) as client:
        path = f'/api/roles/{role.id}'
        uploaded = client.post(path + '/illustrations?name=blue.png', content=image_bytes(), headers={'content-type': 'image/png'})
        assert uploaded.status_code == 200, uploaded.text
        item = uploaded.json()['role']['illustrations'][0]
        assert client.get(item['url']).content == image_bytes()
        assert client.get(f'/api/roles/{other.id}/illustrations/{item["id"]}').status_code == 404
        assert client.put(f'/api/roles/{other.id}/chat-background', json={'illustrationId': item['id']}).status_code == 404
        assert client.put(path + '/chat-background', json={'illustrationId': item['id']}).json()['role']['chatBackgroundUrl'] == item['url']
        assert client.put(path + '/avatar-from-illustration', json={'illustrationId': item['id']}).json()['role']['avatarUrl']
        assert store.avatar_path(role.id).read_bytes() == image_bytes()
        moved = client.patch(path + '/illustrations', json={'categories': ['场景'], 'bindings': {item['id']: '场景'}})
        assert moved.status_code == 200 and moved.json()['role']['illustrations'][0]['category'] == '场景'
        configured = client.patch(path + '/illustrations', json={'categorySend': {'场景': True}, 'moodBindings': {'开心': item['id']}, 'defaultMood': '开心'})
        assert configured.status_code == 200, configured.text
        reloaded = RoleStore(store.root).get(role.id)
        assert reloaded.illustrationCategorySend == {'场景': True}
        assert reloaded.moodIllustrationBindings == {'开心': item['id']} and reloaded.defaultMood == '开心'
        assert client.patch(path + '/illustrations', json={'categorySend': {'不存在': True}}).status_code == 422
        assert client.patch(path + '/illustrations', json={'moodBindings': {'开心': 'foreign'}}).status_code == 422
        assert client.patch(path + '/illustrations', json={'categorySend': {'场景': 'true'}}).status_code == 422
        # No stale default-category dependency after the owner renames it.
        imported = store.add_illustration(role.id, image_bytes('red'), 'image/png', 'second')
        assert imported.illustrations[-1].category == '场景'
        removed = client.delete(item['url'])
        assert removed.status_code == 200 and removed.json()['role']['chatBackgroundUrl'] is None
        assert removed.json()['role']['moodIllustrationBindings'] == {}
        assert removed.json()['role']['defaultMood'] == '平静'
        assert client.get(item['url']).status_code == 404
        assert len(list((store.root / role.id / 'illustrations-removed').iterdir())) == 1
        assert RoleStore(store.root).get(role.id).illustrations[0].name == 'second'


def test_generated_illustration_is_idempotent_and_does_not_restore_a_later_background(tmp_path):
    store = RoleStore(tmp_path / 'roles')
    role = store.create(RoleInput(name='角色', profile={'profile': 'test'}))
    content = image_bytes()
    saved = store.add_illustration(role.id, content, 'image/png', operation_id='a' * 32,
        set_background=True, authorized=lambda: True, expected_background_id=None)
    identifier = saved.chatBackgroundId
    assert identifier
    store.set_chat_background(role.id, None)
    again = store.add_illustration(role.id, content, 'image/png', operation_id='a' * 32,
        set_background=True, authorized=lambda: False, expected_background_id=identifier)
    assert len(again.illustrations) == 1 and again.chatBackgroundId is None
    assert store.reconcile_generated_illustration(role.id, operation_id='a' * 32,
        digest=hashlib.sha256(content).hexdigest(), size=len(content)) is True
    store.remove_illustration(role.id, identifier)
    assert store.reconcile_generated_illustration(role.id, operation_id='a' * 32,
        digest=hashlib.sha256(content).hexdigest(), size=len(content)) is True
    with pytest.raises(PermissionError):
        store.add_illustration(role.id, content, 'image/png', operation_id='b' * 32,
            set_background=True, authorized=lambda: True, expected_background_id='changed')
    assert store.get(role.id).illustrations == []
