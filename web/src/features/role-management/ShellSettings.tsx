import type { RoleDraft } from "./draft";

export function ShellSettings({ draft, onChange, disabled }: { draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled: boolean }) {
  return <section aria-label="Shell 配置">
    <p className="field-hint">Shell 默认提供，Windows 使用 PowerShell 7，其他平台使用 /bin/sh。工作目录固定为当前角色工作区；它不提供操作系统沙箱。</p>
    <details><summary>受限任务的命令许可</summary>
      <p className="field-hint">仅控制脚本子任务和软时间任务；不会关闭普通对话的默认 Shell。</p>
      <label className="field checkbox-field"><input type="checkbox" aria-label="允许受限任务执行命令"
        checked={draft.shellEnabled} disabled={disabled}
        onChange={(event) => onChange({ ...draft, shellEnabled: event.target.checked })} />允许受限任务执行命令</label>
      <label className="field"><span>受限任务命令白名单</span><textarea aria-label="受限任务命令白名单"
        value={draft.shellAllowedCommands} disabled={disabled}
        onChange={(event) => onChange({ ...draft, shellAllowedCommands: event.target.value })} /></label>
    </details>
    <label className="field"><span>Shell 超时（秒）</span><input type="number" min="0" max="300" step="any" required value={Number.isFinite(draft.shellTimeoutSeconds) ? draft.shellTimeoutSeconds : ''} disabled={disabled} onChange={(event) => onChange({ ...draft, shellTimeoutSeconds: event.target.valueAsNumber })} /></label>
    <label className="field"><span>Shell 输出字符上限</span><input type="number" min="1" max="200000" step="1" required value={Number.isFinite(draft.shellMaxOutputChars) ? draft.shellMaxOutputChars : ''} disabled={disabled} onChange={(event) => onChange({ ...draft, shellMaxOutputChars: event.target.valueAsNumber })} /></label>
  </section>;
}
