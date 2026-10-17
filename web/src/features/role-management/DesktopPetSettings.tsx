import { useEffect, useState } from 'react';
import { desktopPet, type PetState } from '../../api/desktopPet';
import { errorMessage } from '../../api/client';

const labels: Record<string, string> = { ready: '窗口已就绪', stopped: '已停止', starting: '正在启动',
  disconnected: '窗口已断开', cleanup_pending: '正在等待原宿主退出' };
const receipts: Record<string, string> = { accepted: '已接收', started: '开始播放', finished: '已完成',
  error: '失败', closed: '窗口已关闭', ready: '窗口已就绪' };
export function DesktopPetSettings({ roleId, disabled = false }: { roleId: string; disabled?: boolean }) {
  const [state, setState] = useState<PetState | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { let active = true;
    const refresh = () => { void desktopPet.status(roleId).then((value) => { if (active) setState(value); })
      .catch((cause) => { if (active) setError(errorMessage(cause, '桌宠状态读取失败')); }); };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [roleId]);
  async function perform(action: () => Promise<PetState>) {
    if (busy) return;
    setBusy(true); setError('');
    try { setState(await action()); } catch (cause) {
      setError(errorMessage(cause, '桌宠操作失败'));
      try { setState(await desktopPet.status(roleId)); } catch { /* Preserve the original failure. */ }
    } finally { setBusy(false); }
  }
  const locked = disabled || busy || !state;
  const selected = state?.packages.find((item) => item.id === state.selectedPackageId);
  return <fieldset aria-label="桌宠设置"><legend>桌宠</legend>
    <p className="field-hint">导入 Shiori 动作包后显式开启。设置立即生效；同一时间只显示一个角色，仅当前主人桌面对话可以控制。</p>
    <label className="field">导入桌宠 ZIP
      <input type="file" accept=".zip,application/zip" aria-label="导入桌宠ZIP" disabled={locked}
        onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = '';
          if (file) { if (file.size > 32 * 1024 * 1024) setError('桌宠ZIP最大32MiB');
            else void perform(() => desktopPet.import(roleId, file)); } }} />
    </label>
    <label className="field">动作包<select aria-label="桌宠动作包" disabled={locked} value={state?.selectedPackageId ?? ''}
      onChange={(event) => { const id = event.currentTarget.value || null;
        if (state) void perform(() => desktopPet.configure(roleId, state, id, Boolean(id && state.enabled))); }}>
      <option value="">未选择</option>{state?.packages.map((item) => <option key={item.id} value={item.id}>{item.displayName}</option>)}
    </select></label>
    <label className="field checkbox-field"><input aria-label="开启角色桌宠" type="checkbox" checked={state?.enabled ?? false}
      disabled={locked || !selected} onChange={(event) => { const enabled = event.currentTarget.checked;
        if (state) void perform(() => desktopPet.configure(roleId, state, state.selectedPackageId, enabled)); }} /><span>开启角色桌宠</span></label>
    {selected && <div><img src={desktopPet.preview(roleId, selected.id)} alt={`${selected.displayName}预览`} width="96" height="104" style={{ objectFit: 'contain' }} />
      <p>{selected.description}</p><p>可用动作：{Object.keys(selected.actions).join('、') || '无自定义动作'}；支持移动到五个桌面位置。</p></div>}
    <p role="status">{state ? labels[state.host.status] ?? state.host.status : '正在读取…'}{state?.unavailableReason ? ` · ${state.unavailableReason}` : ''}</p>
    <button type="button" className="ghost" disabled={locked || !state?.enabled} onClick={() => void perform(() => desktopPet.restart(roleId))}>重启桌宠</button>
    <button type="button" className="ghost" disabled={locked} onClick={() => void perform(() => desktopPet.status(roleId))}>刷新状态</button>
    {state?.packages.map((item) => <div key={item.id}><span>{item.displayName}</span>
      <button type="button" className="ghost" disabled={locked || state.enabled && state.selectedPackageId === item.id}
        onClick={() => void perform(() => desktopPet.remove(roleId, state, item.id))}>删除动作包 {item.displayName}</button></div>)}
    {state?.history.length ? <details><summary>最近窗口与动作回执</summary><ul>{state.history.slice(-12).map((event, index) =>
      <li key={`${event.actionId ?? 'window'}:${event.type}:${index}`}>{receipts[event.type] ?? event.type}{event.actionId ? ` · ${event.actionId.slice(0, 8)}` : ''}{event.reason ? ` · ${event.reason}` : ''}</li>)}</ul></details> : null}
    {error && <p role="alert">{error}</p>}
  </fieldset>;
}
