/** Shared switch for every settings domain; the label is its accessible name. */
export function SettingsSwitch({ label, checked, disabled = false, onChange }: {
  label: string; checked: boolean; disabled?: boolean; onChange: (checked: boolean) => void;
}) {
  return <button type="button" className="settings-switch" role="switch" aria-label={label}
    aria-checked={checked} disabled={disabled} onClick={() => onChange(!checked)}><span /></button>;
}
