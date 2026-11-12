"""The bounded model catalog supported by Meido's NovelAI adapter."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    label: str
    version: str
    params_version: int
    scale: int
    noise_schedule: str
    supports_unicode: bool = False


MODEL_CATALOG = {
    'nai-diffusion-4-5-curated': ModelCapabilities('V4.5 Curated', 'V4.5', 3, 5, 'native'),
    'nai-diffusion-4-5-full': ModelCapabilities('V4.5 Full', 'V4.5', 3, 5, 'native'),
    'nai-diffusion-5-curated': ModelCapabilities('V5 Curated', 'V5', 4, 7, 'karras', True),
    'nai-diffusion-5-full': ModelCapabilities('V5 Full', 'V5', 4, 7, 'karras', True),
}
MODELS = tuple(MODEL_CATALOG)
LEGACY_MODELS = ('nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full')
