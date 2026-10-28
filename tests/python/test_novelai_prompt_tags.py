"""Owner tags have the same role/rating-filtered expansion as a real scene."""
import io
import json

import pytest
from PIL import Image

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


def test_explicit_prompt_tags_require_service_nsfw_and_role_rating_grants(novelai_main):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    explicit = client.post(path, json={'name': 'Adult scene', 'keywords': ['garden'],
        'prompt': 'explicit detail', 'negative_prompt': '', 'rating': 'explicit', 'enabled': True})
    assert explicit.status_code == 200, explicit.text
    explicit_id = explicit.json()['tag']['id']
    role = service.roles.get(service.role.id)
    updated = client.put(f'/api/roles/{service.role.id}', json={'name': role.name,
        'profile': role.profile.model_dump(), 'agentConfig': {'novelai': {
            'promptTagsEnabled': True, 'allowedPromptTagRatings': ['general', 'explicit']}}})
    assert updated.status_code == 200, updated.text

    blocked = client.post(path + '/preview', json={'text': 'garden'}).json()
    assert explicit_id not in blocked['matchedIds']
    assert 'explicit detail' not in blocked['prompt']

    public = service.application.settings.public()
    enabled = service.application.settings.save({
        'allowedModels': ['nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full'],
        'nsfwEnabled': True,
    }, expected_revision=public['revision'])
    allowed = client.post(path + '/preview', json={'text': 'garden'}).json()
    assert explicit_id in allowed['matchedIds']
    assert 'explicit detail' in allowed['prompt']
    assert enabled['nsfwEnabled'] is True

    role = service.roles.get(service.role.id)
    revoked = client.put(f'/api/roles/{service.role.id}', json={'name': role.name,
        'profile': role.profile.model_dump(), 'agentConfig': {'novelai': {
            'promptTagsEnabled': True, 'allowedPromptTagRatings': ['general']}}})
    assert revoked.status_code == 200, revoked.text
    role_blocked = client.post(path + '/preview', json={'text': 'garden'}).json()
    assert explicit_id not in role_blocked['matchedIds']
    assert 'explicit detail' not in role_blocked['prompt']


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


def test_prompt_tag_expansion_deduplicates_case_insensitively(novelai_main):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    saved = client.post(path, json={'name': 'Garden', 'keywords': ['garden'],
        'prompt': 'Flowers, sunlight', 'negative_prompt': 'Night', 'rating': 'general', 'enabled': True})
    assert saved.status_code == 200, saved.text
    response = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert response.status_code == 200, response.text
    preview = client.post(path + '/preview', json={'text': 'garden', 'prompt': 'flowers', 'negative_prompt': 'night'})
    assert preview.status_code == 200, preview.text
    assert preview.json()['prompt'] == 'flowers, sunlight'
    assert preview.json()['negative_prompt'] == 'night'


@pytest.mark.parametrize(('base', 'addition', 'expected'), [
    ('1.2::red hair, blue eyes::', '1.8::green dress, blue eyes::, smile',
     '1.2::red hair, blue eyes::, 1.8::green dress, blue eyes::, smile'),
    ('-1::rain, night::', '-2.5::clouds, night::, blur',
     '-1::rain, night::, -2.5::clouds, night::, blur'),
    ('{red hair, blue eyes}', '{green dress, blue eyes}, smile',
     '{red hair, blue eyes}, {green dress, blue eyes}, smile'),
    ('[[rain, night]]', '[[clouds, night]], blur',
     '[[rain, night]], [[clouds, night]], blur'),
    ('sunlight', '1.5::flowers, sunlight, sunlight::, smile',
     'sunlight, 1.5::flowers, sunlight, sunlight::, smile'),
    ('{{rain, night::', '{{clouds, night::, blur',
     '{{rain, night::, {{clouds, night::, blur'),
])
def test_preview_preserves_complete_weighted_fragments_in_both_prompts(novelai_main, base, addition, expected):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    saved = client.post(path, json={'name': 'Weighted', 'keywords': ['garden'],
        'prompt': addition, 'negative_prompt': addition, 'rating': 'general', 'enabled': True})
    assert saved.status_code == 200, saved.text
    enabled = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert enabled.status_code == 200, enabled.text

    preview = client.post(path + '/preview', json={'text': 'garden', 'prompt': base, 'negative_prompt': base})

    assert preview.status_code == 200, preview.text
    assert preview.json()['prompt'] == expected
    assert preview.json()['negative_prompt'] == expected
    assert not service.peer.requests


