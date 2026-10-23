export function SettingsTabs<T extends string>({ label, items, current, onSelect, disabled = false }: {
  label: string; items: readonly { id: T; label: string }[]; current: T; onSelect: (id: T) => void; disabled?: boolean;
}) {
  return <nav className="settings-subnav" aria-label={label}>{items.map(item =>
    <button key={item.id} type="button" disabled={disabled} aria-current={current === item.id ? 'page' : undefined}
      onClick={() => onSelect(item.id)}>{item.label}</button>)}</nav>;
}
