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
    intensities: tuple[str, ...] = ()


MODEL_CATALOG = {
    'nai-diffusion-4-5-curated': ModelCapabilities('V4.5 Curated', 'V4.5', 3, 5, 'native'),
    'nai-diffusion-4-5-full': ModelCapabilities('V4.5 Full', 'V4.5', 3, 5, 'native'),
    'nai-diffusion-5-curated': ModelCapabilities('V5 Curated', 'V5', 4, 7, 'karras', True),
    'nai-diffusion-5-full': ModelCapabilities('V5 Full', 'V5', 4, 7, 'karras', True, ('high', 'medium')),
}
MODELS = tuple(MODEL_CATALOG)
LEGACY_MODELS = ('nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full')

V5_FULL_MEDIUM_PROVIDER_MODEL = 'nai-diffusion-5-full-medium'
V5_FULL_MEDIUM_STEPS = 14
V5_FULL_MEDIUM_SAMPLER = 'k_euler_ancestral'
