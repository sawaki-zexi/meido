"""Local-owner Story API; no agent tool exposes these commands or image grants."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .errors import StorySimulationError, StoryNotFoundError
from ..novelai_generation.media import validate_image

_METHODS = {'stories.list', 'stories.get', 'stories.create', 'stories.input', 'stories.continue',
            'stories.retry', 'stories.archive', 'stories.cg.list', 'stories.cg.retry', 'stories.cg.regenerate'}


def story_router(application):
    router = APIRouter()

    def public(value):
        value = deepcopy(value)
        def resource(item):
            if not isinstance(item, dict):
                return
            story_id, identifier = item.get('storyId'), item.get('id')
            if item.get('path'):
                item['path'] = f'/api/stories/{story_id}/resources/{identifier}/image?v={item["updatedAt"]}'
            grant = application()._repository(story_id).image_request(identifier)
            item['generation'] = json.loads(grant['receipt']) if grant and grant['receipt'] else None
        if 'story' in value:
            story = value['story']
            story['status'] = application()._catalog.require_entry(story['id'])['status']
            story['generateImages'] = bool(story['segment']['openingContext'].get('generate_images'))
            for item in story.get('cgGallery', []):
                resource(item)
            resource(story.get('backgroundResource'))
            role = story['roleSnapshot']
            def media(path):
                return f'/api/stories/{story["id"]}/media/{Path(path).name}' if path and Path(path).is_absolute() else None
            role['avatar'] = media(role.get('avatar'))
            role['illustrations'] = [url for path in role.get('illustrations', []) if (url := media(path))]
            role['chatBackgroundUrl'] = media(role.get('chatBackgroundUrl'))
            role['moodIllustrationBindings'] = {mood: media(path) for mood, path in role.get('moodIllustrationBindings', {}).items()}
        for story in value.get('stories', []):
            for item in story.get('items', []):
                resource(item)
        return value

    @router.get('/api/stories/capabilities')
    async def capabilities(role_id: str | None = None):
        return application().capabilities(role_id)

    @router.post('/api/stories/commands')
    async def command(request: Request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 64 * 1024:
                raise HTTPException(413, '故事请求超过大小限制')
        try:
            data = json.loads(content)
            if (not isinstance(data, dict) or set(data) != {'method', 'payload', 'request_id'}
                or data['method'] not in _METHODS or not isinstance(data['payload'], dict)
                or not isinstance(data['request_id'], str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}', data['request_id'])):
                raise ValueError('故事请求格式无效')
            result = await application().handle(data['method'], data['payload'], request_id=data['request_id'], emit_event=lambda event: None)
            return public(result)
        except StoryNotFoundError as error:
            raise HTTPException(404, '故事或图片不存在') from None
        except StorySimulationError as error:
            raise HTTPException(409, str(error)) from None
        except KeyError:
            raise HTTPException(404, '角色或故事已不可用') from None
        except PermissionError as error:
            raise HTTPException(403, str(error)) from None
        except (ValueError, TypeError, RecursionError) as error:
            raise HTTPException(422, str(error)) from None

    def image_response(repository, path):
        candidate = Path(path).resolve()
        root = (repository.db_path.parent / 'assets').resolve()
        if candidate.parent != root or not candidate.is_file():
            raise HTTPException(404, '故事图片文件不可用')
        try:
            media_type, _ = validate_image(candidate.read_bytes(), max_pixels=20 * 1024 * 1024)
        except (OSError, ValueError):
            raise HTTPException(404, '故事图片文件不可用') from None
        return FileResponse(candidate, media_type=media_type, headers={'Cache-Control': 'private, max-age=3600'})

    @router.get('/api/stories/{story_id}/resources/{resource_id}/image')
    async def resource_image(story_id: str, resource_id: str):
        try:
            repository = application()._repository(story_id)
            resource = repository.resource(resource_id)
            if resource['storyId'] != story_id or not resource.get('path'):
                raise StoryNotFoundError()
            return image_response(repository, resource['path'])
        except StoryNotFoundError:
            raise HTTPException(404, '故事图片不存在') from None

    @router.get('/api/stories/{story_id}/media/{identifier}')
    async def frozen_media(story_id: str, identifier: str):
        if not re.fullmatch(r'[0-9a-f]{64}', identifier):
            raise HTTPException(404, '故事图片不存在')
        try:
            repository = application()._repository(story_id)
            role = repository.story_read_model(story_id)['roleSnapshot']
            paths = [role.get('avatar'), role.get('chatBackgroundUrl'), *role.get('illustrations', []), *role.get('moodIllustrationBindings', {}).values()]
            path = next((item for item in paths if item and Path(item).name == identifier), None)
            if not path:
                raise StoryNotFoundError()
            return image_response(repository, path)
        except StoryNotFoundError:
            raise HTTPException(404, '故事图片不存在') from None
    return router