def test_weighted_expansion_still_rejects_prompts_over_the_combined_bound(novelai_main):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    saved = client.post(path, json={'name': 'Long weighted tag', 'keywords': ['garden'],
        'prompt': '{' + 'x' * 1500 + '}', 'negative_prompt': '', 'rating': 'general', 'enabled': True})
    assert saved.status_code == 200, saved.text
    enabled = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
        'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert enabled.status_code == 200, enabled.text

    preview = client.post(path + '/preview', json={'text': 'garden', 'prompt': '1.2::' + 'y' * 2990 + '::'})

    assert preview.status_code == 422
    assert not service.peer.requests


def _png_bytes():
    content = io.BytesIO()
    Image.new('RGB', (8, 8), '#171abc').save(content, format='PNG')
    return content.getvalue()


def test_prompt_tags_rank_by_keyword_hits_limit_to_five_and_expose_owned_reference(novelai_main):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    role = service.roles.get(service.role.id)
    enabled = client.put(f'/api/roles/{service.role.id}', json={'name': role.name,
        'profile': role.profile.model_dump(), 'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert enabled.status_code == 200, enabled.text
    uploaded = client.post(f'/api/roles/{service.role.id}/attachments', content=_png_bytes(), headers={
        'content-type': 'image/png', 'x-attachment-filename': 'reference.png'})
    assert uploaded.status_code == 200, uploaded.text
    reference_id = uploaded.json()['id']
    entries = [
        ('Zeta', ['garden'], 'zeta'),
        ('Alpha', ['garden', 'rain'], 'alpha'),
        ('Beta', ['garden', 'rain'], 'beta'),
        ('Gamma', ['garden', 'rain', 'flower'], 'gamma'),
        ('Delta', ['garden', 'rain', 'flower', 'sun'], 'delta'),
        ('Epsilon', ['garden', 'rain', 'flower', 'sun', 'tree'], 'epsilon'),
    ]
    identifiers = {}
    for name, keywords, prompt in entries:
        response = client.post(path, json={'name': name, 'category': 'composition', 'keywords': keywords,
            'prompt': prompt, 'negative_prompt': '', 'rating': 'general', 'enabled': True,
            'reference_attachment_id': reference_id if name == 'Epsilon' else None})
        assert response.status_code == 200, response.text
        identifiers[name] = response.json()['tag']['id']
        if name == 'Epsilon':
            saved = response.json()['tag']
            assert saved['reference_attachment_id'] == reference_id
            assert saved['referenceAttachment']['id'] == reference_id
            assert 'path' not in saved['referenceAttachment']
    preview = client.post(path + '/preview', json={'text': 'garden rain flower sun tree'}).json()
    assert preview['matchedIds'] == [identifiers['Epsilon'], identifiers['Delta'], identifiers['Gamma'],
                                     identifiers['Alpha'], identifiers['Beta']]
    assert preview['prompt'] == 'epsilon, delta, gamma, alpha, beta'
    listed = next(item for item in client.get(path).json()['tags'] if item['id'] == identifiers['Epsilon'])
    assert listed['category'] == 'composition'
    assert listed['referenceAttachment']['mediaType'] == 'image/png'


def test_prompt_tag_reference_must_be_current_role_image_and_legacy_rows_get_defaults(novelai_main):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    other = client.post('/api/roles', json={'name': 'Other role', 'profile': {'profile': 'isolated'}})
    assert other.status_code == 201, other.text
    other_id = other.json()['role']['id']
    foreign = client.post(f'/api/roles/{other_id}/attachments', content=_png_bytes(), headers={
        'content-type': 'image/png', 'x-attachment-filename': 'foreign.png'}).json()['id']
    text = client.post(f'/api/roles/{service.role.id}/attachments', content=b'notes', headers={
        'content-type': 'text/plain', 'x-attachment-filename': 'notes.txt'}).json()['id']
    base = {'name': 'Reference', 'category': 'composition', 'keywords': ['garden'], 'prompt': 'garden',
        'negative_prompt': '', 'rating': 'general', 'enabled': True}
    assert client.post(path, json={**base, 'reference_attachment_id': foreign}).status_code == 422
    assert client.post(path, json={**base, 'reference_attachment_id': text}).status_code == 422
    assert client.post(path, json={**base, 'reference_attachment_id': 'f' * 31}).status_code == 422
    with service.application.ledger.connect() as db:
        db.execute('INSERT INTO novelai_prompt_tags(role_id,id,value) VALUES(?,?,?)', (service.role.id, 'c' * 32,
            json.dumps({'name': 'Legacy', 'keywords': ['legacy'], 'prompt': 'legacy', 'negative_prompt': '',
                        'rating': 'general', 'enabled': True})))
    legacy = next(item for item in client.get(path).json()['tags'] if item['name'] == 'Legacy')
    assert legacy['category'] == 'composition'
    assert legacy['reference_attachment_id'] is None
    assert legacy['referenceAttachment'] is None
