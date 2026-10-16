import { useEffect, useState } from "react";
import { defaultCodingRole, getCodingConfiguration, type CodingConfiguration } from "../../api/coding";
import { errorMessage } from "../../api/client";
import type { RoleDraft } from "../role-management/draft";

export function CodingRoleSettings({ roleId, draft, onChange, disabled }: { roleId?: string; draft: RoleDraft; onChange: (value: RoleDraft) => void; disabled: boolean }) {
  const [configuration, setConfiguration] = useState<CodingConfiguration | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { let live = true; void getCodingConfiguration().then(value => { if (live) setConfiguration(value); }).catch(cause => { if (live) setError(errorMessage(cause, "无法读取编程配置")); }); return () => { live = false; }; }, []);
  const value = { ...defaultCodingRole(), ...draft.preservedAgentConfig.coding };
  const change = (patch: Partial<typeof value>) => onChange({ ...draft, preservedAgentConfig: { ...draft.preservedAgentConfig, coding: { ...value, ...patch } } });
  const toggle = (field: "repoGrantIds" | "profileIds", id: string, checked: boolean) => change({ [field]: checked ? [...new Set([...value[field], id])] : value[field].filter(item => item !== id) });
  return <fieldset><legend>编程委托</legend>
    <label className="checkbox-field"><input type="checkbox" aria-label="允许编程委托" checked={value.enabled} disabled={disabled} onChange={event => onChange({ ...draft, otherEnabledTools: event.target.checked ? [...new Set([...draft.otherEnabledTools, 'tool_search'])] : draft.otherEnabledTools, preservedAgentConfig: { ...draft.preservedAgentConfig, coding: { ...value, enabled: event.target.checked } } })} />允许编程委托</label>
    <p className="field-hint">在专用 worktree 使用已核验 CLI。仓库中的验证程序会以当前账号权限运行，请仅授权信任的仓库。</p>
    {error && <p role="alert">{error}</p>}
    {!configuration?.enabled && <p>先在设置 → 编程委托启用全局开关并登记仓库和 CLI。</p>}
    {!roleId && <p>保存角色后可选择全局配置中授予她的仓库和 CLI。</p>}
    {configuration?.repositories.filter(item => item.allowedRoleIds.includes(roleId ?? "")).map(item => <label className="field checkbox-field" key={item.id}><input type="checkbox" checked={value.repoGrantIds.includes(item.id)} disabled={disabled} onChange={event => toggle("repoGrantIds", item.id, event.target.checked)} />{item.root}</label>)}
    {configuration?.profiles.filter(item => item.allowedRoleIds.includes(roleId ?? "")).map(item => <label className="field checkbox-field" key={item.id}><input type="checkbox" checked={value.profileIds.includes(item.id)} disabled={disabled} onChange={event => toggle("profileIds", item.id, event.target.checked)} />{item.name} · {item.verification.status === "verified" ? "已核验" : item.verification.reason ?? "尚未核验"}</label>)}
    <label className="field">默认 Herdr CLI<select aria-label="默认 Herdr CLI" disabled={disabled} value={value.defaultHerdrProfileId ?? ""} onChange={event => change({ defaultHerdrProfileId: event.target.value || null })}><option value="">请选择</option>{configuration?.profiles.filter(item => item.executor === "herdr" && value.profileIds.includes(item.id) && item.allowedRoleIds.includes(roleId ?? "")).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
    <label className="field">每天最多尝试次数<input aria-label="每天最多编程尝试次数" type="number" min={1} max={100} disabled={disabled} value={value.maxAttemptsPerDay} onChange={event => change({ maxAttemptsPerDay: Number(event.target.value) })} /></label>
  </fieldset>;
}
