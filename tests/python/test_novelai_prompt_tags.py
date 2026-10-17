"""Owner tags have the same role/rating-filtered expansion as a real scene."""
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def test_owner_manages_role_tags_and_previews_only_currently_permitted_matches(novelai_main):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    saved = client.post(path, json={'name': 'Garden', 'keywords': ['花园', 'garden'], 'prompt': 'flowers, sunlight',
        'negative_prompt': 'night', 'rating': 'general', 'enabled': True})
    assert saved.status_code == 200, saved.text
    identifier = saved.json()['tag']['id']
    other = client.post(path, json={'name': 'Restricted', 'keywords': ['garden'], 'prompt': 'restricted',
        'negative_prompt': '', 'rating': 'explicit', 'enabled': True})
    assert other.status_code == 200, other.text
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert response.status_code == 200, response.text
    preview = client.post(path + '/preview', json={'text': 'At the GARDEN 花园', 'prompt': 'blue sky', 'negative_prompt': 'rain'})
    assert preview.status_code == 200, preview.text
    assert preview.json()['prompt'] == 'blue sky, flowers, sunlight'
    assert preview.json()['negative_prompt'] == 'rain, night'
    assert preview.json()['matchedIds'] == [identifier]
    assert len(client.get(path).json()['tags']) == 2
    deleted = client.delete(path + '/' + identifier)
    assert deleted.status_code == 200, deleted.text
    assert client.post(path + '/preview', json={'text': 'garden', 'prompt': 'blue sky', 'negative_prompt': ''}).json()['prompt'] == 'blue sky'
    assert not service.peer.requests


def test_tag_library_is_role_local_paginated_and_rejects_unbounded_or_executable_inputs(novelai_main):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    tag = {'name': 'Literal', 'keywords': ['[garden]'], 'prompt': 'flowers',
        'negative_prompt': '', 'rating': 'general', 'enabled': True}
    first = client.post(path, json=tag).json()['tag']['id']
    second = client.post(path, json={**tag, 'name': 'Disabled', 'enabled': False}).json()['tag']['id']
    page = client.get(path, params={'limit': 1}).json()
    next_page = client.get(path, params={'limit': 1, 'cursor': page['nextCursor']}).json()
    assert {page['tags'][0]['id'], next_page['tags'][0]['id']} == {first, second}
    assert next_page['nextCursor'] is None
    other = client.post('/api/roles', json={'name': 'Other role', 'profile': {'profile': 'isolated'}})
    assert other.status_code == 201, other.text
    other_path = f'/api/roles/{other.json()["role"]["id"]}/novelai/prompt-tags'
    assert client.get(other_path).json()['tags'] == []
    assert client.put(other_path + '/' + first, json=tag).status_code == 404
    assert client.delete(other_path + '/' + first).status_code == 404
    for invalid in ({'name': '  '}, {'keywords': []}, {'keywords': ['x' * 81]}, {'prompt': '花'},
                    {'prompt': 'x' * 4001}, {'rating': 'unrated'}, {'enabled': 1}, {'expression': '.*'}):
        assert client.post(path, json={**tag, **invalid}).status_code == 422
    assert client.get(path, params={'limit': 51}).status_code == 422
    assert client.post(path + '/preview', json={'text': 'x' * 12001}).status_code == 422
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert response.status_code == 200, response.text
    assert client.post(path + '/preview', json={'text': 'garden'}).json()['matchedIds'] == []
    assert client.post(path + '/preview', json={'text': '［ＧＡＲＤＥＮ］'}).json()['matchedIds'] == [first]
    updated = client.put(path + '/' + first, json={**tag, 'prompt': 'sunlight'})
    assert updated.status_code == 200, updated.text
    assert client.post(path + '/preview', json={'text': '[garden]'}).json()['prompt'] == 'sunlight'
    assert not service.peer.requests
