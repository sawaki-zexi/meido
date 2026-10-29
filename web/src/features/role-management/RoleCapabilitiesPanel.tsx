import { useId, useState } from 'react';
import { createPortal } from 'react-dom';
import type { Role } from '../../api/types';
import { Icon, IconButton, type IconName } from '../../ui/Icon';
import { Dialog } from '../../ui/Dialog';
import { Notice } from '../../ui/Status';
import { RolePurposeSettings } from '../settings/RolePurposeSettings';
import { useRoleCapabilities, type RuntimeCapability } from './useRoleCapabilities';
import { RoleServiceCard, useRoleServices } from './RoleServiceCapabilities';
import type { SettingsSectionId } from '../settings/navigation';

type Props = { role: Role; onRoleSaved?: (role: Role) => void; onOpenSettings?: (section?: SettingsSectionId) => void; actionTarget?: HTMLElement | null; settingsVersion?: number };
type Configurable = Exclude<RuntimeCapability, 'reminders' | 'natural'>;
const names = { natural: '自然聊天', autonomy: '主动推送', pet: '桌宠', novelai: '自动场景 CG', reminders: '待办提醒' };

export function RoleCapabilitiesPanel(props: Props) {
  return <RuntimeCapabilities key={props.role.id} {...props} />;
}

