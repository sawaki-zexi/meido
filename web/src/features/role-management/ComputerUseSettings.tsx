import type { RoleDraft } from "./draft";

export function ComputerUseSettings({ draft, onChange, disabled = false }: {
  draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled?: boolean;
}) {
  return <label className="field checkbox-field">
    <input type="checkbox" aria-label="允许操作本机窗口" checked={draft.computerUseEnabled}
      disabled={disabled} onChange={(event) => onChange({ ...draft, computerUseEnabled: event.target.checked })} />
    <span>允许操作本机窗口</span>
    <small className="field-hint">可操作本机窗口，窗口截图发送至当前聊天模型。仅当前主人桌面回合可用；停止或关闭后释放控制。</small>
  </label>;
}
