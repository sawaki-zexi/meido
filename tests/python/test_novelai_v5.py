"""V5 uses the existing owner API, bounded HTTP peer and managed media history."""
import base64
import io
import zipfile

from PIL import Image
import pytest

from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


@pytest.mark.parametrize('model', ['nai-diffusion-5-curated', 'nai-diffusion-5-full'])
@pytest.mark.parametrize('mode', ['txt2img', 'img2img'])
def test_v5_generation_preserves_actual_parameters_and_transparency_through_download(novelai_main, model, mode):
    client, service = novelai_main
    configuration = client.get('/api/services/novelai').json()
    saved = client.put('/api/services/novelai', json={'expectedRevision': configuration['revision'],
        'allowedModels': [model], 'defaultModel': model, 'addQualityTags': True, 'undesiredContentPreset': 1})
    assert saved.status_code == 200, saved.text
    image = Image.new('RGBA', (64, 64), (20, 60, 90, 128))
    image.putpixel((0, 0), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image_format = 'PNG' if mode == 'txt2img' else 'WEBP'
    image.save(buffer, format=image_format, lossless=True)
    content = buffer.getvalue()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('image_0.' + image_format.lower(), content)
    service.peer.body = archive.getvalue()
    parameters = {**service.request, 'mode': mode, 'negative_prompt': 'rain'}
    if mode == 'img2img':
        uploaded = client.post(f'/api/roles/{service.role.id}/attachments', content=content,
            headers={'Content-Type': 'image/webp', 'x-attachment-filename': 'base.webp'})
        assert uploaded.status_code == 200, uploaded.text
        parameters.update(base_attachment_id=uploaded.json()['id'], strength=.45, noise=.1)
    path = f'/api/roles/{service.role.id}/generations'
    request = {'requestId': '35635635635635635635635635635635', 'expectedRevision': saved.json()['revision'],
               'parameters': parameters}
    response = client.post(path, json=request)
    assert response.status_code == 200, response.text
    result = response.json()['generation']
    assert result['status'] == 'succeeded' and result['deliveryStatus'] == 'sent'
    sent = service.peer.requests[0]['body']
    assert result['parameters']['model'] == sent['model'] == model
    assert result['parameters']['mode'] == mode
    assert result['parameters']['prompt'] == parameters['prompt']
    assert result['parameters']['effective_prompt'] == sent['input']
    assert result['parameters']['effective_negative_prompt'] == sent['parameters']['negative_prompt']
    assert sent['parameters']['straight_alpha'] is True
    if mode == 'img2img':
        assert base64.b64decode(sent['parameters']['image']) == content
        assert sent['parameters']['strength'] == .45 and sent['parameters']['noise'] == .1
    repeated = client.post(path, json=request)
    assert repeated.json()['generation']['attemptId'] == result['attemptId']
    assert len(service.peer.requests) == 1
    page = client.get(path).json()
    assert page['generations'][0]['parameters'] == result['parameters']
    assert page['usage']['accountAttempts'] == page['usage']['manualAttempts'] == 1
    attachment = result['attachments'][0]
    downloaded = client.get(f"/api/roles/{service.role.id}/attachments/{attachment['id']}")
    assert downloaded.status_code == 200 and downloaded.content == content
    with Image.open(io.BytesIO(downloaded.content)) as stored:
        assert stored.getchannel('A').getextrema() == (0, 128)


@pytest.mark.parametrize('changes', [
    {'model': 'unknown'}, {'model': 'nai-diffusion-5-full-medium'}, {'model': 'nai-diffusion-4-5-full'},
    {'prompt': '中文\n换行'}, {'negative_prompt': 'line\nbreak'}, {'steps': 29},
    {'mode': 'img2img', 'base_attachment_id': 'a' * 32},
    {'effective_prompt': 'injected'},
])
def test_v5_invalid_or_unowned_inputs_do_not_use_an_attempt(novelai_main, changes):
    client, service = novelai_main
    settings = service.application.settings
    configured = settings.save({'allowedModels': ['nai-diffusion-5-curated'], 'defaultModel': 'nai-diffusion-5-curated'},
        expected_revision=settings.public()['revision'])
    path = f'/api/roles/{service.role.id}/generations'
    result = client.post(path, json={'requestId': '35635635635635635635635635635636',
        'expectedRevision': configured['revision'], 'parameters': {**service.request, **changes}})
    assert result.status_code in (403, 422), result.text
    assert not service.peer.requests
    history = client.get(path).json()
    assert not history['generations'] and history['usage']['accountAttempts'] == 0


def test_v5_full_medium_freezes_intensity_and_provider_model_in_the_real_generation_path(novelai_main):
    client, service = novelai_main
    configuration = client.get('/api/services/novelai').json()
    saved = client.put('/api/services/novelai', json={'expectedRevision': configuration['revision'],
        'allowedModels': ['nai-diffusion-5-full'], 'defaultModel': 'nai-diffusion-5-full',
        'undesiredContentPreset': 0})
    assert saved.status_code == 200, saved.text
    parameters = {**service.request, 'model': 'nai-diffusion-5-full', 'intensity': 'medium',
                  'sampler': 'k_euler_ancestral', 'steps': 14, 'ucPreset': 0}
    path = f'/api/roles/{service.role.id}/generations'
    response = client.post(path, json={'requestId': '35835835835835835835835835835835',
        'expectedRevision': saved.json()['revision'], 'parameters': parameters})
    assert response.status_code == 200, response.text
    result = response.json()['generation']
    assert result['status'] == 'succeeded'
    assert result['parameters']['model'] == 'nai-diffusion-5-full'
    assert result['parameters']['intensity'] == 'medium'
    assert result['parameters']['steps'] == 14
    assert result['parameters']['sampler'] == 'k_euler_ancestral'
    assert service.peer.requests[0]['body']['model'] == 'nai-diffusion-5-full-medium'
    assert service.peer.requests[0]['body']['parameters']['steps'] == 14
