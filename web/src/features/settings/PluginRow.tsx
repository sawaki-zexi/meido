export function PluginRow({ name, description, status, enabled, busy, onConfigure, onToggle, stopLabel = '关闭', startLabel = '启动' }: {
  name: string; description: string; status: string; enabled?: boolean; busy: boolean;
  onConfigure: () => void; onToggle: () => void; stopLabel?: string; startLabel?: string;
}) {
  return <article className="plugin-row" aria-label={name}>
    <div className="plugin-row-copy">
      <div className="plugin-row-title"><h4>{name}</h4><span className={`plugin-status${enabled ? ' is-enabled' : ''}`}>{status}</span></div>
      <p>{description}</p>
    </div>
    <div className="plugin-row-actions">
      <button type="button" className="ghost" disabled={busy} aria-label={`${name} 设置`} onClick={onConfigure}>设置</button>
      <button type="button" disabled={busy || enabled === undefined} aria-label={`${enabled ? '关闭' : startLabel} ${name}`} onClick={onToggle}>{enabled ? stopLabel : startLabel}</button>
    </div>
  </article>;
}
