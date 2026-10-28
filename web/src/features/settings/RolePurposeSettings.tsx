import { useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { Role } from '../../api/types';
import { draftFromRole, payloadFromDraft, type RoleDraft } from '../role-management/draft';
import { NovelaiRoleSettings } from '../novelai/NovelaiRoleSettings';
import { CodingRoleSettings } from '../coding/CodingRoleSettings';
import { AutonomySettings } from '../role-management/AutonomySettings';
import { AutonomyManagement } from '../role-management/AutonomyManagement';
import { DesktopPetSettings } from '../role-management/DesktopPetSettings';
import { ShellSettings } from '../role-management/ShellSettings';
import { SpawnSettings } from '../role-management/SpawnSettings';
import { ToolCatalog } from '../role-management/ToolCatalog';

export type RolePurpose = 'novelai' | 'coding' | 'autonomy' | 'autonomy-management' | 'pet' | 'advanced';
export function RolePurposeSettings({ purpose, onRoleSaved, serviceVersion = 0, roleId }: {
  purpose: RolePurpose; onRoleSaved?: (role: Role) => void; serviceVersion?: number; roleId?: string;
}) {
  const [roles, setRoles] = useState<Role[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [draft, setDraft] = useState<RoleDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);
  useEffect(() => {
    const sequence = ++generation.current;
    setLoading(true); setRoles([]); setDraft(null); setSelectedId(''); setError(''); setNotice(''); setBusy(false);
    const request = roleId ? api<{ role: Role }>(`/api/roles/${encodeURIComponent(roleId)}`).then(({ role }) => ({ roles: [role] })) : api<{ roles: Role[] }>('/api/roles');
    void request.then(({ roles: items }) => {
      if (sequence !== generation.current) return;
      setRoles(items); setSelectedId(items[0]?.id ?? ''); setDraft(items[0] ? draftFromRole(items[0]) : null);
    }).catch(cause => { if (sequence === generation.current) setError(errorMessage(cause, '无法读取角色参数')); })
      .finally(() => { if (sequence === generation.current) setLoading(false); });
    return () => { ++generation.current; };
  }, [roleId]);
  const selected = roles.find(role => role.id === selectedId);
  async function save() {
    if (busy || !selected || !draft) return;
    const sequence = ++generation.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const { role: fresh } = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selected.id)}`);
      if (sequence !== generation.current) return;
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
      if (sequence !== generation.current) return;
      setRoles(items => items.map(item => item.id === saved.id ? saved : item));
      setDraft(draftFromRole(saved)); onRoleSaved?.(saved); setNotice('角色参数已保存');
    } catch (cause) { if (sequence === generation.current) setError(errorMessage(cause, '无法保存角色参数')); }
    finally { if (sequence === generation.current) setBusy(false); }
  }
  return <section className="role-purpose-settings" aria-label="角色用途参数">
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {loading && <p role="status">正在读取角色设置…</p>}
    {!loading && !error && roles.length === 0 && <p>请先创建角色。</p>}
    {!roleId && roles.length > 0 && <label className="field settings-role-picker">角色<select aria-label="角色参数" value={selectedId} disabled={busy} onChange={event => {
      setSelectedId(event.target.value); setDraft(draftFromRole(roles.find(role => role.id === event.target.value)!)); setError(''); setNotice('');
    }}>{roles.map(role => <option key={role.id} value={role.id}>{role.name}</option>)}</select></label>}
    {selected && draft && <div key={selected.id}>
      {purpose === 'autonomy' ? <AutonomySettings roleId={selected.id} disabled={busy} />
        : purpose === 'autonomy-management' ? <AutonomyManagement roleId={selected.id} disabled={busy} />
        : purpose === 'pet' ? <DesktopPetSettings roleId={selected.id} disabled={busy} />
        : <form className="settings-section" onSubmit={event => { event.preventDefault(); void save(); }}>
          {purpose === 'novelai' ? <NovelaiRoleSettings key={serviceVersion} draft={draft} onChange={setDraft} disabled={busy} />
            : purpose === 'coding' ? <CodingRoleSettings key={serviceVersion} roleId={selected.id} draft={draft} onChange={setDraft} disabled={busy} showEnabledToggle={false} />
            : <><ShellSettings draft={draft} onChange={setDraft} disabled={busy} /><SpawnSettings draft={draft} onChange={setDraft} disabled={busy} /></>}
          <div className="form-actions settings-save-actions"><button type="submit" className="primary" disabled={busy}>{busy ? '保存中…' : '保存角色参数'}</button></div>
        </form>}
      {purpose === 'advanced' && <ToolCatalog roleId={selected.id} />}
    </div>}
  </section>;
}
