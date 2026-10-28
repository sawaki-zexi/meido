"""Focused Story feature smoke: durable playback and the existing real image ledger."""
import asyncio
import io
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from novelai_fixtures import novelai_environment, novelai_service  # noqa: F401
from backend.app.story.api import story_router
from backend.app.story.application import StoryApplication
from backend.app.story.errors import StoryInvalidOutputError
from backend.app.story.models import DirectorDraft, StoryBeatDraft, StoryScene


class Director:
    def __init__(self):
        self.fail = False

    async def generate(self, *, context, input_text, opening):
        if self.fail:
            raise StoryInvalidOutputError('fixture failure')
        return DirectorDraft(
            beats=(StoryBeatDraft('风从窗边吹来。'), StoryBeatDraft('欢迎回来。', 'dialogue')),
            visual_prompt='empty room, sunlight' if opening else '1girl, smile, room',
            visual_type='scene' if opening else 'character',
            current_scene=StoryScene('room', '客厅', (context.role_snapshot['id'], 'player')))


def test_story_create_play_save_gallery_retry_and_reopen(novelai_service, tmp_path):
    service = novelai_service
    director = Director()
    # Integrate the real shared role library: mood-bound portrait must beat the
    # older card image, and the chosen background must not become a character.
    def picture(color):
        buffer = io.BytesIO()
        Image.new('RGB', (64, 64), color).save(buffer, format='PNG')
        return buffer.getvalue()
    service.roles.set_avatar(service.role.id, picture('green'), 'image/png', card=True)
    role = service.roles.add_illustration(service.role.id, picture('yellow'), 'image/png', set_background=True)
    background_id = role.chatBackgroundId
    role = service.roles.add_illustration(role.id, picture('red'), 'image/png')
    role = service.roles.add_illustration(role.id, picture('blue'), 'image/png')
    portrait_id = role.illustrations[-1].id
    service.roles.update_illustrations(role.id, mood_bindings={'开心': portrait_id}, default_mood='开心')
    root = tmp_path / 'story'
    def create_application():
        return StoryApplication(workspace=root, role_store=service.roles, model_configuration=lambda _: None,
            model_adapter=lambda: None, novelai=lambda: service.application, director=director)

    async def scenario():
        story_app = create_application()
        app = FastAPI()
        app.include_router(story_router(lambda: story_app))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            async def command(method, payload, identifier=None):
                response = await client.post('/api/stories/commands', json={'method': method, 'payload': payload, 'request_id': identifier or uuid4().hex})
                assert response.status_code == 200, response.text
                return response.json()
            capability = (await client.get('/api/stories/capabilities', params={'role_id': service.role.id})).json()
            payload = {'creation_id': uuid4().hex, 'title': '回家', 'background': '重逢的一天',
                       'story_date': '2026-10-28', 'time_band': '下午', 'role_id': service.role.id,
                       'player_profile': {'display_name': '旅人', 'appearance': '黑发青年', 'identity': '邻居'},
                       'generate_images': True, 'expected_novelai_revision': capability['novelaiRevision']}
            created = await command('stories.create', payload)
            story_id = created['story']['id']
            await story_app.drain()
            story = (await command('stories.get', {'story_id': story_id}))['story']
            assert story['beats'][1]['speaker'] == service.role.name
            snapshot = story['roleSnapshot']
            assert snapshot['defaultMood'] == '开心'
            assert snapshot['moodIllustrationBindings']['开心'] == snapshot['illustrations'][0]
            assert (await client.get(snapshot['illustrations'][0])).content == picture('blue')
            assert (await client.get(snapshot['chatBackgroundUrl'])).content == picture('yellow')
            assert snapshot['chatBackgroundUrl'] not in snapshot['illustrations']
            service.roles.remove_illustration(role.id, portrait_id)
            service.roles.remove_illustration(role.id, background_id)
            assert (await client.get(snapshot['illustrations'][0])).content == picture('blue')
            assert (await client.get(snapshot['chatBackgroundUrl'])).content == picture('yellow')
            assert story['backgroundResource']['status'] == 'ready'
            assert (await client.get(story['backgroundResource']['path'])).headers['content-type'] == 'image/png'
            assert len(service.peer.requests) == 1
            await command('stories.create', payload)  # Exact replay is read-only after commit.
            await story_app.drain()
            assert len(service.peer.requests) == 1
            await command('stories.input', {'story_id': story_id, 'expected_revision': story['revision'], 'input': '一起喝茶吧。'})
            await story_app.drain()
            story = (await command('stories.get', {'story_id': story_id}))['story']
            assert len(story['beats']) == 4
            cg = story['cgGallery'][-1]
            assert cg['kind'] == 'cg' and cg['status'] == 'ready' and cg['generation']['status'] == 'succeeded'
            request_id = uuid4().hex
            retry = {'story_id': story_id, 'resource_id': cg['id'], 'expected_novelai_revision': capability['novelaiRevision']}
            await command('stories.cg.regenerate', retry, request_id)
            await story_app.drain()
            await command('stories.cg.regenerate', retry, request_id)
            await story_app.drain()
            assert len(service.peer.requests) == 3
            assert len((await command('stories.get', {'story_id': story_id}))['story']['beats']) == 4
            await command('stories.archive', {'story_id': story_id, 'archived': True})
            assert not (await command('stories.list', {}))['stories']
            assert (await command('stories.cg.list', {}))['stories'][0]['items'][-1]['status'] == 'ready'
            await story_app.aclose()
            story_app = create_application()
            saved = (await command('stories.get', {'story_id': story_id}))['story']
            assert saved['status'] == 'archived' and len(saved['beats']) == 4
            await command('stories.archive', {'story_id': story_id, 'archived': False})
            assert (await command('stories.list', {}))['stories'][0]['story_id'] == story_id
            await story_app.aclose()
    asyncio.run(scenario())


