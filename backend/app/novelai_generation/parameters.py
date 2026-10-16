"""Strict bounded owner-approved request contract.

V4.5 protocol constants adapted from Shiori-Agent 4f266ae (MIT),
Copyright (c) 2026 YinFengWindy. See SHIORI-LICENSE.txt.
"""
from __future__ import annotations

import base64
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, field_validator

from .config import NovelaiSettings


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str = Field(min_length=1, max_length=4000)
    mode: Literal['txt2img', 'img2img']
    negative_prompt: str = Field(default='', max_length=4000)
    base_attachment_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    size_preset: Literal['square', 'landscape', 'portrait', 'custom'] = 'square'
    custom_width: StrictInt | None = Field(default=None, ge=64, le=1024)
    custom_height: StrictInt | None = Field(default=None, ge=64, le=1024)
    steps: StrictInt | None = Field(default=None, ge=1, le=28)
    seed: StrictInt | None = Field(default=None, ge=0, le=2**32-1)
    sampler: str | None = None
    model: str | None = None
    strength: StrictFloat | None = Field(default=None, ge=.01, le=1, allow_inf_nan=False)
    noise: StrictFloat | None = Field(default=None, ge=0, le=.99, allow_inf_nan=False)
    intent: Literal['user_requested', 'scene_cg'] = 'user_requested'
    scene_key: str | None = Field(default=None, max_length=80)
    background: StrictBool = False

    @field_validator('prompt', 'negative_prompt')
    @classmethod
    def ascii_prompt(cls, value: str):
        if not value.isascii() or any(ord(character) < 32 for character in value):
            raise ValueError('English ASCII tags required')
        return value.strip()


def normalize_request(arguments: dict[str, object], settings: NovelaiSettings) -> dict[str, object]:
    request = GenerationRequest.model_validate(arguments)
    if not request.prompt or request.intent != 'user_requested' or request.scene_key is not None:
        raise ValueError('scene generation requires a host-committed observation')
    if request.background:
        # #172 admits background only after its original-source delivery is implemented.
        raise ValueError('background generation is not enabled')
    if request.mode == 'img2img':
        if request.base_attachment_id is None:
            raise ValueError('img2img requires an owned current-source attachment')
    elif {'base_attachment_id', 'strength', 'noise'} & request.model_fields_set:
        raise ValueError('txt2img does not accept image input settings')
    if request.size_preset == 'custom':
        width, height = request.custom_width, request.custom_height
        if width is None or height is None or width % 64 or height % 64:
            raise ValueError('custom dimensions must be provided as multiples of 64')
    else:
        if {'custom_width', 'custom_height'} & request.model_fields_set:
            raise ValueError('custom dimensions require custom preset')
        width, height = {'square': (1024, 1024), 'landscape': (1216, 832), 'portrait': (832, 1216)}[request.size_preset]
    if width * height > settings.maxPixels:
        raise ValueError('pixel budget exceeded')
    steps = request.steps if request.steps is not None else settings.maxSteps
    model, sampler = request.model or settings.defaultModel, request.sampler or settings.defaultSampler
    if model not in settings.allowedModels or sampler not in settings.allowedSamplers or steps > settings.maxSteps:
        raise ValueError('model, sampler or steps not currently allowed')
    value = request.model_dump(exclude_none=True)
    value.update(width=width, height=height, steps=steps, model=model, sampler=sampler,
                 seed=request.seed if request.seed is not None else secrets.randbelow(2**32))
    if request.mode == 'img2img':
        value.update(strength=request.strength if request.strength is not None else .7,
                     noise=request.noise if request.noise is not None else .2)
    return value


def generation_payload(request: dict[str, object], *, base_image: bytes | None = None) -> dict[str, object]:
    parameters = {key: request[key] for key in ('width', 'height', 'steps', 'sampler', 'seed', 'negative_prompt')}
    parameters.update(n_samples=1, scale=5, params_version=3, noise_schedule='native', cfg_rescale=0,
        dynamic_thresholding=False, characterPrompts=[], controlnet_strength=1,
        deliberate_euler_ancestral_bug=False, prefer_brownian=True, reference_image_multiple=[],
        reference_information_extracted_multiple=[], reference_strength_multiple=[], skip_cfg_above_sigma=None,
        use_coords=False, qualityToggle=False, ucPreset=0,
        v4_prompt={'caption': {'base_caption': request['prompt'], 'char_captions': []}, 'use_coords': False, 'use_order': True},
        v4_negative_prompt={'caption': {'base_caption': request['negative_prompt'], 'char_captions': []}})
    if request['mode'] == 'img2img':
        if base_image is None:
            raise ValueError('owned input has not been resolved')
        parameters.update(image=base64.b64encode(base_image).decode('ascii'), strength=request['strength'], noise=request['noise'])
    return {'input': request['prompt'], 'model': request['model'],
            'action': 'img2img' if request['mode'] == 'img2img' else 'generate', 'parameters': parameters}
