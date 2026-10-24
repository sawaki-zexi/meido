"""Owner settings never round-trip the saved NovelAI secret."""
import pytest


class Credentials:
    def __init__(self):
        self.values = {}
        self.reads = 0

    def get(self, key):
        self.reads += 1
        return self.values.get(key, '')

    def set(self, key, value):
        self.values[key] = value


def test_owner_can_configure_service_without_reading_or_exposing_saved_token(tmp_path):
    from backend.app.novelai_generation.config import NovelaiSettingsStore
    credentials = Credentials()
    settings = NovelaiSettingsStore(tmp_path / 'novelai.db', credentials)
    assert settings.public()['enabled'] is False
    assert settings.public()['tokenConfigured'] is False
    settings.set_token('fixture-secret-171')
    revision = settings.public()['revision']
    result = settings.save({'enabled': True}, expected_revision=revision)
    assert result['enabled'] is True and result['tokenConfigured'] is True
    assert credentials.reads == 0
    assert 'fixture-secret-171' not in str(result)
    with pytest.raises(ValueError, match='revision'):
        settings.save({'enabled': False}, expected_revision=revision)
    settings.clear_token()
    assert settings.public()['tokenConfigured'] is False
    assert credentials.values['token'] == ''


def test_owner_prompt_preferences_persist_and_invalidate_old_consumption_consent(tmp_path):
    from backend.app.novelai_generation.config import NovelaiSettingsStore
    credentials = Credentials()
    database = tmp_path / 'novelai.db'
    settings = NovelaiSettingsStore(database, credentials)
    settings.set_token('fixture-secret-171')
    configured = settings.save({'enabled': True, 'manualEnabled': True,
        'confirmManualConsumption': True}, expected_revision=settings.public()['revision'])
    changed = settings.save({'addQualityTags': True, 'undesiredContentPreset': 2},
        expected_revision=configured['revision'])

    assert changed['addQualityTags'] is True
    assert changed['undesiredContentPreset'] == 2
    assert changed['manualConsentRevision'] == 0
    reopened = NovelaiSettingsStore(database, credentials)
    assert reopened.public()['addQualityTags'] is True
    assert reopened.public()['undesiredContentPreset'] == 2


def test_nsfw_default_model_requires_explicit_full_model_allowance(tmp_path):
    from backend.app.novelai_generation.config import NovelaiSettingsStore
    credentials = Credentials()
    settings = NovelaiSettingsStore(tmp_path / 'novelai.db', credentials)
    with pytest.raises(ValueError, match='NSFW default model'):
        settings.save({'nsfwEnabled': True}, expected_revision=settings.public()['revision'])
    expanded = settings.save({'allowedModels': ['nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full'],
        'nsfwEnabled': True}, expected_revision=settings.public()['revision'])
    assert expanded['nsfwEnabled'] is True and expanded['nsfwModel'] == 'nai-diffusion-4-5-full'
    narrowed = settings.save({'allowedModels': ['nai-diffusion-4-5-curated'], 'nsfwEnabled': False},
        expected_revision=expanded['revision'])
    assert narrowed['nsfwEnabled'] is False and narrowed['nsfwModel'] == 'nai-diffusion-4-5-full'


def test_generation_mode_switches_default_on_and_revoke_manual_consent(tmp_path):
    from backend.app.novelai_generation.config import NovelaiSettingsStore
    credentials = Credentials()
    settings = NovelaiSettingsStore(tmp_path / 'novelai.db', credentials)
    settings.set_token('fixture-secret-171')
    configured = settings.save({'enabled': True, 'manualEnabled': True,
        'confirmManualConsumption': True}, expected_revision=settings.public()['revision'])
    assert configured['allowTxt2Img'] is True and configured['allowImg2Img'] is True
    disabled = settings.save({'allowImg2Img': False}, expected_revision=configured['revision'])
    assert disabled['allowTxt2Img'] is True and disabled['allowImg2Img'] is False
    assert disabled['manualConsentRevision'] == 0
    reopened = NovelaiSettingsStore(tmp_path / 'novelai.db', credentials)
    assert reopened.public()['allowImg2Img'] is False


def test_default_samples_is_bounded_and_changes_invalidate_manual_consent(tmp_path):
    from backend.app.novelai_generation.config import NovelaiSettingsStore
    credentials = Credentials()
    settings = NovelaiSettingsStore(tmp_path / 'novelai.db', credentials)
    settings.set_token('fixture-secret-171')
    approved = settings.save({'enabled': True, 'manualEnabled': True,
        'confirmManualConsumption': True}, expected_revision=settings.public()['revision'])
    changed = settings.save({'defaultSamples': 4}, expected_revision=approved['revision'])
    assert changed['defaultSamples'] == 4
    assert changed['manualConsentRevision'] == 0
    with pytest.raises(ValueError):
        settings.save({'defaultSamples': 5}, expected_revision=changed['revision'])
