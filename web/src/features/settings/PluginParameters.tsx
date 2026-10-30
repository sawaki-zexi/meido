import { useState } from 'react';
import { errorMessage } from '../../api/client';
import { plugins, type PluginState, type HerdrStatus } from '../../api/plugins';
import { emptyDraft } from '../role-management/draft';
import { ScreenPerceptionSettings } from '../role-management/ScreenPerceptionSettings';

export function PluginParameters({ plugin, onSaved }: { plugin: PluginState; onSaved: (value: PluginState) => void }) {
  const [saved, setSaved] = useState(plugin);
  const [values, setValues] = useState(plugin.values);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [herdrStatus, setHerdrStatus] = useState<HerdrStatus | null>(null);
  if (plugin.id !== 'browser' && plugin.id !== 'screen' && plugin.id !== 'herdr') return null;
  return <form aria-label="公共插件参数" onSubmit={event => {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError(''); setNotice('');
    void plugins.configure(saved, values).then(value => { setSaved(value); setValues(value.values); onSaved(value); setNotice('公共参数已保存'); })
      .catch(cause => setError(errorMessage(cause, '无法保存公共参数'))).finally(() => setBusy(false));
  }}>
    <p className="field-hint">对所有角色生效。</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {plugin.id === 'browser' ? <>
      <label className="field checkbox-field"><input type="checkbox" checked={values.headed === true} disabled={busy}
        onChange={event => setValues({ ...values, headed: event.target.checked })} />显示浏览器窗口</label>
      <label className="field">操作超时（秒）<input aria-label="操作超时（秒）" type="number" min={5} max={120} required
        value={Number(values.timeout_seconds)} disabled={busy} onChange={event => setValues({ ...values, timeout_seconds: Number(event.target.value) })} /></label>
    </> : plugin.id === 'herdr' ? <>
      <p className="field-hint">连接你已启动的 Herdr session（兼容版本 ≥ 0.9.4）。角色发送指令后接手代理，状态回报原聊天；关闭插件只停止追踪。</p>
      <label className="field">Herdr 可执行文件<input aria-label="Herdr 可执行文件" required value={String(values.executable ?? 'herdr')}
        disabled={busy} onChange={event => setValues({ ...values, executable: event.target.value })} /></label>
      <label className="field">Herdr Session<input aria-label="Herdr Session" required pattern="[a-zA-Z0-9_-]+" value={String(values.session ?? 'default')}
        disabled={busy} onChange={event => setValues({ ...values, session: event.target.value })} /></label>
      <label className="field">CLI 超时（秒）<input aria-label="CLI 超时（秒）" type="number" required min={5} max={300}
        value={Number(values.cli_timeout_seconds ?? 45)} disabled={busy}
        onChange={event => setValues({ ...values, cli_timeout_seconds: Number(event.target.value) })} /></label>
      <label className="field">代理追踪超时（秒）<input aria-label="代理追踪超时（秒）" type="number" required min={60} max={86400}
        value={Number(values.task_timeout_seconds ?? 7200)} disabled={busy}
        onChange={event => setValues({ ...values, task_timeout_seconds: Number(event.target.value) })} /></label>
      <button type="button" className="ghost" disabled={busy} onClick={() => {
        setBusy(true); setError('');
        void plugins.herdrStatus().then(setHerdrStatus).catch(cause => setError(errorMessage(cause, '无法读取 Herdr session')))
          .finally(() => setBusy(false));
      }}>检查已保存的 session</button>
      {herdrStatus && (herdrStatus.ok ? <div role="status">
        <p>Session：{herdrStatus.session} · {herdrStatus.workspaces?.reduce((count, group) => count + group.agents.length, 0) ?? 0} 个代理</p>
        {herdrStatus.workspaces?.map(group => <div key={group.workspace_id}><strong>{group.label}</strong>
          <ul>{group.agents.map(agent => <li key={agent.pane_id}>{agent.title || agent.pane_id} · {agent.status}
            <br />{agent.cwd} · 接手角色：{agent.handler?.role_name || agent.handler?.role_id || '无'}</li>)}</ul></div>)}
      </div> : <p role="alert">{herdrStatus.error?.message}</p>)}
    </> : <ScreenPerceptionSettings showEnabledToggle={false} disabled={busy}
      draft={{ ...emptyDraft, screenPerceptionEnabled: plugin.enabled, screenVisionModelConfigurationId: String(values.visionModelConfigurationId ?? '') }}
      onChange={draft => setValues({ visionModelConfigurationId: draft.screenVisionModelConfigurationId || null })} />}
    <button type="submit" className="primary" disabled={busy}>{busy ? '保存中…' : '保存参数'}</button>
  </form>;
}
