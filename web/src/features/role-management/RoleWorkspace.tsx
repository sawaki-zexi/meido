import { roleMood } from './roleMood';
import { useEffect, useId, useState, type ComponentProps } from "react";
import type { Role } from "../../api/types";
import { Avatar } from "../../ui/Avatar";
import { Icon, IconButton } from "../../ui/Icon";
import { MemoryPanel } from "../memory/MemoryPanel";
import { RoleAccountsPanel } from "../channels/RoleAccountsPanel";
import { RoleMaterialLibrary } from "../materials/RoleMaterialLibrary";
import { RoleIllustrationLibrary } from './RoleIllustrationLibrary';
import { RoleEditor } from "./RoleEditor";
import { RoleCapabilitiesPanel } from "./RoleCapabilitiesPanel";
import type { RolesState } from "./useRoles";
import type { SettingsSectionId } from '../settings/navigation';

export type RoleWorkspaceTab = "profile" | "memory" | "capabilities" | "accounts" | "materials";

type Props = {
  role: Role;
  state: RolesState;
  tab: RoleWorkspaceTab;
  onTabChange: (tab: RoleWorkspaceTab) => void;
  onBack: () => void;
  backLabel: string;
  onChat: () => void;
  chatDisabled: boolean;
  onEdit: () => void;
  onSubmit: () => void;
  onCancel: () => void;
  onDelete: () => void;
  onOpenSettings: (section?: SettingsSectionId) => void;
  settingsVersion?: number;
  onOpenChannelSettings: () => void;
  onOpenMemorySource: (origin: Parameters<NonNullable<ComponentProps<typeof MemoryPanel>["onOpenSource"]>>[0], messageId?: string) => void;
};

const tabs: Array<{ id: RoleWorkspaceTab; label: string }> = [
  { id: "profile", label: "资料" },
  { id: "memory", label: "记忆" },
  { id: "materials", label: "素材库" },
  { id: "capabilities", label: "能力" },
  { id: "accounts", label: "账号" },
];

export function RoleWorkspace({ role, state, tab, onTabChange, onBack, backLabel, onChat, chatDisabled, onEdit, onSubmit, onCancel, onDelete, onOpenSettings, onOpenChannelSettings, onOpenMemorySource, settingsVersion }: Props) {
  const writable = state.editing || state.creating;
  const workspaceId = useId();
  const [actionTarget, setActionTarget] = useState<HTMLDivElement | null>(null);
  const [mediaOpen, setMediaOpen] = useState(false);
  const profileFormId = `${workspaceId}-profile-form`;
  const profileDirty = state.draft.name !== role.name || state.draft.description !== role.description
    || (['profile', 'personality', 'behaviorRules', 'responseConstraints', 'nickname'] as const).some(key => state.draft[key] !== role.profile[key])
    || Boolean(state.avatarFile || state.cardImageFile || state.avatarRemoved || state.cardImageRemoved);
  const cropping = Boolean(state.avatarSource || state.cardImageSource);
  const changeIdentity = (key: 'name' | 'description', value: string) => { if (!state.editing) state.startEdit(); state.setDraft({ ...state.draft, [key]: value }); };
  const [capabilitiesOpened, setCapabilitiesOpened] = useState(tab === 'capabilities');
  useEffect(() => { if (tab === 'capabilities') setCapabilitiesOpened(true); }, [tab]);
  return <section className="role-workspace" aria-label={`${role.name}的角色工作区`}>
    <header className="role-workspace-hero">
      <Avatar id={role.id} name={writable ? state.draft.name : role.name} avatarUrl={state.avatarPreviewUrl ?? (state.avatarRemoved ? null : role.avatarUrl)} size="lg" />
      <div className="role-workspace-identity">{writable || tab === 'profile' ? <><input aria-label="名称（角色名称）" disabled={state.busy} value={state.draft.name} placeholder="未命名角色" onChange={event => changeIdentity('name', event.target.value)} /><input aria-label="角色简介" disabled={state.busy} value={state.draft.description} placeholder="添加一行角色简介" onChange={event => changeIdentity('description', event.target.value)} /></> : <><h1>{role.name}</h1><p>{role.description || "还没有简介"}</p></>}
        <div className="role-workspace-actions"><span className="role-state"><Icon name="profile" size={15} />{roleMood(role).mood}</span>
          <button type="button" className="ghost" disabled={state.busy} aria-expanded={mediaOpen && tab === 'profile'} onClick={() => { onEdit(); setMediaOpen(value => tab !== 'profile' || !value); }}><Icon name="image" size={16} />更换形象</button>
        </div>
      </div>
    </header>
    <div className="role-workspace-toolbar">
      <IconButton icon="back" label={backLabel} onClick={onBack} disabled={state.busy} />
      <nav className="role-workspace-tabs" role="tablist" aria-label="角色详情">
        {tabs.map((item, index) => <button key={item.id} id={`${workspaceId}-${item.id}-tab`} type="button" role="tab" aria-selected={tab === item.id} tabIndex={tab === item.id ? 0 : -1} onClick={() => onTabChange(item.id)} onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
          onTabChange(tabs[next].id); document.getElementById(`${workspaceId}-${tabs[next].id}-tab`)?.focus();
        }}>{item.label}</button>)}
      </nav>
      <div className="role-workspace-toolbar-actions">
        <button type="button" className="ghost role-workspace-chat" onClick={onChat} disabled={chatDisabled || state.busy}><Icon name="chat" size={16} />去聊天</button>
        {tab === 'profile' && <div className="role-capability-actions">
          <button type="button" className="ghost" disabled={state.busy || !profileDirty || cropping} onClick={onCancel}><Icon name="undo" size={17} />重置</button>
          <button type="submit" form={profileFormId} className="primary" disabled={state.busy || !profileDirty || cropping}><Icon name="save" size={17} />{state.saving ? '保存中…' : '保存'}</button>
        </div>}
        <div ref={setActionTarget} />
      </div>
    </div>
    <div className="role-workspace-content">
      {tab === "profile" && <div className="role-workspace-profile" role="tabpanel" aria-label="资料">
        <RoleEditor state={state} onSubmit={onSubmit} onCancel={onCancel} onDelete={onDelete} hideHeaderFields workspace formId={profileFormId} mediaOpen={mediaOpen} />
      </div>}
      {tab === "materials" && <div role="tabpanel" aria-label="素材库"><RoleIllustrationLibrary role={role} onRoleSaved={state.receiveRole} /><RoleMaterialLibrary roleId={role.id} roleName={role.name} /></div>}
      {tab === "memory" && <div className="role-workspace-memory" role="tabpanel" aria-label="记忆"><MemoryPanel role={role} embedded onBack={onBack} onOpenSource={onOpenMemorySource} /></div>}
      {(tab === 'capabilities' || capabilitiesOpened) && <div hidden={tab !== 'capabilities'} role="tabpanel" aria-label="能力">
        <RoleCapabilitiesPanel role={role} onRoleSaved={state.receiveRole} actionTarget={tab === 'capabilities' ? actionTarget : null} onOpenSettings={onOpenSettings} settingsVersion={settingsVersion} />
      </div>}
      {tab === "accounts" && <RoleAccountsPanel roleId={role.id} workspace onOpenSettings={onOpenChannelSettings} />}
    </div>
  </section>;
}
