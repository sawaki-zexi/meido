import type { NovelaiAvailability, NovelaiConfiguration, GenerationPage } from '../../api/novelai';

// Derive from the displayed fields so unsaved settings and older servers stay coherent.
export function generationAvailability(config: NovelaiConfiguration, usage?: GenerationPage['usage']): NovelaiAvailability {
  if (!config.enabled) return { available: false, code: 'service_disabled', message: 'NovelAI 生图已关闭，请在插件设置中启用。' };
  if (config.credentialCleanupPending) return { available: false, code: 'credential_cleanup_pending', message: 'NovelAI 凭据操作未完成，请重新保存或清空凭据。' };
  if (!config.tokenConfigured) return { available: false, code: 'credential_missing', message: '尚未配置 NovelAI 凭据，请在插件设置中保存 Token。' };
  if (config.allowTxt2Img === false && config.allowImg2Img === false)
    return { available: false, code: 'generation_modes_disabled', message: '文生图和图生图均已关闭，请在 NovelAI 设置中开启所需模式。' };
  if (usage && (usage.accountAttempts >= config.dailyAttemptLimit || usage.manualAttempts >= (config.manualDailyAttempts ?? 10)))
    return { available: false, code: 'daily_limit', message: '今日生图尝试次数已达上限，请明日再试或调整次数上限。' };
  return { available: true, code: 'ready', message: '本地配置已就绪，可发起生图；Token 有效性和账户额度将在实际生成时验证。' };
}

export function availabilityLabel(state: NovelaiAvailability): string {
  return ({ ready: '可生图', service_disabled: '已关闭', credential_missing: '待配置凭据',
    credential_cleanup_pending: '凭据待处理', generation_modes_disabled: '生成模式已关闭', daily_limit: '今日次数已用完' } as Record<string, string>)[state.code] ?? '暂不可用';
}
