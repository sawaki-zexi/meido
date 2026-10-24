import { useEffect, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { Role } from '../../api/types';
import { draftFromRole, payloadFromDraft, type RoleDraft } from '../role-management/draft';
import { NovelaiRoleSettings } from '../novelai/NovelaiRoleSettings';
import { CodingRoleSettings } from '../coding/CodingRoleSettings';
import { AutonomySettings } from '../role-management/AutonomySettings';
import { DesktopPetSettings } from '../role-management/DesktopPetSettings';
import { ShellSettings } from '../role-management/ShellSettings';
import { SpawnSettings } from '../role-management/SpawnSettings';
import { ToolCatalog } from '../role-management/ToolCatalog';

export type RolePurpose = 'novelai' | 'coding' | 'autonomy' | 'pet' | 'advanced';
export function RolePurposeSettings({ purpose, onRoleSaved, serviceVersion = 0 }: {
  purpose: RolePurpose; onRoleSaved?: (role: Role) => void; serviceVersion?: number;
}) {
  const [roles, setRoles] = useState<Role[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [draft, setDraft] = useState<RoleDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let active = true;
    void api<{ roles: Role[] }>('/api/roles').then(({ roles: items }) => {
      if (!active) return;
      setRoles(items); setSelectedId(items[0]?.id ?? ''); setDraft(items[0] ? draftFromRole(items[0]) : null);
    }).catch(cause => { if (active) setError(errorMessage(cause, '无法读取角色参数')); });
    return () => { active = false; };
  }, []);
  const selected = roles.find(role => role.id === selectedId);
  async function save() {
    if (busy || !selected || !draft) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const { role: fresh } = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selected.id)}`);
      const before = payloadFromDraft(draftFromRole(selected)).agentConfig;
      const next = payloadFromDraft(draft).agentConfig;
      const current = payloadFromDraft(draftFromRole(fresh)).agentConfig;
      const fields = purpose === 'advanced' ? ['shell', 'spawn'] as const : [purpose] as ('novelai' | 'coding')[];
      const patch: Record<string, unknown> = {};
      for (const field of fields) {
        const updates: Record<string, unknown> = {};
        const previous = (before[field] ?? {}) as Record<string, unknown>;
        const wanted = (next[field] ?? {}) as Record<string, unknown>;
        const latest = (current[field] ?? {}) as Record<string, unknown>;
        for (const key of Object.keys(wanted)) {
          if (JSON.stringify(previous[key]) === JSON.stringify(wanted[key])) continue;
          if (JSON.stringify(previous[key]) !== JSON.stringify(latest[key]) && JSON.stringify(wanted[key]) !== JSON.stringify(latest[key]))
            throw new Error('角色参数已被其他页面修改，请重新打开设置后保存');
          updates[key] = wanted[key];
        }
        if (Object.keys(updates).length) patch[field] = field === 'shell' || field === 'spawn' ? { ...latest, ...updates } : updates;
      }
      const { role: saved } = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selected.id)}`, {
        method: 'PUT', body: JSON.stringify({ name: fresh.name, description: fresh.description, profile: fresh.profile, agentConfig: patch }),
      });
      setRoles(items => items.map(item => item.id === saved.id ? saved : item));
      setDraft(draftFromRole(saved)); onRoleSaved?.(saved); setNotice('角色参数已保存');
    } catch (cause) { setError(errorMessage(cause, '无法保存角色参数')); }
    finally { setBusy(false); }
  }
  return <section className="role-purpose-settings" aria-label="角色用途参数">
    <p className="field-hint">仅配置此功能需要的角色差异；插件启停由全局列表控制。</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {roles.length === 0 && <p>请先创建角色。</p>}
    {roles.length > 0 && <label className="field">角色<select aria-label="角色参数" value={selectedId} disabled={busy} onChange={event => {
      setSelectedId(event.target.value); setDraft(draftFromRole(roles.find(role => role.id === event.target.value)!)); setError(''); setNotice('');
    }}>{roles.map(role => <option key={role.id} value={role.id}>{role.name}</option>)}</select></label>}
    {selected && draft && <div key={selected.id}>
      {purpose === 'autonomy' ? <AutonomySettings roleId={selected.id} disabled={busy} />
        : purpose === 'pet' ? <DesktopPetSettings roleId={selected.id} disabled={busy} />
        : <form onSubmit={event => { event.preventDefault(); void save(); }}>
          {purpose === 'novelai' ? <NovelaiRoleSettings key={serviceVersion} draft={draft} onChange={setDraft} disabled={busy} sceneOnly />
            : purpose === 'coding' ? <CodingRoleSettings key={serviceVersion} roleId={selected.id} draft={draft} onChange={setDraft} disabled={busy} showEnabledToggle={false} />
            : <><ShellSettings draft={draft} onChange={setDraft} disabled={busy} /><SpawnSettings draft={draft} onChange={setDraft} disabled={busy} /></>}
          <button type="submit" className="primary" disabled={busy}>{busy ? '保存中…' : '保存角色参数'}</button>
        </form>}
      {purpose === 'advanced' && <ToolCatalog roleId={selected.id} />}
    </div>}
  </section>;
}
