import type { RoleDraft } from "./draft";

export function SpawnSettings({ draft, onChange, disabled }: { draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled: boolean }) {
  const fields = [
    { key: "spawnTimeoutSeconds", label: "子任务超时（秒）", min: 1, max: 600 },
    { key: "spawnMaxTurns", label: "子任务模型请求上限", min: 1, max: 30 },
    { key: "spawnMaxOutputChars", label: "子任务结果字符上限", min: 100, max: 100000 },
    { key: "spawnMaxModelOutputChars", label: "子任务模型累计输出字符上限", min: 100, max: 200000 },
  ] as const;
  return <section aria-label="子任务委托配置">
    <label className="field checkbox-field">
      <input type="checkbox" aria-label="允许委托独立子任务" checked={draft.spawnEnabled} disabled={disabled} onChange={(event) => onChange({ ...draft, spawnEnabled: event.target.checked })} />
      <span>允许委托独立子任务</span>
      <small className="field-hint">角色可以同步等待结果，或让任务在后台继续；完成结果仅回原会话。子任务只使用角色本次已获授权的工具子集。</small>
    </label>
    {fields.map(({ key, label, min, max }) => <label className="field" key={key}><span>{label}</span>
      <input type="number" aria-label={label} min={min} max={max} step="1" required value={Number.isFinite(draft[key]) ? draft[key] : ""} disabled={disabled} onChange={(event) => onChange({ ...draft, [key]: event.target.valueAsNumber })} />
    </label>)}
    <p className="field-hint">脚本仅通过已有命令白名单执行，文件写入独立任务目录；此功能不提供操作系统或网络沙箱。</p>
  </section>;
}