def test_failed_story_can_resume_without_losing_committed_facts(novelai_service, tmp_path):
    service = novelai_service
    director = Director()
    app = StoryApplication(workspace=tmp_path / 'story', role_store=service.roles,
        model_configuration=lambda _: None, model_adapter=lambda: None, novelai=lambda: service.application, director=director)
    async def run():
        payload = {'creation_id': uuid4().hex, 'title': '雨天', 'background': '图书室', 'story_date': '2026-10-28',
                   'time_band': '夜晚', 'role_id': service.role.id, 'generate_images': False,
                   'player_profile': {'display_name': '访客', 'appearance': '黑发', 'identity': '同学'}}
        director.fail = True
        result = await app.handle('stories.create', payload, request_id=uuid4().hex, emit_event=lambda _: None)
        await app.drain()
        story_id = result['story']['id']
        failed_opening = app._repository(story_id).story_read_model(story_id)
        with pytest.raises(ValueError, match='开场'):
            await app.handle('stories.continue', {'story_id': story_id, 'expected_revision': failed_opening['revision']}, request_id=uuid4().hex, emit_event=lambda _: None)
        await app.handle('stories.create', payload, request_id=uuid4().hex, emit_event=lambda _: None)
        await app.drain()
        assert app._repository(story_id).story_read_model(story_id)['turns'] == failed_opening['turns']
        director.fail = False
        await app.handle('stories.retry', {'story_id': story_id, 'turn_id': failed_opening['turns'][0]['id'],
            'expected_revision': failed_opening['revision']}, request_id=uuid4().hex, emit_event=lambda _: None)
        await app.drain()
        director.fail = True
        story = app._repository(story_id).story_read_model(story_id)
        await app.handle('stories.continue', {'story_id': story_id, 'expected_revision': story['revision']}, request_id=uuid4().hex, emit_event=lambda _: None)
        await app.drain()
        failed = app._repository(story_id).story_read_model(story_id)
        assert len(failed['beats']) == 2 and failed['turns'][-1]['status'] == 'failed'
        director.fail = False
        payload = {'story_id': story_id, 'turn_id': failed['turns'][-1]['id'], 'expected_revision': failed['revision']}
        request_id = uuid4().hex
        await app.handle('stories.retry', payload, request_id=request_id, emit_event=lambda _: None)
        await app.drain()
        await app.handle('stories.retry', payload, request_id=request_id, emit_event=lambda _: None)
        await app.drain()
        resumed = app._repository(story_id).story_read_model(story_id)
        assert len(resumed['beats']) == 4 and resumed['turns'][-1]['status'] == 'committed'
        assert not service.peer.requests  # Text-only never acquires an image consumption claim.
        await app.aclose()
    asyncio.run(run())
