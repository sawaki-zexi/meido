"""Story visuals use Meido's existing bounded, receipt-owning NovelAI service."""
from __future__ import annotations

from ..chat_attachments import resolve_attachment
from ..novelai_generation.context import GenerationContext
from ..task_runtime import TaskOrigin
from .errors import StorySimulationError, StoryProviderUnavailableError, StoryInvalidOutputError


class StoryImageFailure(StorySimulationError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class StoryImageGenerator:
    def __init__(self, novelai, repository):
        self._novelai = novelai
        self._repository = repository

    async def generate(self, *, story, resource):
        repository = self._repository(story['id'])
        app = self._novelai()
        grant = repository.image_request(resource['id'])
        if grant is None:
            settings = story['segment']['openingContext']
            if not settings.get('generate_images'):
                raise StoryImageFailure('images_not_enabled')
            repository.authorize_image(resource['id'], 'auto:' + resource['id'], app.settings.public()['revision'])
            grant = repository.image_request(resource['id'])
        app = self._novelai()
        role_id = story['roleSnapshot']['id']
        reason = app.unavailable_reason(app.roles.get(role_id), include_usage=False)
        if reason:
            raise StoryProviderUnavailableError(reason)
        prompt = str(resource['prompt']).strip()
        if not prompt or not prompt.isascii():
            raise StoryInvalidOutputError('视觉资源缺少英文 NovelAI tags')
        key = f"story:{story['id']}:{grant['request_id']}"
        source = GenerationContext(
            TaskOrigin(role_id, f'role:{role_id}', source_thread_id=f'desktop:{role_id}', source_scope_id=f"story:{story['id']}"),
            key, key, frozenset(), ui_action=True)
        settings = app.settings.public()
        # Shiori Story uses V4.5 full; retain the owner's model allowlist.
        model = 'nai-diffusion-4-5-full' if 'nai-diffusion-4-5-full' in settings['allowedModels'] else (
            settings['nsfwModel'] if settings['nsfwEnabled'] else settings['defaultModel'])
        try:
            result = await app.submit(source, {'prompt': prompt, 'mode': 'txt2img', 'size_preset': 'landscape',
                                              'samples': 1, 'background': False, 'intent': 'user_requested', 'model': model},
                                      expected_revision=grant['expected_revision'], delivery_mode='history_only')
        finally:
            # Cancellation/revocation after dispatch can still leave a genuine
            # paid receipt. Record it, never turn that uncertainty into a retry.
            self.receipt(story, resource)
        if result['status'] != 'succeeded':
            raise StoryImageFailure('generation_' + result['status'])
        attachments = result.get('attachments', [])
        item = resolve_attachment(app.roles.workspace_path(role_id), attachments[0]['id']) if attachments else None
        if item is None or not item.get('path'):
            raise StoryImageFailure('local_media_pending')
        app.keep_history_only(result['attemptId'])
        return item['path']

    def receipt(self, story, resource):
        repository = self._repository(story['id'])
        grant = repository.image_request(resource['id'])
        if not grant:
            return None
        app = self._novelai()
        key = f"story:{story['id']}:{grant['request_id']}"
        row = app.ledger.request(story['roleSnapshot']['id'], f"story:{story['id']}", key)
        if row is None:
            return None
        result = app.ledger.get(row['role_id'], row['source_scope'], row['generation_id'], attempt_id=row['attempt_id'])
        repository.image_receipt(resource['id'], result)
        return result

    def recovered_image(self, story, resource):
        """Resume only a local copy from a verified existing receipt, never HTTP."""
        result = self.receipt(story, resource)
        if not result or result['status'] != 'succeeded' or not result['attachments']:
            return None
        app = self._novelai()
        role_id = story['roleSnapshot']['id']
        if not app.roles.get(role_id):
            return None
        item = resolve_attachment(app.roles.workspace_path(role_id), result['attachments'][0]['id'])
        return item['path'] if item else None
