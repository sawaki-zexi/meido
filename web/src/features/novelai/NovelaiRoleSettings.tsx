import { useEffect, useState } from 'react';
import { defaultNovelaiRoleConfig, novelai, type NovelaiConfiguration, type NovelaiRoleConfig } from '../../api/novelai';
import type { AgentToolConfig } from '../../api/types';
import type { RoleDraft } from '../role-management/draft';
import { errorMessage } from '../../api/client';

export function NovelaiRoleSettings({ draft, onChange, disabled = false }: {
  draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled?: boolean;
}) {
  const [configuration, setConfiguration] = useState<NovelaiConfiguration | null>(null);
  const [error, setError] = useState('');
  const config = { ...defaultNovelaiRoleConfig, ...(draft.preservedAgentConfig as AgentToolConfig & { novelai?: Partial<NovelaiRoleConfig> }).novelai };
  useEffect(() => {
    let active = true;
    void novelai.configuration().then((value) => { if (active) setConfiguration(value); })
      .catch((cause) => { if (active) setError(errorMessage(cause, '无法加载 NovelAI 设置')); });
    return () => { active = false; };
  }, []);
  const patch = (changes: Partial<NovelaiRoleConfig>) => onChange({ ...draft,
    preservedAgentConfig: { ...draft.preservedAgentConfig, novelai: { ...config, ...changes } } as AgentToolConfig });
  const available = configuration?.enabled && configuration.tokenConfigured && !configuration.credentialCleanupPending;
  const consent = Boolean(configuration && config.consentRevision === configuration.revision);
  return <fieldset aria-label="角色生图许可" disabled={disabled}>
    <legend>生图</legend>
    <p>按主人当前输入或明确操作生成；图片只属于本角色和原来源。开启并确认有限许可后才接受新的生成请求。</p>
    {error && <p role="alert">{error}</p>}
    {!configuration ? <p>正在读取独立服务设置…</p> : !available ? <p>请先在设置中开启 NovelAI 服务并配置独立凭据。</p> : <p>账户每日最多 {configuration.dailyAttemptLimit} 次，每张最多 {configuration.maxPixels.toLocaleString()} 像素、{configuration.maxSteps} 步。</p>}
    <label className="field checkbox-field"><input type="checkbox" checked={config.manualEnabled}
      disabled={disabled || (!available && !config.manualEnabled)}
      onChange={(event) => patch({ manualEnabled: event.target.checked, consentRevision: 0 })} />允许按主人输入生图</label>
    {config.manualEnabled && <>
      <label className="field"><span>角色手动每日最多尝试</span><input aria-label="角色手动每日最多尝试" type="number" min="1" max="100" step="1" required value={config.manualDailyAttempts}
        onChange={(event) => patch({ manualDailyAttempts: Number(event.target.value), consentRevision: 0 })} /></label>
      <label className="field checkbox-field"><input type="checkbox" required checked={consent} disabled={disabled || !available}
        onChange={(event) => patch({ consentRevision: event.target.checked ? configuration!.revision : 0 })} />我确认当前设置的有限生图消费；次数不是 Anlas 估价，取消或未知结果可能已计费</label>
      {configuration && !consent && <p>请确认当前版本的有限许可后保存角色。</p>}
    </>}
  </fieldset>;
}
