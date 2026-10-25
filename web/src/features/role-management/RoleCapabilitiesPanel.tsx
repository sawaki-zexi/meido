import { useId, useState } from 'react';
import { createPortal } from 'react-dom';
import type { Role } from '../../api/types';
import { Icon, IconButton, type IconName } from '../../ui/Icon';
import { Dialog } from '../../ui/Dialog';
import { Notice } from '../../ui/Status';
import { RolePurposeSettings } from '../settings/RolePurposeSettings';
import { useRoleCapabilities, type RuntimeCapability } from './useRoleCapabilities';

type Props = { role: Role; onRoleSaved?: (role: Role) => void; onOpenSettings?: () => void; actionTarget?: HTMLElement | null };
const names = { autonomy: '主动推送', pet: '桌宠', novelai: '自动场景 CG' };

export function RoleCapabilitiesPanel(props: Props) {
  return <RuntimeCapabilities key={props.role.id} {...props} />;
}

function RuntimeCapabilities({ role, onRoleSaved, onOpenSettings, actionTarget }: Props) {
  const state = useRoleCapabilities(role.id, onRoleSaved);
  const [configure, setConfigure] = useState<RuntimeCapability | null>(null);
  const usageId = useId();
  const closeConfiguration = () => { const key = configure; setConfigure(null); if (key) void state.load(key); };
  const selectedPet = state.pet?.packages.some(item => item.id === state.pet?.selectedPackageId);
  const status = (key: RuntimeCapability, ready: boolean) => state.loadErrors[key] ? '读取失败' : !ready ? '正在读取…'
    : state.draft[key] !== state.saved[key] ? state.draft[key] ? '待启用' : '待停用'
    : state.saved[key] ? '已启用' : '未启用';
  const actions = <div className="role-capability-actions">
    <button type="button" className="ghost" disabled={state.busy || !state.dirty} onClick={state.reset}><Icon name="undo" size={17} />重置</button>
    <button type="button" className="primary" disabled={state.busy || !state.dirty} onClick={() => void state.save()}><Icon name="save" size={17} />{state.busy && state.dirty ? '保存中…' : '保存'}</button>
  </div>;
  const card = (title: string, icon: IconName, label: string, checked: boolean, disabled: boolean, toggle: () => void, settings?: RuntimeCapability) =>
    <div className="role-runtime-card" key={title}>
      <span className="role-runtime-icon"><Icon name={icon} size={23} /></span>
      <div className="role-runtime-copy"><h3>{title}</h3><span className={`role-runtime-status${checked && label === '已启用' ? ' is-enabled' : ''}`}>{label}</span>
        {settings === 'autonomy' && checked && <small className="role-runtime-usage" id={usageId}>使用角色聊天模型，开启后会产生模型用量。</small>}
      </div>
      <button type="button" role="switch" aria-label={title} aria-checked={checked} title={label} className="settings-switch"
        aria-describedby={settings === 'autonomy' && checked ? usageId : undefined} disabled={state.busy || disabled} onClick={toggle}><span /></button>
      {settings && <IconButton icon="settings" label={`${title}设置`} disabled={state.busy} onClick={() => setConfigure(settings)} />}
    </div>;
  return <section className="role-capabilities" aria-label="角色能力">
    <header className="role-section-heading"><h2>运行能力</h2>{actionTarget ? createPortal(actions, actionTarget) : actions}</header>
    {state.error && <Notice onDismiss={state.dismissError}>{state.error}<button type="button" className="ghost" disabled={state.busy} onClick={() => void state.load()}>重新读取设置</button></Notice>}
    {state.notice && <p className="role-capability-feedback" role="status">{state.notice}</p>}
    {Object.entries(state.loadErrors).filter(([, message]) => message).map(([key, message]) => <div className="role-capability-error" role="alert" key={key}>
      <span>{names[key as RuntimeCapability]}：{message}</span><button type="button" className="ghost" disabled={state.busy} onClick={() => void state.load(key as RuntimeCapability)}>重试</button>
    </div>)}
    <div className="role-runtime-grid">
      {card('自然聊天', 'chat', '尚未支持', false, true, () => {})}
      {card('NSFW 记忆', 'brain', '尚未支持', false, true, () => {})}
      {card('主动推送', 'bell', state.push && !state.push.modelReady && !state.draft.autonomy ? '未配置模型' : status('autonomy', !!state.push),
        state.draft.autonomy, !state.push || !!state.loadErrors.autonomy || (!state.push.modelReady && !state.draft.autonomy), () => state.change('autonomy', !state.draft.autonomy), 'autonomy')}
      {card('桌宠', 'monitor', state.pet && !selectedPet && !state.draft.pet ? '未配置桌宠' : status('pet', !!state.pet),
        state.draft.pet, !state.pet || !!state.loadErrors.pet || (!selectedPet && !state.draft.pet), () => state.change('pet', !state.draft.pet), 'pet')}
      {card('自动场景 CG', 'image', status('novelai', !!state.sceneRole), state.draft.novelai, !state.sceneRole || !!state.loadErrors.novelai,
        () => state.draft.novelai || state.saved.novelai ? state.change('novelai', !state.draft.novelai) : setConfigure('novelai'), 'novelai')}
    </div>
    {onOpenSettings && <button type="button" className="ghost role-capability-more" onClick={onOpenSettings}>更多工具与插件<Icon name="back" size={14} /></button>}
    {configure && <Dialog title={`${names[configure]}设置`} modalFocus onClose={closeConfiguration}>
      <div className="role-runtime-settings"><RolePurposeSettings purpose={configure} roleId={role.id} onRoleSaved={state.receiveScene} />
        {onOpenSettings && <button type="button" className="ghost" onClick={() => { closeConfiguration(); onOpenSettings(); }}>打开服务设置</button>}
      </div>
    </Dialog>}
  </section>;
}
