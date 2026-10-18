import { useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { Role } from '../../api/types';
import { draftFromRole, payloadFromDraft, type RoleDraft } from '../role-management/draft';
import { RolePluginSettings } from '../role-management/RolePluginSettings';
import { SettingsTabs } from './SettingsTabs';
import { pluginServices, type SettingsServiceId } from './navigation';

export function PluginSettings({ onRoleSaved, onOpenService, serviceVersion = 0, initialTab = 'roles' }: {
  onRoleSaved?: (role: Role) => void; onOpenService?: (id: SettingsServiceId) => void;
  serviceVersion?: number; initialTab?: 'roles' | 'services';
}) {
  const [tab, setTab] = useState<'roles' | 'services'>(initialTab);
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
  const save = async (candidate: RoleDraft): Promise<boolean> => {
    if (!selected || busy) return false;
    const request = ++generation.current;
    setBusy(true); setError(''); setNotice('');
    try {
      // Only update edited tool settings; preserve the latest role profile and other configuration.
      const fresh = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selected.id)}`);
      if (request !== generation.current) return false;
      const config = payloadFromDraft(candidate).agentConfig;
      const current = fresh.role.agentConfig;
      const before = payloadFromDraft(draftFromRole(selected)).agentConfig;
      const changes: Record<string, unknown> = {};
      for (const key of ['browserUse', 'computerUse', 'screenPerception', 'novelai', 'coding', 'shell', 'spawn'] as const) {
        if (JSON.stringify(config[key]) !== JSON.stringify(before[key])) changes[key] = config[key];
      }
      if (candidate.todoSearchEnabled !== draftFromRole(selected).todoSearchEnabled) {
        const updated = payloadFromDraft({ ...draftFromRole(fresh.role), todoSearchEnabled: candidate.todoSearchEnabled }).agentConfig;
        changes.enabledTools = updated.enabledTools;
        changes.enabledPlugins = updated.enabledPlugins;
        changes.grantedCapabilities = updated.grantedCapabilities;
      }
      const { role: saved } = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selected.id)}`, {
        method: 'PUT', body: JSON.stringify({ name: fresh.role.name, description: fresh.role.description,
          profile: fresh.role.profile, agentConfig: { ...current, ...changes } }),
      });
      if (request !== generation.current) return false;
      setRoles((items) => items.map((item) => item.id === saved.id ? saved : item));
      onRoleSaved?.(saved);
      setDraft(draftFromRole(saved)); setNotice('插件设置已保存');
      return true;
    } catch (cause) {
      if (request === generation.current) setError(errorMessage(cause, '无法保存插件设置'));
      return false;
    } finally { if (request === generation.current) setBusy(false); }
  };
  return <section className="plugin-settings" aria-label="插件设置">
    <h2>插件</h2>
    <SettingsTabs label="插件分类" items={[{ id: 'roles', label: '角色功能' }, { id: 'services', label: '服务与连接' }]} current={tab} onSelect={setTab} disabled={busy} />
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {tab === 'services' && <div className="plugin-services">{['连接', '功能'].map(group => <section key={group} aria-label={group}>
      <h4>{group}</h4><div className="plugin-service-list">{pluginServices.filter(service => service.group === group).map(service =>
        <button type="button" key={service.id} className="plugin-service-entry" aria-label={service.label} disabled={!onOpenService} onClick={() => onOpenService?.(service.id)}>
          <span><strong>{service.label}</strong><small>{service.description}</small></span><span aria-hidden="true">›</span>
        </button>)}</div></section>)}</div>}
    <div hidden={tab !== 'roles'} className="plugin-role-page">
    {roles.length === 0 && <p>请先创建角色。</p>}
    {roles.length > 0 && <label className="field"><span>配置角色</span>
      <select aria-label="配置角色" disabled={busy} value={selectedId} onChange={(event) => {
        generation.current += 1; setSelectedId(event.target.value); setError(''); setNotice('');
        const role = roles.find((item) => item.id === event.target.value);
        setDraft(role ? draftFromRole(role) : null);
      }}>{roles.map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}</select>
    </label>}
    {selected && draft && <RolePluginSettings key={selected.id} roleId={selected.id} draft={draft}
      savedDraft={draftFromRole(selected)} onChange={setDraft} onSave={save} disabled={busy}
      onOpenService={onOpenService} serviceVersion={serviceVersion} />}
    </div>
  </section>;
}