function RuntimeCapabilities({ role, onRoleSaved, onOpenSettings, actionTarget, settingsVersion = 0 }: Props) {
  const state = useRoleCapabilities(role.id, onRoleSaved, settingsVersion);
  const [refresh, setRefresh] = useState(0);
  const services = useRoleServices(settingsVersion + refresh);
  const [configure, setConfigure] = useState<Configurable | null>(null);
  const usageId = useId();
  const closeConfiguration = () => { const key = configure; setConfigure(null); if (key) void state.load(key); };
  const selectedPet = state.pet?.packages.some(item => item.id === state.pet?.selectedPackageId);
  const status = (key: RuntimeCapability, ready: boolean) => state.loadErrors[key] ? '读取失败' : !ready ? '正在读取…'
    : state.draft[key] !== state.saved[key] ? state.draft[key] ? '待启用' : '待停用'
    : state.saved[key] && services.host.find(item => item.id === key)?.enabled === false ? '全局已暂停'
    : state.saved[key] ? '已启用' : '未启用';
  const reminderReady = Boolean(state.reminders?.settings.enabled && state.reminders.settings.remindersEnabled);
  const reminderStatus = !state.reminders || state.loadErrors.reminders || state.draft.reminders !== state.saved.reminders ? status('reminders', !!state.reminders)
    : !reminderReady ? '全局已暂停' : !state.saved.reminders ? '未启用'
    : !state.reminders.role.enabled && state.reminders.role.activeToday && state.reminders.role.effectiveDate > state.reminders.today.date
      ? `${state.reminders.role.effectiveDate} 起停用 · ${state.reminders.today.paused ? '今日已暂停' : '今日仍参与'}`
    : state.reminders.role.effectiveDate > state.reminders.today.date && !state.reminders.role.activeToday ? `${state.reminders.role.effectiveDate} 起生效`
    : state.reminders.today.paused ? '今日已暂停' : '已加入提醒';
  const actions = <div className="role-capability-actions">
    <button type="button" className="ghost" disabled={state.busy || !state.dirty} onClick={state.reset}><Icon name="undo" size={17} />重置</button>
    <button type="button" className="primary" disabled={state.busy || !state.dirty} onClick={() => void state.save()}><Icon name="save" size={17} />{state.busy && state.dirty ? '保存中…' : '保存'}</button>
  </div>;
  const card = (title: string, icon: IconName, label: string, checked: boolean, disabled: boolean, toggle: () => void, settings?: Exclude<RuntimeCapability, 'natural'>, description?: string) =>
    <div className="role-runtime-card" key={title}>
      <span className="role-runtime-icon"><Icon name={icon} size={23} /></span>
      <div className="role-runtime-copy"><h3>{title}</h3><span className={`role-runtime-status${checked && label === '已启用' ? ' is-enabled' : ''}`}>{label}</span>
        {settings === 'autonomy' && checked && <small className="role-runtime-usage" id={usageId}>使用角色聊天模型，开启后会产生模型用量。</small>}
        {settings === 'reminders' && <small className="role-runtime-usage" id={`${usageId}-reminders`}>启用次日生效，关闭立即生效。每天选一位角色提醒。</small>}
        {description && <small className="role-runtime-usage" id={`${usageId}-${icon}`}>{description}</small>}
      </div>
      <button type="button" role="switch" aria-label={title} aria-checked={checked} title={label} className="settings-switch"
        aria-describedby={description ? `${usageId}-${icon}` : settings === 'reminders' ? `${usageId}-reminders` : settings === 'autonomy' && checked ? usageId : undefined} disabled={state.busy || disabled} onClick={toggle}><span /></button>
      {settings && <IconButton icon="settings" label={`${title}设置`} disabled={state.busy || (settings === 'reminders' && !onOpenSettings)} onClick={() => settings === 'reminders' ? onOpenSettings?.('todo') : setConfigure(settings)} />}
    </div>;
  return <section className="role-capabilities" aria-label="角色能力">
    <header className="role-section-heading"><h2>运行能力</h2><IconButton icon="refresh" label="刷新能力" disabled={state.busy} onClick={() => { void state.load(undefined, true); setRefresh(value => value + 1); }} />{actionTarget ? createPortal(actions, actionTarget) : actions}</header>
    {state.error && <Notice onDismiss={state.dismissError}>{state.error}<button type="button" className="ghost" disabled={state.busy} onClick={() => void state.load()}>重新读取设置</button></Notice>}
    {state.notice && <p className="role-capability-feedback" role="status">{state.notice}</p>}
    {Object.entries(state.loadErrors).filter(([, message]) => message).map(([key, message]) => <div className="role-capability-error" role="alert" key={key}>
      <span>{names[key as RuntimeCapability]}：{message}</span><button type="button" className="ghost" disabled={state.busy} onClick={() => void state.load(key as RuntimeCapability)}>重试</button>
    </div>)}
    <div className="role-runtime-grid">
      {card('自然聊天', 'chat', status('natural', !!state.sceneRole), state.draft.natural, !state.sceneRole || !!state.loadErrors.natural,
        () => state.change('natural', !state.draft.natural), undefined, '普通对话中由角色决定分条回复；不会开启群聊主动接话。')}
      <RoleServiceCard service={services.owner} onOpenSettings={onOpenSettings} disabled={state.busy} />
      {card('待办提醒', 'todo', reminderStatus, state.draft.reminders, !state.reminders || !!state.loadErrors.reminders || (!reminderReady && !state.draft.reminders), () => state.change('reminders', !state.draft.reminders), 'reminders')}
      {card('主动推送', 'bell', state.push && !state.push.modelReady && !state.draft.autonomy ? '未配置模型' : status('autonomy', !!state.push),
        state.draft.autonomy, !state.push || !!state.loadErrors.autonomy || (!state.push.modelReady && !state.draft.autonomy), () => state.change('autonomy', !state.draft.autonomy), 'autonomy')}
      {card('桌宠', 'monitor', state.pet && !selectedPet && !state.draft.pet ? '未配置桌宠' : status('pet', !!state.pet),
        state.draft.pet, !state.pet || !!state.loadErrors.pet || (!selectedPet && !state.draft.pet), () => state.change('pet', !state.draft.pet), 'pet')}
      {card('自动场景 CG', 'image', status('novelai', !!state.sceneRole), state.draft.novelai, !state.sceneRole || !!state.loadErrors.novelai,
        () => state.draft.novelai || state.saved.novelai ? state.change('novelai', !state.draft.novelai) : setConfigure('novelai'), 'novelai')}
    </div>
    <section className="role-shared-capabilities" aria-label="共享能力"><header className="role-section-heading"><div><h2>共享能力</h2><p>这些服务的开关对所有角色生效；资料和操作范围仍遵循各自授权。</p></div></header>
      <div className="role-runtime-grid">
        <RoleServiceCard service={{ id: 'todo', title: '待办管理', icon: 'todo', section: 'todo', status: state.loadErrors.reminders ? '读取失败' : state.reminders ? state.reminders.settings.enabled ? '已启用' : '已关闭' : '正在读取…', description: '协助管理同一份主人待办清单，提醒资格单独设置。' }} onOpenSettings={onOpenSettings} disabled={state.busy} />
        {services.services.map(service => <RoleServiceCard key={service.id} service={service} onOpenSettings={onOpenSettings} disabled={state.busy} />)}
      </div>
    </section>
    {onOpenSettings && <button type="button" className="ghost role-capability-more" disabled={state.busy} onClick={() => onOpenSettings('plugins')}>更多工具与插件<Icon name="back" size={14} /></button>}
    {configure && <Dialog title={`${names[configure]}设置`} modalFocus onClose={closeConfiguration}>
      <div className="role-runtime-settings"><RolePurposeSettings purpose={configure} roleId={role.id} onRoleSaved={state.receiveScene} />
        {onOpenSettings && <button type="button" className="ghost" onClick={() => { closeConfiguration(); onOpenSettings(configure === 'novelai' ? 'novelai' : 'plugins'); }}>打开服务设置</button>}
      </div>
    </Dialog>}
  </section>;
}
