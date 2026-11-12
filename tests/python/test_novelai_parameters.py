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
    restored = normalize_request({'prompt': '1girl', 'mode': 'txt2img', 'qualityToggle': False, 'ucPreset': 0}, settings)
    assert restored['qualityToggle'] is False and restored['ucPreset'] == 0


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


@pytest.mark.parametrize('model', ['nai-diffusion-5-curated', 'nai-diffusion-5-full'])
@pytest.mark.parametrize('mode', ['txt2img', 'img2img'])
def test_v5_freezes_effective_prompts_and_official_request_parameters(model, mode):
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request, generation_payload, replay_request
    settings = NovelaiSettings(allowedModels=[model], defaultModel=model,
        addQualityTags=True, undesiredContentPreset=1)
    arguments = {'prompt': '1girl, blue sky', 'negative_prompt': 'text', 'mode': mode, 'seed': 0}
    if mode == 'img2img':
        arguments['base_attachment_id'] = 'a' * 32
    request = normalize_request(arguments, settings)
    payload = generation_payload(request, base_image=b'owned image')
    assert payload['model'] == model
    assert payload['action'] == ('img2img' if mode == 'img2img' else 'generate')
    effective_prompt = '1girl, blue sky, very aesthetic, masterpiece, no text'
    effective_negative = ('nsfw, ' if model.endswith('full') else '') + (
        'lowres, bad hands, bad anatomy, artistic error, sepia, white haze, worst quality, '
        'very displeasing, jpeg artifacts, 0::ai-generated::, text')
    assert request['prompt'] == arguments['prompt']
    assert request['effective_prompt'] == payload['input'] == effective_prompt
    parameters = payload['parameters']
    assert request['effective_negative_prompt'] == parameters['negative_prompt'] == effective_negative
    assert parameters['v4_prompt']['caption'] == {'base_caption': effective_prompt, 'char_captions': []}
    assert parameters['v4_negative_prompt'] == {
        'caption': {'base_caption': effective_negative, 'char_captions': []}, 'legacy_uc': False}
    assert parameters['params_version'] == request['params_version'] == 4
    assert parameters['scale'] == request['scale'] == 7
    assert parameters['noise_schedule'] == request['noise_schedule'] == 'karras'
    assert parameters['tag_hint_qt'] == 1 and parameters['tag_hint_uc_preset'] == 3
    assert 'qualityToggle' not in parameters and 'ucPreset' not in parameters
    assert parameters['straight_alpha'] is True
    if mode == 'img2img':
        assert parameters['extra_noise_seed'] == -1 and parameters['color_correct'] is False
    replayed = normalize_request(replay_request(request, {'seed': 0}), settings)
    assert generation_payload(replayed, base_image=b'owned image') == payload


def test_v5_full_nsfw_and_none_presets_do_not_add_an_unwanted_nsfw_negative():
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request, generation_payload
    model = 'nai-diffusion-5-full'
    settings = NovelaiSettings(allowedModels=[model], defaultModel=model)
    request = normalize_request({'prompt': 'NSFW, 1girl', 'mode': 'txt2img', 'ucPreset': 2}, settings)
    assert not request['effective_negative_prompt'].startswith('nsfw,')
    request = normalize_request({'prompt': '1girl', 'negative_prompt': 'text', 'mode': 'txt2img',
        'ucPreset': 0, 'qualityToggle': False}, settings)
    payload = generation_payload(request)
    assert payload['input'] == '1girl'
    assert payload['parameters']['negative_prompt'] == 'text'
    assert payload['parameters']['tag_hint_qt'] == payload['parameters']['tag_hint_uc_preset'] == 0


@pytest.mark.parametrize(('prompt', 'expected'), [
    ('{portrait}, 1.2::blue sky::|rain', '{portrait}, 1.2::blue sky::, very aesthetic, masterpiece, no text|rain'),
    ('portrait text:HELLO', 'portrait, very aesthetic, masterpiece, no text text:HELLO'),
    ('text:HELLO', 'very aesthetic, masterpiece, no text text:HELLO'),
    ('||sun|rain||, portrait|room', '||sun|rain||, portrait, very aesthetic, masterpiece, no text|room'),
])
def test_v5_quality_preserves_prompt_chunks_weights_and_rendered_text(prompt, expected):
    from backend.app.novelai_generation.config import NovelaiSettings
    from backend.app.novelai_generation.parameters import normalize_request
    settings = NovelaiSettings(allowedModels=['nai-diffusion-5-curated'], defaultModel='nai-diffusion-5-curated',
        addQualityTags=True, undesiredContentPreset=2)
    request = normalize_request({'prompt': prompt, 'negative_prompt': '|rain', 'mode': 'txt2img'}, settings)
    assert request['prompt'] == prompt
    assert request['effective_prompt'] == expected
    assert request['effective_negative_prompt'].endswith('blank page|rain')
