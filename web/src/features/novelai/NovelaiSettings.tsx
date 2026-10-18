import { useEffect, useState, type FormEvent } from 'react';
import { novelai, type NovelaiConfiguration } from '../../api/novelai';
import { errorMessage } from '../../api/client';
import { Notice, Placeholder } from '../../ui/Status';

export function NovelaiSettings() {
  const [configuration, setConfiguration] = useState<NovelaiConfiguration | null>(null);
  const [token, setToken] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [consent, setConsent] = useState(false);
  useEffect(() => {
    let active = true;
    void novelai.configuration().then((value) => { if (active) { setConfiguration(value); setConsent(value.manualConsentRevision === value.revision); } })
      .catch((cause) => { if (active) setError(errorMessage(cause, '无法读取生图设置')); });
    return () => { active = false; };
  }, []);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!configuration || pending) return;
    setPending(true); setError(''); setMessage('');
    try {
      const { revision, enabled: _enabled, tokenConfigured: _configured, credentialCleanupPending: _cleanup, manualConsentRevision: _consent, ...values } = configuration;
      const saved = await novelai.saveConfiguration({ ...values, confirmManualConsumption: consent }, revision);
      setConfiguration(saved); setConsent(saved.manualConsentRevision === saved.revision);
      setMessage('公共生图参数已保存；场景 CG 在角色参数中单独确认。');
    } catch (cause) { setError(errorMessage(cause, '生图设置保存失败')); }
    finally { setPending(false); }
  };
  const credentials = async (clear: boolean) => {
    if (pending || (!clear && !token)) return;
    setPending(true); setError(''); setMessage('');
    const newToken = token;
    setToken('');
    try {
      setConfiguration(await (clear ? novelai.clearToken() : novelai.setToken(newToken))); setConsent(false);
      setMessage(clear ? '独立凭据已清空。' : '独立凭据已保存；请重新确认有限消费许可。');
    } catch (cause) { setError(errorMessage(cause, '独立凭据操作未完成')); }
    finally { setPending(false); }
  };
  async function toggle() {
    if (!configuration || pending) return;
    setPending(true); setError(''); setMessage('');
    try { const saved = await novelai.saveConfiguration({ enabled: !configuration.enabled }, configuration.revision);
      setConfiguration(saved); setConsent(saved.manualConsentRevision === saved.revision); }
    catch (cause) { setError(errorMessage(cause, '无法改变生图插件状态')); }
    finally { setPending(false); }
  }
  if (!configuration) return error ? <Notice>{error}</Notice> : <Placeholder loading>正在加载生图设置…</Placeholder>;
  const patch = (value: Partial<NovelaiConfiguration>) => { setConfiguration({ ...configuration, ...value }); setConsent(false); };
  return <section className="page-narrow" aria-label="NovelAI 生图设置">
    <h2>NovelAI 生图</h2>
    <p>按主人当前输入或明确操作生成图片。次数是本地尝试上限，不是 Anlas 估价；结果不确定时可能已计费。</p>
    {error && <Notice>{error}</Notice>}{message && <Notice tone="success">{message}</Notice>}
    <div className="settings-subheading"><span>{configuration.enabled ? '已启用' : '已关闭'}</span>
      <button type="button" disabled={pending} onClick={() => void toggle()}>{configuration.enabled ? '关闭 NovelAI' : '启动 NovelAI'}</button></div>
    <form onSubmit={(event) => void save(event)}>
      <p>{configuration.credentialCleanupPending ? '独立凭据清理未完成' : configuration.tokenConfigured ? '独立凭据已配置' : '缺少独立凭据'}</p>
      <label className="field checkbox-field"><input type="checkbox" checked={configuration.manualEnabled ?? false} disabled={pending}
        onChange={event => patch({ manualEnabled: event.target.checked })} />允许按主人输入手动生图（全部角色）</label>
      {configuration.manualEnabled && <>
        <label className="field">每个角色手动每日最多尝试<input aria-label="每个角色手动每日最多尝试" type="number" min={1} max={100} required
          value={configuration.manualDailyAttempts ?? 10} disabled={pending} onChange={event => patch({ manualDailyAttempts: Number(event.target.value) })} /></label>
        <label className="field checkbox-field"><input type="checkbox" checked={consent} disabled={pending || !configuration.enabled || !configuration.tokenConfigured || configuration.credentialCleanupPending}
          onChange={event => setConsent(event.target.checked)} />我确认当前公共参数的有限手动生图消费</label>
        {!consent && <p className="field-hint">确认并保存后才接受手动生成；此确认不启用角色场景 CG。</p>}
      </>}
      <label className="field"><span>账户每日最多尝试</span><input aria-label="账户每日最多尝试" type="number" min="1" max="1000" step="1" required value={configuration.dailyAttemptLimit} disabled={pending} onChange={(event) => patch({ dailyAttemptLimit: Number(event.target.value) })} /></label>
      <label className="field"><span>每张最大像素</span><input aria-label="每张最大像素" type="number" min="4096" max="1048576" step="1" required value={configuration.maxPixels} disabled={pending} onChange={(event) => patch({ maxPixels: Number(event.target.value) })} /></label>
      <label className="field"><span>最多采样步数</span><input aria-label="最多采样步数" type="number" min="1" max="28" step="1" required value={configuration.maxSteps} disabled={pending} onChange={(event) => patch({ maxSteps: Number(event.target.value) })} /></label>
      <label className="field checkbox-field"><input type="checkbox" checked={configuration.allowedModels.includes('nai-diffusion-4-5-full')} disabled={pending} onChange={(event) => patch({ allowedModels: event.target.checked ? ['nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full'] : ['nai-diffusion-4-5-curated'], ...(!event.target.checked ? { defaultModel: 'nai-diffusion-4-5-curated' } : {}) })} />允许 nai-diffusion-4-5-full</label>
      <label className="field"><span>默认模型</span><select aria-label="默认生图模型" value={configuration.defaultModel} disabled={pending} onChange={(event) => patch({ defaultModel: event.target.value })}>{configuration.allowedModels.map((value) => <option key={value}>{value}</option>)}</select></label>
      <label className="field"><span>默认采样器</span><select aria-label="默认采样器" value={configuration.defaultSampler} disabled={pending} onChange={(event) => patch({ defaultSampler: event.target.value })}>{configuration.allowedSamplers.map((value) => <option key={value}>{value}</option>)}</select></label>
      <button type="submit" className="primary" disabled={pending}>保存生图设置</button>
    </form>
    <section aria-label="独立 NovelAI 凭据"><h3>独立凭据</h3>
      <label className="field"><span>新的 NovelAI token</span><input aria-label="新的 NovelAI token" type="password" autoComplete="new-password" value={token} disabled={pending} onChange={(event) => setToken(event.target.value)} /></label>
      <button type="button" disabled={pending || !token} onClick={() => void credentials(false)}>设置新凭据</button>
      <button type="button" disabled={pending || (!configuration.tokenConfigured && !configuration.credentialCleanupPending)} onClick={() => void credentials(true)}>清空独立凭据</button>
    </section>
  </section>;
}
