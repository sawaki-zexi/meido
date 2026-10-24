import type { ComponentProps } from "react";
import type { Role } from "../../api/types";
import { Avatar } from "../../ui/Avatar";
import { Icon, IconButton } from "../../ui/Icon";
import { MemoryPanel } from "../memory/MemoryPanel";
import { RoleAccountsPanel } from "../channels/RoleAccountsPanel";
import { RoleEditor } from "./RoleEditor";
import { RoleCapabilitiesPanel } from "./RoleCapabilitiesPanel";
import type { RolesState } from "./useRoles";

export type RoleWorkspaceTab = "profile" | "memory" | "capabilities" | "accounts";

type Props = {
  role: Role;
  state: RolesState;
  tab: RoleWorkspaceTab;
  onTabChange: (tab: RoleWorkspaceTab) => void;
  onBack: () => void;
  onChat: () => void;
  onEdit: () => void;
  onSubmit: () => void;
  onCancel: () => void;
  onDelete: () => void;
  onOpenSettings: () => void;
  onOpenChannelSettings: () => void;
  onOpenMemorySource: (origin: Parameters<NonNullable<ComponentProps<typeof MemoryPanel>["onOpenSource"]>>[0], messageId?: string) => void;
};

const tabs: Array<{ id: RoleWorkspaceTab; label: string }> = [
  { id: "profile", label: "资料" },
  { id: "memory", label: "记忆" },
  { id: "capabilities", label: "能力" },
  { id: "accounts", label: "账号" },
];

export function RoleWorkspace({ role, state, tab, onTabChange, onBack, onChat, onEdit, onSubmit, onCancel, onDelete, onOpenSettings, onOpenChannelSettings, onOpenMemorySource }: Props) {
  const writable = state.editing || state.creating;
  return <section className="role-workspace" aria-label={`${role.name}的角色工作区`}>
    <header className="role-workspace-hero">
      <IconButton icon="back" label="返回角色" onClick={onBack} />
      <Avatar id={role.id} name={role.name} avatarUrl={role.avatarUrl} size="lg" />
      <div className="role-workspace-identity"><h1>{role.name}</h1><p>{role.description || "还没有简介"}</p>
        <div className="role-workspace-actions"><span className="role-state"><Icon name="profile" size={15} />平静</span><button type="button" className="ghost" onClick={onEdit}><Icon name="edit" size={15} />{writable ? "编辑中" : "编辑资料"}</button></div>
      </div>
      <button type="button" className="primary role-workspace-chat" onClick={onChat}><Icon name="chat" size={16} />去聊天</button>
    </header>
    <nav className="role-workspace-tabs" role="tablist" aria-label="角色详情">
      {tabs.map(item => <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} onClick={() => onTabChange(item.id)}>{item.label}</button>)}
    </nav>
    <div className="role-workspace-content">
      {tab === "profile" && <div className="role-workspace-profile" role="tabpanel" aria-label="资料">
        {!writable && <div className="role-profile-intro"><span>角色设定</span><p>{role.profile.profile}</p></div>}
        <RoleEditor state={state} onSubmit={onSubmit} onCancel={onCancel} onDelete={onDelete} />
      </div>}
      {tab === "memory" && <div className="role-workspace-memory" role="tabpanel" aria-label="记忆"><MemoryPanel role={role} embedded onBack={onBack} onOpenSource={onOpenMemorySource} /></div>}
      {tab === "capabilities" && <RoleCapabilitiesPanel role={role} onOpenSettings={onOpenSettings} />}
      {tab === "accounts" && <RoleAccountsPanel roleId={role.id} onOpenSettings={onOpenChannelSettings} />}
    </div>
  </section>;
}
