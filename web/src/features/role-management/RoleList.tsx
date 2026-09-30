import type { Role } from "../../api/types";
import { Avatar } from "../../ui/Avatar";
import { Placeholder } from "../../ui/Status";

type Props = { roles: Role[]; loading: boolean; loadFailed: boolean; selectedId: string | null; onSelect: (role: Role) => void; onOpenChat: (role: Role) => void };

export function RoleList({ roles, loading, loadFailed, selectedId, onSelect, onOpenChat }: Props) {
  if (loading) return <Placeholder loading>正在加载角色…</Placeholder>;
  if (!roles.length && loadFailed) return null;
  if (!roles.length) return <Placeholder>还没有角色。</Placeholder>;
  return <ul className="role-list" aria-label="角色列表">
    {roles.map((role) => <li key={role.id} className={`role-item${role.id === selectedId ? " active" : ""}`}>
      <button type="button" className="role-select" aria-current={role.id === selectedId || undefined} onClick={() => onSelect(role)}>
        <Avatar id={role.id} name={role.name} />
        <span className="role-text">
          <strong>{role.name}</strong>
          <span>{role.description || "暂无简介"}</span>
        </span>
      </button>
      <button type="button" className="ghost role-open" aria-label={`进入“${role.name}”的会话`} onClick={() => onOpenChat(role)}>进入会话</button>
    </li>)}
  </ul>;
}
