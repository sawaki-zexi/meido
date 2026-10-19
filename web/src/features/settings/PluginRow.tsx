import { Icon } from '../../ui/Icon';

export function PluginRow({ name, description, status, enabled, busy, onConfigure, onToggle, startLabel = '启动' }: {
  name: string; description: string; status: string; enabled?: boolean; busy: boolean;
  onConfigure: () => void; onToggle: () => void; startLabel?: string;
}) {
  const extraStatus = !['已启用', '已关闭'].includes(status);
  return <article className="plugin-row" aria-label={name}>
    <div className="plugin-row-copy">
      <div className="plugin-row-title"><h4>{name}</h4>{extraStatus && <span className="plugin-status">{status}</span>}</div>
      <p>{description}</p>
    </div>
    <div className="plugin-row-actions">
      <button type="button" className="plugin-configure" disabled={busy} aria-label={`${name} 设置`} onClick={onConfigure}><Icon name="settings" size={16} />设置</button>
      {startLabel === '重试关闭' && <button type="button" disabled={busy} onClick={onToggle}>重试关闭</button>}
      <button type="button" className="settings-switch" role="switch" aria-label={`启用 ${name}`} aria-checked={enabled === true}
        disabled={busy || enabled === undefined || startLabel === '重试关闭'} onClick={onToggle}><span /></button>
    </div>
  </article>;
}
