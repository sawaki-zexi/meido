import { expect, it } from 'vitest';
import type { NovelaiConfiguration } from '../../api/novelai';
import { generationInputError } from './generationValidation';

const v45 = 'nai-diffusion-4-5-curated';
const v5 = 'nai-diffusion-5-full';
const configuration = {
  allowedModels: [v45, v5], defaultModel: v5, nsfwEnabled: false, nsfwModel: v5,
  modelCatalog: [{ id: v45, label: 'V4.5 Curated', supportsUnicode: false },
    { id: v5, label: 'V5 Full', supportsUnicode: true }],
  maxPixels: 1048576, maxSteps: 28, allowedSamplers: ['k_euler_ancestral'],
} as NovelaiConfiguration;

it.each(['1.2::青い瞳, 蓝色长发::, cafe\u0301, 🐈', '🐈'.repeat(4000), 'e\u0301'.repeat(2000), '👩‍🎨, \u3000sunlight\u3000'])(
  'accepts V5 Unicode codepoints without changing the editable value', (prompt) => {
    const parameters = { prompt, negative_prompt: prompt, mode: 'txt2img' as const };
    expect(generationInputError(parameters, '', configuration)).toBe('');
    expect(parameters.prompt).toBe(prompt);
    expect(generationInputError({ ...parameters, model: v45 }, '', configuration)).toContain('ASCII');
  });

it.each(['a\nb', 'a\rb', 'a\tb', 'a\x00b', 'a\x7fb', 'a\x85b', 'a\u2028b', 'a\u2029b', 'a\ud800b', 'a\udfffb', '🐈'.repeat(4001)])(
  'rejects invalid characters or excessive codepoint counts for both models', (prompt) => {
    for (const model of [v45, v5]) {
      expect(generationInputError({ prompt: 'portrait', negative_prompt: prompt, mode: 'txt2img', model }, '', configuration)).not.toBe('');
    }
  });

it.each(['', ' ', '\u00a0', '\u3000', '\u2002\u2003'])(
  'requires a non-whitespace positive prompt', (prompt) => {
    expect(generationInputError({ prompt, mode: 'txt2img' }, '', configuration)).toBe('请填写正向提示词。');
  });

it('uses the effective NSFW model and keeps missing catalog capabilities conservative', () => {
  const parameters = { prompt: '蓝色长发', mode: 'txt2img' as const };
  expect(generationInputError(parameters, '', { ...configuration, defaultModel: v45, nsfwEnabled: true })).toBe('');
  expect(generationInputError(parameters, '', { ...configuration, modelCatalog: undefined })).toContain('ASCII');
});
