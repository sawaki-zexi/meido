"""Model-scoped Unicode prompts through owner, chat, CG and history entry points."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401
from novelai_scene_fixtures import scene_main  # noqa: F401
from test_novelai_scene import owner_turn


V45 = 'nai-diffusion-4-5-curated'
V5 = 'nai-diffusion-5-full'
PROMPT = '1.2::青い瞳, 蓝色长发::, cafe\u0301, 🐈, \u3000sunlight\u3000'
NEGATIVE = '[[雨, 暗い空]], blurry, e\u0301'


def configure(service, model=V5, **changes):
    settings = service.application.settings
    return settings.save({'allowedModels': [V45, V5, 'nai-diffusion-5-curated'], 'defaultModel': model,
                          **changes}, expected_revision=settings.public()['revision'])


def save_tag(client, service):
    path = f'/api/roles/{service.role.id}'
    saved = client.post(path + '/novelai/prompt-tags', json={'name': '雨景', 'keywords': ['garden'],
        'prompt': PROMPT, 'negative_prompt': NEGATIVE, 'rating': 'general', 'enabled': True})
    assert saved.status_code == 200, saved.text
    enabled = client.put(path, json={'name': service.role.name, 'profile': service.role.profile.model_dump(),
                                    'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert enabled.status_code == 200, enabled.text
    return saved.json()['tag']['id']


@pytest.mark.parametrize('model', [V5, 'nai-diffusion-5-curated'])
@pytest.mark.parametrize('prompt', [PROMPT, '🐈' * 4000, 'e\u0301' * 2000], ids=['mixed', 'supplementary', 'combining'])
def test_unicode_codepoint_boundaries_preserve_original_text(model, prompt):
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request, generation_payload
    settings = NovelaiSettings(allowedModels=[model], defaultModel=model)
    request = normalize_request({'mode': 'txt2img', 'prompt': prompt, 'negative_prompt': prompt}, settings)
    assert generation_payload(request)['input'] == prompt
    assert generation_payload(request)['parameters']['negative_prompt'] == prompt
    with pytest.raises(ValueError):
        normalize_request({'mode': 'txt2img', 'prompt': '🐈' * 4001}, settings)


@pytest.mark.parametrize('prompt', ['', ' ', '\u00a0', '\u3000', '\u2002\u2003'])
def test_positive_prompt_requires_non_whitespace_text(prompt):
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request
    with pytest.raises(ValueError):
        normalize_request({'mode': 'txt2img', 'prompt': prompt}, NovelaiSettings(allowedModels=[V5], defaultModel=V5))


def test_ascii_edge_spaces_only_and_legacy_empty_model_default():
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request
    settings = NovelaiSettings(allowedModels=[V5], defaultModel=V5)
    request = normalize_request({'mode': 'txt2img', 'prompt': '  \u3000e\u0301\u00a0  ', 'model': ''}, settings)
    assert request['prompt'] == '\u3000e\u0301\u00a0' and request['model'] == V5


@pytest.mark.parametrize('model', [V45, V5])
@pytest.mark.parametrize('invalid', ['a\nb', 'a\rb', 'a\tb', 'a\x00b', 'a\x7fb', 'a\x85b',
                                   'a\u2028b', 'a\u2029b', 'a\ud800b', 'a\udfffb'])
def test_invalid_characters_rejected_before_any_attempt(novelai_main, model, invalid):
    client, service = novelai_main
    config = configure(service, model)
    path = f'/api/roles/{service.role.id}/generations'
    # JSON escapes allow unpaired surrogates to reach the application contract.
    result = client.post(path, content=json.dumps({'requestId': 'a' * 32,
        'expectedRevision': config['revision'], 'parameters': {**service.request, 'negative_prompt': invalid}}),
        headers={'content-type': 'application/json'})
    assert result.status_code == 422, result.text
    history = client.get(path).json()
    assert history['usage']['accountAttempts'] == 0 and history['generations'] == []
    assert not service.peer.requests


@pytest.mark.parametrize('model', [V5, 'nai-diffusion-5-curated'])
def test_unicode_owner_request_history_and_regeneration_preserve_both_prompts(novelai_main, model):
    client, service = novelai_main
    config = configure(service, model, addQualityTags=True, undesiredContentPreset=1)
    path = f'/api/roles/{service.role.id}/generations'
    parameters = {**service.request, 'prompt': PROMPT, 'negative_prompt': NEGATIVE}
    created = client.post(path, json={'requestId': 'b' * 32, 'expectedRevision': config['revision'],
                                     'parameters': parameters, 'deliveryMode': 'history_only'})
    assert created.status_code == 200, created.text
    original = created.json()['generation']
    assert original['status'] == 'succeeded'
    assert original['parameters']['prompt'] == PROMPT
    assert original['parameters']['negative_prompt'] == NEGATIVE
    sent = service.peer.requests[0]['body']
    assert PROMPT in sent['input'] and NEGATIVE in sent['parameters']['negative_prompt']
    assert client.get(path).json()['generations'][0]['parameters'] == original['parameters']
    replayed = client.post(path + '/' + original['generationId'] + '/regenerate', json={
        'requestId': 'c' * 32, 'expectedRevision': config['revision'], 'sourceAttemptId': original['attemptId'],
        'expectedAttachmentId': original['attachments'][0]['id'], 'parameters': {}, 'deliveryMode': 'history_only'})
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()['generation']['parameters']['prompt'] == PROMPT
    assert service.peer.requests[1]['body']['input'] == sent['input']
    assert service.peer.requests[1]['body']['parameters']['negative_prompt'] == sent['parameters']['negative_prompt']
    denied = client.post(path, json={'requestId': 'd' * 32, 'expectedRevision': config['revision'],
                                    'parameters': {**parameters, 'model': V45}})
    assert denied.status_code == 422 and 'ASCII' in denied.text
    assert len(service.peer.requests) == 2
    assert client.get(path).json()['usage']['accountAttempts'] == 2


def test_unicode_library_drafts_validate_only_selected_content_and_effective_preview_model(novelai_main):
    client, service = novelai_main
    configure(service, V45)
    identifier = save_tag(client, service)
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    assert client.get(path).json()['tags'][0]['prompt'] == PROMPT
    assert client.post(path + '/preview', json={'text': 'unmatched'}).status_code == 200
    rejected = client.post(path + '/preview', json={'text': 'garden'})
    assert rejected.status_code == 422 and 'ASCII' in rejected.text
    preview = client.post(path + '/preview', json={'text': 'ＧＡＲＤＥＮ', 'model': V5})
    assert preview.status_code == 200, preview.text
    assert preview.json()['prompt'] == PROMPT and preview.json()['negative_prompt'] == NEGATIVE
    assert preview.json()['matchedIds'] == [identifier]
    for model in ['unknown', 'nai-diffusion-4-5-full', [], 5, '']:
        assert client.post(path + '/preview', json={'text': 'garden', 'model': model}).status_code == 422
    configure(service, V45, nsfwEnabled=True, nsfwModel=V5)
    assert client.post(path + '/preview', json={'text': 'garden'}).json()['prompt'] == PROMPT
    assert not service.peer.requests


def test_supplementary_and_combining_boundaries_through_api_and_tag_expansion(novelai_main):
    client, service = novelai_main
    config = configure(service)
    path = f'/api/roles/{service.role.id}'
    parameters = {**service.request, 'prompt': '🐈' * 4000, 'negative_prompt': 'e\u0301' * 2000}
    response = client.post(path + '/generations', json={'requestId': 'f' * 32,
        'expectedRevision': config['revision'], 'parameters': parameters, 'deliveryMode': 'history_only'})
    assert response.status_code == 200, response.text
    assert response.json()['generation']['parameters']['prompt'] == parameters['prompt']
    assert service.peer.requests[0]['body']['parameters']['negative_prompt'] == parameters['negative_prompt']
    identifier = save_tag(client, service)
    tag = client.get(path + '/novelai/prompt-tags').json()['tags'][0]
    for field in ('id', 'referenceAttachment'):
        tag.pop(field)
    tag['prompt'] = parameters['prompt']
    saved = client.put(path + '/novelai/prompt-tags/' + identifier, json=tag)
    assert saved.status_code == 200, saved.text
    preview = client.post(path + '/novelai/prompt-tags/preview', json={'text': 'garden'})
    assert preview.status_code == 200 and preview.json()['prompt'] == parameters['prompt']
    oversized = client.post(path + '/novelai/prompt-tags/preview', json={'text': 'garden', 'prompt': 'a'})
    assert oversized.status_code == 422
    tag['prompt'] += '🐈'
    assert client.put(path + '/novelai/prompt-tags/' + identifier, json=tag).status_code == 422
    assert len(service.peer.requests) == 1


@pytest.mark.parametrize('selected', [None, V5, V45])
def test_chat_revalidates_unicode_tags_with_actual_model_and_freezes_language_guidance(novelai_main, selected):
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun
    client, service = novelai_main
    configure(service, V45)
    legacy_tool = service.application.tool_for(service.role.id, is_owner=True)
    configure(service)
    save_tag(client, service)
    role_id = service.role.id
    session_key = f'role:{role_id}'
    service.sessions.create_run_with_messages(AgentRun(runId='multilingual-chat', roleId=role_id,
        sessionKey=session_key, status='running', startedAt=datetime.now(timezone.utc),
        modelSnapshot={'humanInputSource': 'desktop_owner'}), 'Please draw the garden.')
    tool = service.application.tool_for(role_id, is_owner=True)
    assert 'Chinese' in tool.definition.description and 'known appearance' in tool.definition.description
    assert '中文' in tool.definition.input_schema['properties']['prompt']['description']
    assert 'English ASCII' in legacy_tool.definition.description
    assert 'Chinese' not in legacy_tool.definition.input_schema['properties']['prompt']['description']
    expired = asyncio.run(legacy_tool.execute({'prompt': 'portrait'},
        ToolContext(role_id, session_key, 'multilingual-chat', CancellationToken(), 'expired')))
    assert expired.is_error and not service.peer.requests
    preview = client.post(f'/api/roles/{role_id}/novelai/prompt-tags/preview', json={'text': 'garden', 'model': V5})
    assert preview.status_code == 200
    arguments = {'prompt': 'portrait', **({'model': selected} if selected else {})}
    result = asyncio.run(tool.execute(arguments,
        ToolContext(role_id, session_key, 'multilingual-chat', CancellationToken(), 'draw')))
    assert result.is_error == (selected == V45)
    if selected == V45:
        assert not service.peer.requests
        assert client.get(f'/api/roles/{role_id}/generations').json()['usage']['accountAttempts'] == 0
    else:
        assert len(service.peer.requests) == 1
        assert service.peer.requests[0]['body']['input'] == 'portrait, ' + PROMPT
        assert service.peer.requests[0]['body']['parameters']['negative_prompt'] == NEGATIVE


def test_scene_auxiliary_uses_frozen_v5_language_and_unicode_tags(scene_main):
    client, service, peer, _ = scene_main
    configure(service)
    save_tag(client, service)
    peer.observation.update(prompt='1.3::白いドレス, 青い瞳::', negative_prompt='[[文字, 杂乱]]')
    result, _ = owner_turn(client, service, 'The garden')
    assert result['status'] == 'succeeded'
    conversion = next(item for item in peer.requests if item['model'] == 'fixture-scene'
                      and 'visual_description' in json.loads(item['messages'][1]['content']))
    instruction = conversion['messages'][0]['content']
    assert 'Chinese' in instruction and 'known hair color' in instruction
    assert 'English ASCII tags' not in instruction
    sent = service.peer.requests[0]['body']
    assert PROMPT in sent['input'] and peer.observation['prompt'] in sent['input']
    assert NEGATIVE in sent['parameters']['negative_prompt']
    assert result['parameters']['prompt'] == sent['input']
    assert len(service.peer.requests) == 1


def test_unicode_preview_cannot_authorize_a_later_disallowed_model(novelai_main):
    client, service = novelai_main
    configure(service)
    save_tag(client, service)
    path = f'/api/roles/{service.role.id}'
    preview = client.post(path + '/novelai/prompt-tags/preview', json={'text': 'garden', 'model': V5})
    assert preview.status_code == 200
    settings = service.application.settings
    changed = settings.save({'allowedModels': [V45], 'defaultModel': V45,
        'managedModelIds': [V45, V5, 'nai-diffusion-5-curated']}, expected_revision=settings.public()['revision'])
    denied = client.post(path + '/generations', json={'requestId': 'e' * 32,
        'expectedRevision': changed['revision'], 'parameters': {**service.request,
            'model': V5, 'prompt': preview.json()['prompt'], 'negative_prompt': preview.json()['negative_prompt']}})
    assert denied.status_code == 422
    assert not service.peer.requests
    assert client.get(path + '/generations').json()['usage']['accountAttempts'] == 0


@pytest.mark.parametrize('model', [V45, V5])
@pytest.mark.parametrize('field', ['prompt', 'negative_prompt'])
def test_legacy_del_draft_remains_readable_and_correctable_but_cannot_be_used(novelai_main, model, field):
    from backend.app.agent_runtime.provider import CancellationToken
    from backend.app.agent_runtime.tools import ToolContext
    from backend.app.models import AgentRun
    client, service = novelai_main
    config = configure(service, model)
    role_id = service.role.id
    path = f'/api/roles/{role_id}'
    tags_path = path + '/novelai/prompt-tags'
    enabled = client.put(path, json={'name': service.role.name, 'profile': service.role.profile.model_dump(),
                                    'agentConfig': {'novelai': {'promptTagsEnabled': True}}})
    assert enabled.status_code == 200, enabled.text
    legacy = {'name': 'Legacy', 'keywords': ['garden'], 'prompt': 'portrait',
              'negative_prompt': 'rain', 'rating': 'general', 'enabled': True}
    legacy[field] += '\x7f'
    disabled = {**legacy, 'name': 'Disabled', 'keywords': ['unrelated'], 'enabled': False}
    stored = {'a' * 32: json.dumps(legacy), 'b' * 32: json.dumps(disabled)}
    with service.application.ledger.connect() as db:
        db.executemany('INSERT INTO novelai_prompt_tags(role_id,id,value) VALUES(?,?,?)',
                       [(role_id, identifier, value) for identifier, value in stored.items()])

    listed = client.get(tags_path)
    assert listed.status_code == 200, listed.text
    assert listed.json()['revision'] == 0
    assert [tag[field] for tag in listed.json()['tags']] == [legacy[field], disabled[field]]
    assert client.post(tags_path, json=legacy).status_code == 422
    assert client.put(tags_path + '/' + 'a' * 32, json=legacy).status_code == 422
    unmatched = client.post(tags_path + '/preview', json={'text': 'unrelated', 'prompt': 'blue sky'})
    assert unmatched.status_code == 200, unmatched.text
    assert unmatched.json()['prompt'] == 'blue sky' and unmatched.json()['matchedIds'] == []
    matched = client.post(tags_path + '/preview', json={'text': 'garden', 'prompt': 'blue sky'})
    assert matched.status_code == 422 and '控制' in matched.text
    submitted = client.post(path + '/generations', json={'requestId': 'a' * 32,
        'expectedRevision': config['revision'], 'parameters': {**service.request, field: legacy[field]}})
    assert submitted.status_code == 422

    session_key = f'role:{role_id}'
    def draw(run_id, text):
        _, _, placeholder = service.sessions.create_run_with_messages(AgentRun(runId=run_id, roleId=role_id,
            sessionKey=session_key, status='running', startedAt=datetime.now(timezone.utc),
            modelSnapshot={'humanInputSource': 'desktop_owner'}), text)
        result = asyncio.run(service.application.tool_for(role_id, is_owner=True).execute({'prompt': 'blue sky'},
            ToolContext(role_id, session_key, run_id, CancellationToken(), 'draw')))
        service.sessions.finish_run(run_id, status='completed', assistant_message_id=placeholder.id,
                                    assistant_content='Drawing request checked.', turn_count=1)
        return result
    rejected = draw('legacy-matched', 'garden')
    assert rejected.is_error and rejected.details['errorCode'] == 'invalid_prompt'
    assert not service.peer.requests
    assert client.get(path + '/generations').json()['usage']['accountAttempts'] == 0
    with service.application.ledger.connect() as db:
        assert {row['id']: row['value'] for row in db.execute(
            'SELECT id,value FROM novelai_prompt_tags WHERE role_id=?', (role_id,))} == stored

    unrelated = draw('legacy-unmatched', 'unrelated')
    assert not unrelated.is_error and len(service.peer.requests) == 1
    assert service.peer.requests[0]['body']['input'] == 'blue sky'
    corrected = {**legacy, field: legacy[field].replace('\x7f', '')}
    updated = client.put(tags_path + '/' + 'a' * 32, json=corrected)
    assert updated.status_code == 200, updated.text
    assert updated.json()['tag'][field] == corrected[field]
    preview = client.post(tags_path + '/preview', json={'text': 'garden'})
    assert preview.status_code == 200 and preview.json()[field] == corrected[field]
    with service.application.ledger.connect() as db:
        assert db.execute('SELECT value FROM novelai_prompt_tags WHERE role_id=? AND id=?',
                          (role_id, 'b' * 32)).fetchone()['value'] == stored['b' * 32]


def test_legacy_del_read_compatibility_preserves_other_stored_record_validation(novelai_main):
    client, service = novelai_main
    path = f'/api/roles/{service.role.id}/novelai/prompt-tags'
    legacy = {'name': 'Legacy', 'keywords': ['garden'], 'prompt': 'portrait\x7f',
              'negative_prompt': '', 'rating': 'general', 'enabled': True}
    for changes in ({'prompt': 'portrait\x7f\n'}, {'prompt': '中文\x7f'}, {'prompt': 'x' * 4000 + '\x7f'},
                    {'rating': 'invalid'}, {'enabled': 1}, {'unexpected': True}):
        with service.application.ledger.connect() as db:
            db.execute('INSERT OR REPLACE INTO novelai_prompt_tags(role_id,id,value) VALUES(?,?,?)',
                       (service.role.id, 'a' * 32, json.dumps({**legacy, **changes})))
        assert client.get(path).status_code == 422
