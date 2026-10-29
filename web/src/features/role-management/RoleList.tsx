import type { Role } from "../../api/types";
import { Avatar } from "../../ui/Avatar";
import { Placeholder } from "../../ui/Status";
import { previewText, previewTime, type ChatPreview } from './chatPreviews';

type Props = { roles: Role[]; previews?: Record<string, ChatPreview>; loading: boolean; loadFailed: boolean; searching?: boolean; activeId: string | null; disabled?: boolean; onOpen: (role: Role) => void };

/** Conversation list: one row per role, like a messenger's chat list. */
export function RoleList({ roles, previews = {}, loading, loadFailed, searching, activeId, disabled, onOpen }: Props) {
  if (loading) return <Placeholder loading>正在加载角色…</Placeholder>;
  if (!roles.length && loadFailed) return null;
  if (!roles.length) return <Placeholder>{searching ? "没有找到匹配的角色" : "还没有角色"}</Placeholder>;
  return <ul className="role-list" aria-label="角色列表">
    {roles.map((role) => <li key={role.id}>
      <button type="button" className={`role-row${role.id === activeId ? " active" : ""}`} aria-current={role.id === activeId || undefined} disabled={disabled && role.id !== activeId} onClick={() => onOpen(role)}>
        <Avatar id={role.id} name={role.name} avatarUrl={role.avatarUrl} />
        <span className="role-text">
          <span className="role-row-top"><strong>{role.name}</strong>{previews[role.id] && <time dateTime={previews[role.id].createdAt}>{previewTime(previews[role.id].createdAt)}</time>}</span>
          <span>{previewText(previews[role.id]) || '\u00a0'}</span>
        </span>
      </button>
    </li>)}
  </ul>;
}
