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
