import { useEffect, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { Role } from '../../api/types';
import { plugins, type PluginId, type PluginState } from '../../api/plugins';
import { novelai, type NovelaiConfiguration } from '../../api/novelai';
import { getCodingConfiguration, type CodingConfiguration } from '../../api/coding';
import { NovelaiSettings } from '../novelai/NovelaiSettings';
import { availabilityLabel, generationAvailability } from '../novelai/availability';
import { CodingSettings } from '../coding/CodingSettings';
import { TodoSettings } from '../todo/TodoSettings';
import { PluginRow } from './PluginRow';
import { PluginParameters } from './PluginParameters';
import { RolePurposeSettings, type RolePurpose } from './RolePurposeSettings';
import { Icon } from '../../ui/Icon';
import { pluginServices, type SettingsServiceId } from './navigation';

type Row = { id: PluginId; name: string; description: string; enabled?: boolean; state?: PluginState; problem?: string; status?: string };
const services: Row[] = [
  { id: 'novelai', name: 'NovelAI 生图', description: '在聊天中生成角色表情，或使用图片工作台；统一管理生图参数与额度。' },
  { id: 'coding', name: '编程委托', description: '将任务委托给已授权的可信仓库和专用 CLI。' },
  { id: 'todo', name: '待办', description: '查询、管理主人的待办和提醒。' },
];
const rolePurposes = new Set<PluginId>(['novelai', 'coding', 'pet', 'autonomy']);
const publicParameters = new Set<PluginId>(['browser', 'screen', 'herdr']);
const displayNames = { browser: 'Browser Use', computer: 'Computer Use', autonomy: '主动推送' };
const displayOrder: PluginId[] = ['browser', 'computer', 'pet', 'herdr', 'coding', 'novelai', 'autonomy', 'screen', 'todo'];
type TodoConfiguration = { enabled: boolean; remindersEnabled: boolean; timezone: string };

export function PluginSettings({ onRoleSaved, onOpenService, serviceVersion = 0 }: {
  onRoleSaved?: (role: Role) => void; onOpenService?: (id: SettingsServiceId) => void;
  serviceVersion?: number;
}) {
  const [rows, setRows] = useState<Row[]>([]);
  const [active, setActive] = useState<PluginId | null>(null);
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
          ? host.value.plugins.map(state => ({ ...state, name: state.id in displayNames ? displayNames[state.id as keyof typeof displayNames] : state.name,
            description: state.id === 'autonomy' ? '主动与你交流，空闲时执行任务。' : state.description, state })) : [];
        if (host.status === 'rejected') setError(errorMessage(host.reason, '无法读取插件列表'));
        external.forEach((result, index) => {
          const service = services[index];
          configured.push({ ...service, enabled: result.status === 'fulfilled' && 'enabled' in result.value ? result.value.enabled : undefined,
            status: service.id === 'novelai' && result.status === 'fulfilled' ? availabilityLabel(generationAvailability(result.value as NovelaiConfiguration)) : undefined,
            problem: result.status === 'rejected' ? '状态读取失败' : undefined });
        });
        setRows(configured.sort((a, b) => displayOrder.indexOf(a.id) - displayOrder.indexOf(b.id)));
      });
    return () => { current = false; };
  }, [serviceVersion, refresh]);
  const selected = rows.find(row => row.id === active);
  const configured = (state: PluginState) => setRows(items => items.map(row => row.id === state.id ? { ...row, enabled: state.enabled, state } : row));
  async function toggle(row: Row) {
    if (busy || row.enabled === undefined) return;
    setBusy(true); setError(''); setNotice('');
    try {
      if (row.state) configured(await plugins.enabled(row.state, row.state.cleanupPending ? false : !row.enabled));
      else {
        let status = row.status;
        if (row.id === 'novelai') {
          const fresh = await novelai.configuration();
          const saved = await api<NovelaiConfiguration>('/api/services/novelai', { method: 'PUT', body: JSON.stringify({ enabled: !row.enabled, expectedRevision: fresh.revision }) });
          status = availabilityLabel(generationAvailability(saved));
        } else if (row.id === 'coding') await api<CodingConfiguration>('/api/coding/configuration', { method: 'PUT', body: JSON.stringify({ enabled: !row.enabled }) });
        else {
          await api<TodoConfiguration>('/api/todos/settings', { method: 'PUT', body: JSON.stringify({ enabled: !row.enabled }) });
        }
        setRows(items => items.map(item => item.id === row.id ? { ...item, enabled: !row.enabled, status } : item));
      }
      setNotice('全局插件状态已保存');
    } catch (cause) { setError(errorMessage(cause, '插件操作失败')); setRefresh(value => value + 1); }
    finally { setBusy(false); }
  }
  const open = (id: PluginId) => { setActive(id); setRolesOpen(false); setError(''); setNotice(''); };
  const activeName = selected?.name ?? '';
  const roleOnly = active === 'pet' || active === 'autonomy';
  return <section className="plugin-settings" aria-label="插件设置">
    <header className="settings-page-heading">
      {active !== null && <button type="button" className="ghost settings-back" disabled={busy}
        onClick={() => { setActive(null); setRolesOpen(false); setRefresh(value => value + 1); }}><Icon name="back" size={18} />返回插件列表</button>}
      <h2>{active === null ? '插件' : active === 'todo' ? '待办与提醒' : activeName}</h2>
    </header>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {active === null ? <>
      <section className="settings-group" aria-label="功能插件"><div className="plugin-group-heading"><h3>功能</h3><span>{rows.length}</span>
        {rows.some(row => row.problem || row.state?.cleanupPending) && <span className="plugin-problem-count">{rows.filter(row => row.problem || row.state?.cleanupPending).length} 项异常</span>}</div>
        <div className="plugin-list" aria-label="可选插件">{rows.map(row =>
        <PluginRow key={row.id} name={row.name} description={row.description} enabled={row.enabled}
          status={row.state?.cleanupPending ? '等待资源收尾' : row.problem ?? row.status ?? (row.enabled === undefined ? '正在读取' : row.enabled ? '已启用' : '已关闭')}
          busy={busy} onConfigure={publicParameters.has(row.id) || rolePurposes.has(row.id) || services.some(service => service.id === row.id) ? () => open(row.id) : undefined} onToggle={() => void toggle(row)}
          startLabel={row.state?.cleanupPending ? '重试关闭' : undefined} />)}</div></section>
        <div className="plugin-services">{['连接', '资料'].map(group => <section className="settings-group" key={group} aria-label={group}>
          <h3>{group}</h3><div className="plugin-service-list">{pluginServices.filter(service => group === '连接' ? service.group === '连接' : service.id === 'owner-knowledge').map(service =>
            <button type="button" key={service.id} className="plugin-service-entry" aria-label={service.label} disabled={!onOpenService} onClick={() => onOpenService?.(service.id)}>
              <span><strong>{service.label}</strong><small>{service.description}</small></span><span aria-hidden="true">›</span>
            </button>)}</div></section>)}
        </div>
    </> : <section className="plugin-detail" aria-label={activeName + '设置'}>
      {selected?.state && publicParameters.has(active) && <PluginParameters key={active} plugin={selected.state} onSaved={configured} />}
      {active === 'novelai' && <NovelaiSettings key={serviceVersion} showTitle={false} />}
      {active === 'coding' && <CodingSettings key={serviceVersion} showTitle={false} />}
      {active === 'todo' && <TodoSettings showTitle={false} />}
      {rolePurposes.has(active) && <>
        {!roleOnly && <button type="button" className="ghost" aria-expanded={rolesOpen} onClick={() => setRolesOpen(value => !value)}>
          {rolesOpen ? '收起角色参数' : '角色参数'}</button>}
        {(roleOnly || rolesOpen) && <RolePurposeSettings key={active} purpose={active as RolePurpose} onRoleSaved={onRoleSaved} serviceVersion={serviceVersion} />}
      </>}
    </section>}
  </section>;
}
