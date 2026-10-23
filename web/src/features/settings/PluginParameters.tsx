import { useState } from 'react';
import { errorMessage } from '../../api/client';
import { plugins, type PluginState } from '../../api/plugins';
import { emptyDraft } from '../role-management/draft';
import { ScreenPerceptionSettings } from '../role-management/ScreenPerceptionSettings';

export function PluginParameters({ plugin, onSaved }: { plugin: PluginState; onSaved: (value: PluginState) => void }) {
  const [saved, setSaved] = useState(plugin);
  const [values, setValues] = useState(plugin.values);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  if (plugin.id !== 'browser' && plugin.id !== 'screen') return <p className="field-hint">此插件没有公共参数。</p>;
  return <form aria-label="公共插件参数" onSubmit={event => {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError(''); setNotice('');
    void plugins.configure(saved, values).then(value => { setSaved(value); setValues(value.values); onSaved(value); setNotice('公共参数已保存'); })
      .catch(cause => setError(errorMessage(cause, '无法保存公共参数'))).finally(() => setBusy(false));
  }}>
    <p className="field-hint">公共参数对所有角色生效，保存不会启动插件。</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {plugin.id === 'browser' ? <>
      <label className="field checkbox-field"><input type="checkbox" checked={values.headed === true} disabled={busy}
        onChange={event => setValues({ ...values, headed: event.target.checked })} />显示浏览器窗口</label>
      <label className="field">操作超时（秒）<input aria-label="操作超时（秒）" type="number" min={5} max={120} required
        value={Number(values.timeout_seconds)} disabled={busy} onChange={event => setValues({ ...values, timeout_seconds: Number(event.target.value) })} /></label>
    </> : <ScreenPerceptionSettings showEnabledToggle={false} disabled={busy}
      draft={{ ...emptyDraft, screenPerceptionEnabled: plugin.enabled, screenVisionModelConfigurationId: String(values.visionModelConfigurationId ?? '') }}
      onChange={draft => setValues({ visionModelConfigurationId: draft.screenVisionModelConfigurationId || null })} />}
    <button type="submit" className="primary" disabled={busy}>{busy ? '保存中…' : '保存参数'}</button>
  </form>;
}
