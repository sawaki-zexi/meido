import type { GenerationParameters, NovelaiConfiguration } from '../../api/novelai';

export function supportsUnicodePrompts(configuration: NovelaiConfiguration | null | undefined, model?: string): boolean {
  const selected = model ?? (configuration?.nsfwEnabled ? configuration.nsfwModel : configuration?.defaultModel);
  return configuration?.modelCatalog?.find((item) => item.id === selected)?.supportsUnicode === true;
}

export function promptInputError(value: string, label: string, unicode = true, required = false): string {
  // JS string iteration counts codepoints, including supplementary characters.
  // The u flag makes the surrogate range match only unpaired surrogate halves.
  if (Array.from(value).length > 4000) return `${label}最多 4000 字符，组合字符分开计数。`;
  if (/[\x00-\x1f\x7f-\x9f\ud800-\udfff\u2028\u2029]/u.test(value))
    return `${label}不能包含换行、控制字符或无效 Unicode。`;
  if (required && /^\p{White_Space}*$/u.test(value)) return '请填写正向提示词。';
  if (!unicode && /[^\x00-\x7f]/.test(value)) return `${label}只能使用英文 ASCII，请选择支持多语言的 V5 或调整输入。`;
  return '';
}

/** Validate editable input before reserving an attempt; the service remains authoritative. */
export function generationInputError(parameters: GenerationParameters, seed: string, configuration: NovelaiConfiguration): string {
  const unicode = supportsUnicodePrompts(configuration, parameters.model);
  for (const [label, value] of [['正向提示词', parameters.prompt], ['负向提示词', parameters.negative_prompt ?? '']]) {
    const error = promptInputError(value, label, unicode, label === '正向提示词');
    if (error) return error;
  }
  const preset = parameters.size_preset ?? 'square';
  const [width, height] = preset === 'custom' ? [parameters.custom_width ?? 0, parameters.custom_height ?? 0]
    : preset === 'portrait' ? [832, 1216] : preset === 'landscape' ? [1216, 832] : [1024, 1024];
  if (preset === 'custom' && [width, height].some((value) => !Number.isInteger(value) || value < 64 || value > 1024 || value % 64 !== 0))
    return '自定义尺寸需为 64–1024 之间的 64 的倍数。';
  if (width * height > configuration.maxPixels) return '图片尺寸超过当前像素上限，请缩小尺寸。';
  if (parameters.model !== undefined && !configuration.allowedModels.includes(parameters.model)) return '请选择当前允许的模型（高级参数）。';
  if (parameters.sampler !== undefined && !configuration.allowedSamplers.includes(parameters.sampler)) return '请选择当前允许的采样器（高级参数）。';
  if (parameters.steps !== undefined && (!Number.isInteger(parameters.steps) || parameters.steps < 1 || parameters.steps > configuration.maxSteps))
    return `采样步数需为 1–${configuration.maxSteps} 之间的整数（高级参数）。`;
  if (!Number.isInteger(parameters.samples ?? 1) || (parameters.samples ?? 1) < 1 || (parameters.samples ?? 1) > 4) return '生成张数需为 1–4（高级参数）。';
  if (seed && (!/^\d+$/.test(seed) || Number(seed) > 4294967295)) return 'Seed 需为 0–4294967295 之间的整数，或留空随机生成（高级参数）。';
  if (parameters.mode === 'img2img') {
    if (!parameters.base_attachment_id) return '图生图需要先添加参考图。';
    if (parameters.strength !== undefined && (!Number.isFinite(parameters.strength) || parameters.strength < .01 || parameters.strength > 1)) return '重绘强度需在 0.01–1 之间。';
    if (parameters.noise !== undefined && (!Number.isFinite(parameters.noise) || parameters.noise < 0 || parameters.noise > .99)) return '噪声需在 0–0.99 之间。';
  }
  return '';
}
