import type { Role } from "../../api/types";
import { Icon, IconButton } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";
import { RoleCard } from "./RoleCard";

type Props = {
  roles: Role[];
  loading: boolean;
  error: string;
  onDismissError: () => void;
  /** Locks the chat shortcuts while a reply is generating. */
  chatDisabled?: boolean;
  onBack: () => void;
  onCreate: () => void;
  onShow: (role: Role) => void;
  onChat: (role: Role) => void;
  onPreviewAvatar: (role: Role) => void;
};

/** Role tavern: every role as a card. New roles are created here and get their own card. */
export function TavernView({ roles, loading, error, onDismissError, chatDisabled, onBack, onCreate, onShow, onChat, onPreviewAvatar }: Props) {
  return <section className="tavern" aria-label="角色酒馆">
    <header className="chat-header">
      <IconButton icon="back" label="对话列表" className="only-narrow" onClick={onBack} />
      <div className="chat-title">
        <h1>角色酒馆</h1>
        <small>{loading ? "正在整理卡牌…" : `${roles.length} 张卡牌`}</small>
      </div>
    </header>
    <div className="tavern-scroll">
      {error && <Notice onDismiss={onDismissError}>{error}</Notice>}
      {loading ? <Placeholder loading>正在加载角色…</Placeholder>
        : <ul className="card-grid" aria-label="角色卡牌">
          {roles.map((role, index) => <li key={role.id}>
            <RoleCard role={role} number={index + 1} onPreviewAvatar={onPreviewAvatar}>
              <button type="button" className="card-hit" aria-label={`与${role.name}对话`} onClick={() => onChat(role)} disabled={chatDisabled} />
              <IconButton icon="edit" label={`编辑${role.name}`} className="card-edit" onClick={() => onShow(role)} />
            </RoleCard>
          </li>)}
          <li>
            <button type="button" className="card-new" onClick={onCreate}>
              <Icon name="plus" size={30} /><span>新建角色</span>
            </button>
          </li>
        </ul>}
    </div>
  </section>;
}
