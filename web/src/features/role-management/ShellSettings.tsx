import type { RoleDraft } from "./draft";

export function ShellSettings({ draft, onChange, disabled }: { draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled: boolean }) {
  return <section aria-label="Shell 配置">
    <label className="field checkbox-field">
      <input type="checkbox" aria-label="允许执行 Shell 命令" checked={draft.shellEnabled} disabled={disabled} onChange={(event) => onChange({ ...draft, shellEnabled: event.target.checked })} />
      <span>允许执行 Shell 命令</span>
      <small className="field-hint">只执行命令白名单中的程序，工作目录固定为此角色的工作区。</small>
    </label>
    <label className="field"><span>命令白名单（每行一个）</span>
      <textarea aria-label="命令白名单（每行一个）" value={draft.shellAllowedCommands} disabled={disabled} placeholder="python&#10;git" onChange={(event) => onChange({ ...draft, shellAllowedCommands: event.target.value })} />
      <small className="field-hint">填写程序名或绝对路径；空白名单会关闭工具。此功能不提供操作系统沙箱。</small>
    </label>
    <label className="field"><span>Shell 超时（秒）</span><input type="number" min="0" max="300" step="any" required value={draft.shellTimeoutSeconds} disabled={disabled} onChange={(event) => onChange({ ...draft, shellTimeoutSeconds: event.target.valueAsNumber })} /></label>
    <label className="field"><span>Shell 输出字符上限</span><input type="number" min="1" max="200000" step="1" required value={draft.shellMaxOutputChars} disabled={disabled} onChange={(event) => onChange({ ...draft, shellMaxOutputChars: event.target.valueAsNumber })} /></label>
  </section>;
}
