import pytest

from novelai_fixtures import novelai_environment  # noqa: F401


def test_generation_freezes_seed_and_approved_v45_request_without_identity_fields():
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request, generation_payload
    request = normalize_request({'prompt': '1girl, blue sky', 'mode': 'txt2img', 'seed': 171}, NovelaiSettings())
    payload = generation_payload(request)
    assert payload['action'] == 'generate'
    assert payload['model'] == 'nai-diffusion-4-5-curated'
    assert payload['parameters']['seed'] == 171
    assert payload['parameters']['n_samples'] == 1
    assert (payload['parameters']['width'], payload['parameters']['height']) == (1024, 1024)
    assert payload['parameters']['v4_prompt']['caption']['base_caption'] == '1girl, blue sky'
    assert 'image' not in payload['parameters']


def test_generation_accepts_a_bounded_sample_count_and_freezes_it_in_payload():
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request, generation_payload
    request = normalize_request({'prompt': '1girl, blue sky', 'mode': 'txt2img', 'samples': 3}, NovelaiSettings())
    assert request['samples'] == 3
    assert generation_payload(request)['parameters']['n_samples'] == 3
    with pytest.raises(ValueError):
        normalize_request({'prompt': '1girl', 'mode': 'txt2img', 'samples': 5}, NovelaiSettings())


def test_omitted_samples_use_the_owner_selected_service_default_but_explicit_values_win():
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request
    settings = NovelaiSettings(defaultSamples=3)
    assert normalize_request({'prompt': '1girl', 'mode': 'txt2img'}, settings)['samples'] == 3
    assert normalize_request({'prompt': '1girl', 'mode': 'txt2img', 'samples': 1}, settings)['samples'] == 1
    with pytest.raises(ValueError):
        normalize_request({'prompt': '1girl', 'mode': 'txt2img', 'samples': None}, settings)


def test_owner_quality_preferences_are_frozen_and_mapped_to_v45_payload():
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request, generation_payload
    settings = NovelaiSettings(addQualityTags=True, undesiredContentPreset=2)
    request = normalize_request({'prompt': '1girl, blue sky', 'mode': 'txt2img', 'seed': 171}, settings)
    payload = generation_payload(request)

    assert request['qualityToggle'] is True
    assert request['ucPreset'] == 2
    assert payload['parameters']['qualityToggle'] is True
    assert payload['parameters']['ucPreset'] == 2
    with pytest.raises(ValueError):
        normalize_request({'prompt': '1girl', 'mode': 'txt2img', 'qualityToggle': False, 'ucPreset': 0}, settings)


def test_nsfw_mode_selects_only_the_approved_default_model():
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request
    settings = NovelaiSettings(allowedModels=['nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full'], nsfwEnabled=True)
    request = normalize_request({'prompt': '1girl', 'mode': 'txt2img'}, settings)
    assert request['model'] == 'nai-diffusion-4-5-full'
    explicit = normalize_request({'prompt': '1girl', 'mode': 'txt2img', 'model': 'nai-diffusion-4-5-curated'}, settings)
    assert explicit['model'] == 'nai-diffusion-4-5-curated'
    with pytest.raises(ValueError):
        normalize_request({'prompt': '1girl', 'mode': 'txt2img'}, NovelaiSettings(nsfwEnabled=True))


@pytest.mark.parametrize(('mode', 'field'), [('txt2img', 'allowTxt2Img'), ('img2img', 'allowImg2Img')])
def test_disabled_generation_mode_is_rejected_before_request_normalization(mode, field):
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request
    settings = NovelaiSettings(**{field: False})
    arguments = {'prompt': '1girl', 'mode': mode}
    if mode == 'img2img':
        arguments['base_attachment_id'] = 'a' * 32
    with pytest.raises(ValueError, match='disabled'):
        normalize_request(arguments, settings)


@pytest.mark.parametrize('changes', [
    {'seed': True}, {'seed': -1}, {'steps': 29}, {'steps': '3'},
    {'prompt': '中文'}, {'prompt': '   '}, {'model': 'nai-diffusion-4-5-full'},
    {'endpoint': 'https://evil.invalid'}, {'role': 'another'},
    {'size_preset': 'custom', 'custom_width': 65, 'custom_height': 64},
    {'mode': 'img2img'}, {'strength': .7}, {'background': 'false'},
    {'noise': float('nan')}, {'intent': 'scene_cg', 'scene_key': 'fake'},
])
def test_invalid_or_untrusted_generation_is_rejected_before_consumption(changes):
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request
    with pytest.raises(ValueError):
        normalize_request({'prompt': '1girl', 'mode': 'txt2img', **changes}, NovelaiSettings())
