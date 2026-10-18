import { useEffect, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { Role } from '../../api/types';
import { plugins, type PluginId, type PluginState } from '../../api/plugins';
import { novelai, type NovelaiConfiguration } from '../../api/novelai';
import { getCodingConfiguration, type CodingConfiguration } from '../../api/coding';
import { NovelaiSettings } from '../novelai/NovelaiSettings';
import { CodingSettings } from '../coding/CodingSettings';
import { TodoSettings } from '../todo/TodoSettings';
import { PluginRow } from './PluginRow';
import { PluginParameters } from './PluginParameters';
import { RolePurposeSettings, type RolePurpose } from './RolePurposeSettings';
import { SettingsTabs } from './SettingsTabs';
import { pluginServices, type SettingsServiceId } from './navigation';

type Row = { id: PluginId; name: string; description: string; enabled?: boolean; state?: PluginState; problem?: string };
const services: Row[] = [
  { id: 'novelai', name: 'NovelAI 生图', description: '手动生图使用公共许可；场景 CG 按角色配置。' },
  { id: 'coding', name: '编程委托', description: '将任务委托给已授权的可信仓库和专用 CLI。' },
  { id: 'todo', name: '待办', description: '查询、管理主人的待办和提醒。' },
];
const rolePurposes = new Set<PluginId>(['novelai', 'coding', 'pet', 'autonomy']);
type TodoConfiguration = { enabled: boolean; remindersEnabled: boolean; timezone: string };

export function PluginSettings({ onRoleSaved, onOpenService, serviceVersion = 0, initialTab = 'roles' }: {
  onRoleSaved?: (role: Role) => void; onOpenService?: (id: SettingsServiceId) => void;
  serviceVersion?: number; initialTab?: 'roles' | 'services';
}) {
  const [tab, setTab] = useState<'roles' | 'services'>(initialTab);
  const [rows, setRows] = useState<Row[]>([]);
  const [active, setActive] = useState<PluginId | 'advanced' | null>(null);
  const [rolesOpen, setRolesOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let current = true;
    void Promise.allSettled([plugins.list(), novelai.configuration(), getCodingConfiguration(), api<TodoConfiguration>('/api/todos/settings')])
      .then(results => {
        if (!current) return;
        const [host, ...external] = results;
        const configured: Row[] = host.status === 'fulfilled' && 'plugins' in host.value
          ? host.value.plugins.map(state => ({ ...state, state })) : [];
        if (host.status === 'rejected') setError(errorMessage(host.reason, '无法读取插件列表'));
        external.forEach((result, index) => {
          const service = services[index];
          configured.push({ ...service, enabled: result.status === 'fulfilled' && 'enabled' in result.value ? result.value.enabled : undefined,
            problem: result.status === 'rejected' ? '状态读取失败' : undefined });
        });
        setRows(configured);
      });
    return () => { current = false; };
  }, [serviceVersion, refresh]);
  const selected = rows.find(row => row.id === active);
  const configured = (state: PluginState) => setRows(items => items.map(row => row.id === state.id ? { ...row, enabled: state.enabled, state } : row));
  async function toggle(row: Row) {
    if (busy || row.enabled === undefined) return;
    if (row.id === 'screen' && !row.enabled && !row.state?.values.visionModelConfigurationId) {
      setActive('screen'); setRolesOpen(false); setNotice('请先选择公共视觉连接并保存参数，再启动插件。'); return;
    }
    setBusy(true); setError(''); setNotice('');
    try {
      if (row.state) configured(await plugins.enabled(row.state, row.state.cleanupPending ? false : !row.enabled));
      else {
        if (row.id === 'novelai') {
          const fresh = await novelai.configuration();
          await api<NovelaiConfiguration>('/api/services/novelai', { method: 'PUT', body: JSON.stringify({ enabled: !row.enabled, expectedRevision: fresh.revision }) });
        } else if (row.id === 'coding') await api<CodingConfiguration>('/api/coding/configuration', { method: 'PUT', body: JSON.stringify({ enabled: !row.enabled }) });
        else {
          await api<TodoConfiguration>('/api/todos/settings', { method: 'PUT', body: JSON.stringify({ enabled: !row.enabled }) });
        }
        setRows(items => items.map(item => item.id === row.id ? { ...item, enabled: !row.enabled } : item));
      }
      setNotice('全局插件状态已保存');
    } catch (cause) { setError(errorMessage(cause, '插件操作失败')); setRefresh(value => value + 1); }
    finally { setBusy(false); }
  }
  const open = (id: PluginId | 'advanced') => { setActive(id); setRolesOpen(false); setError(''); setNotice(''); };
  const activeName = selected?.name ?? (active === 'advanced' ? '高级运行设置' : '');
  return <section className="plugin-settings" aria-label="插件设置">
    <h2>插件</h2>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {active === null ? <>
      <SettingsTabs label="插件分类" items={[{ id: 'roles', label: '功能插件' }, { id: 'services', label: '服务与连接' }]} current={tab} onSelect={setTab} disabled={busy} />
      {tab === 'roles' ? <div className="plugin-list" aria-label="可选插件">{rows.map(row =>
        <PluginRow key={row.id} name={row.name} description={row.description} enabled={row.enabled}
          status={row.state?.cleanupPending ? '等待资源收尾' : row.problem ?? (row.enabled === undefined ? '正在读取' : row.enabled ? '已启用' : '已关闭')}
          busy={busy} onConfigure={() => open(row.id)} onToggle={() => void toggle(row)}
          startLabel={row.state?.cleanupPending ? '重试关闭' : undefined} />)}</div>
        : <div className="plugin-services">{['连接', '功能'].map(group => <section key={group} aria-label={group}>
          <h4>{group}</h4><div className="plugin-service-list">{pluginServices.filter(service => service.group === group).map(service =>
            <button type="button" key={service.id} className="plugin-service-entry" aria-label={service.label} disabled={!onOpenService} onClick={() => onOpenService?.(service.id)}>
              <span><strong>{service.label}</strong><small>{service.description}</small></span><span aria-hidden="true">›</span>
            </button>)}</div></section>)}
          <button type="button" className="ghost plugin-advanced" onClick={() => open('advanced')}>高级运行设置</button>
        </div>}
    </> : <section className="plugin-detail" aria-label={activeName + '设置'}>
      <div className="settings-subheading"><h3>{activeName}</h3><button type="button" className="ghost" disabled={busy}
        onClick={() => { setActive(null); setRolesOpen(false); setRefresh(value => value + 1); }}>返回插件列表</button></div>
      {selected?.state && <PluginParameters key={active} plugin={selected.state} onSaved={configured} />}
      {active === 'novelai' && <NovelaiSettings key={serviceVersion} />}
      {active === 'coding' && <CodingSettings key={serviceVersion} />}
      {active === 'todo' && <TodoSettings />}
      {(active === 'advanced' || rolePurposes.has(active)) && <>
        <button type="button" className="ghost" aria-expanded={rolesOpen} onClick={() => setRolesOpen(value => !value)}>
          {rolesOpen ? '收起角色参数' : '角色参数'}</button>
        {rolesOpen && <RolePurposeSettings key={active} purpose={active as RolePurpose} onRoleSaved={onRoleSaved} serviceVersion={serviceVersion} />}
      </>}
    </section>}
  </section>;
}
