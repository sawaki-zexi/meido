import type { GenerationParameters, NovelaiConfiguration } from '../../api/novelai';

/** Validate editable input before reserving an attempt; the service remains authoritative. */
export function generationInputError(parameters: GenerationParameters, seed: string, configuration: NovelaiConfiguration): string {
  for (const [label, value] of [['正向提示词', parameters.prompt], ['负向提示词', parameters.negative_prompt ?? '']]) {
    if (value.length > 4000 || /[^\x20-\x7e]/.test(value)) return `${label}最多 4000 字符，只能使用英文 ASCII，不能包含换行或控制符。`;
  }
  if (!parameters.prompt.trim()) return '请填写正向提示词。';
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
