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
