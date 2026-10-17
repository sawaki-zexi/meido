import { useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { Role } from '../../api/types';
import { draftFromRole, payloadFromDraft, type RoleDraft } from '../role-management/draft';
import { RolePluginSettings } from '../role-management/RolePluginSettings';
import { ToolCatalog } from '../role-management/ToolCatalog';
import { BasicToolSettings } from '../role-management/BasicToolSettings';
import { ShellSettings } from '../role-management/ShellSettings';
import { SpawnSettings } from '../role-management/SpawnSettings';

export function PluginSettings({ onRoleSaved }: { onRoleSaved?: (role: Role) => void }) {
  const [roles, setRoles] = useState<Role[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [draft, setDraft] = useState<RoleDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const generation = useRef(0);
  useEffect(() => {
    const request = ++generation.current;
    void api<{ roles: Role[] }>('/api/roles').then(({ roles: result }) => {
      if (request !== generation.current) return;
      setRoles(result); setSelectedId(result[0]?.id ?? '');
      setDraft(result[0] ? draftFromRole(result[0]) : null);
    }).catch((cause) => { if (request === generation.current) setError(errorMessage(cause, '无法读取角色插件设置')); });
    return () => { generation.current += 1; };
  }, []);
  const selected = roles.find((role) => role.id === selectedId);
  const save = async () => {
    if (!selected || !draft || busy) return;
    const request = ++generation.current;
    setBusy(true); setError(''); setNotice('');
    try {
      // Only update edited tool settings; preserve the latest role profile and other configuration.
      const fresh = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selected.id)}`);
      if (request !== generation.current) return;
      const config = payloadFromDraft(draft).agentConfig;
      const current = fresh.role.agentConfig;
      const before = payloadFromDraft(draftFromRole(selected)).agentConfig;
      const changes: Record<string, unknown> = {};
      for (const key of ['browserUse', 'computerUse', 'screenPerception', 'novelai', 'coding', 'shell', 'spawn'] as const) {
        if (JSON.stringify(config[key]) !== JSON.stringify(before[key])) changes[key] = config[key];
      }
      if (draft.todoSearchEnabled !== draftFromRole(selected).todoSearchEnabled) {
        const updated = payloadFromDraft({ ...draftFromRole(fresh.role), todoSearchEnabled: draft.todoSearchEnabled }).agentConfig;
        changes.enabledTools = updated.enabledTools;
        changes.enabledPlugins = updated.enabledPlugins;
        changes.grantedCapabilities = updated.grantedCapabilities;
      }
      const { role: saved } = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selected.id)}`, {
        method: 'PUT', body: JSON.stringify({ name: fresh.role.name, description: fresh.role.description,
          profile: fresh.role.profile, agentConfig: { ...current, ...changes } }),
      });
      if (request !== generation.current) return;
      setRoles((items) => items.map((item) => item.id === saved.id ? saved : item));
      onRoleSaved?.(saved);
      setDraft(draftFromRole(saved)); setNotice('插件设置已保存');
    } catch (cause) {
      if (request === generation.current) setError(errorMessage(cause, '无法保存插件设置'));
    } finally { if (request === generation.current) setBusy(false); }
  };
  return <section aria-label="插件设置">
    <h3>工具与插件</h3>
    <p>基础工具默认随角色提供。特殊插件的运行依赖和服务凭据使用各自设置。</p>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {roles.length === 0 && <p>请先创建角色。</p>}
    {roles.length > 0 && <label className="field"><span>配置角色</span>
      <select aria-label="配置角色" disabled={busy} value={selectedId} onChange={(event) => {
        generation.current += 1; setSelectedId(event.target.value); setError(''); setNotice('');
        const role = roles.find((item) => item.id === event.target.value);
        setDraft(role ? draftFromRole(role) : null);
      }}>{roles.map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}</select>
    </label>}
    {selected && draft && <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <BasicToolSettings draft={draft} onChange={setDraft} disabled={busy} />
      <ShellSettings draft={draft} onChange={setDraft} disabled={busy} />
      <SpawnSettings draft={draft} onChange={setDraft} disabled={busy} />
      <RolePluginSettings key={selected.id} roleId={selected.id} draft={draft} onChange={setDraft} disabled={busy} />
      <button type="submit" className="primary" disabled={busy}>{busy ? '保存中…' : '保存'}</button>
      <ToolCatalog key={`${selected.id}:${selected.updatedAt}`} roleId={selected.id}
        hasUnsavedChanges={JSON.stringify(payloadFromDraft(draft).agentConfig) !== JSON.stringify(payloadFromDraft(draftFromRole(selected)).agentConfig)} />
    </form>}
  </section>;
}
