import type { Role } from "../../api/types";
import { IconButton } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";
import { RoleCard } from "./RoleCard";

type Props = {
  roles: Role[];
  query?: string;
  loading: boolean;
  error: string;
  onDismissError: () => void;
  /** Locks the chat shortcuts while a reply is generating. */
  chatDisabled?: boolean;
  onBack: () => void;
  onShow: (role: Role) => void;
  onWorkspace?: (role: Role) => void;
  onChat: (role: Role) => void;
  onPreviewAvatar: (role: Role) => void;
};

/** Role tavern: every role as a card. New roles are created here and get their own card. */
export function TavernView({ roles, query = "", loading, error, onDismissError, chatDisabled, onBack, onShow, onWorkspace, onChat, onPreviewAvatar }: Props) {
  const cards = roles.map((role, index) => ({ role, number: index + 1 }))
    .filter(({ role }) => `${role.name} ${role.description}`.toLocaleLowerCase().includes(query));
  return <section className="tavern" aria-label="角色酒馆">
    <header className="chat-header tavern-header">
      <IconButton icon="back" label="对话列表" className="only-narrow" onClick={onBack} />
      <div className="chat-title">
        <h1>角色酒馆</h1>
        <small>{loading ? "正在整理卡牌…" : `${roles.length} 张卡牌`}</small>
      </div>
    </header>
    <div className="tavern-scroll">
      {error && <Notice onDismiss={onDismissError}>{error}</Notice>}
      {loading ? <Placeholder loading>正在加载角色…</Placeholder>
        : <>
          {cards.length === 0 && <Placeholder>{query ? "没有匹配的角色卡片，换个关键词试试。" : "还没有角色，从角色列表的“新建”开始吧。"}</Placeholder>}
          <ul className="card-grid" aria-label="角色卡牌">
            {cards.map(({ role, number }) => <li key={role.id}>
              <RoleCard role={role} number={number} onPreviewAvatar={onPreviewAvatar} onWorkspace={onWorkspace}>
                <button type="button" className="card-hit" aria-label={`与${role.name}对话`} onClick={() => onChat(role)} disabled={chatDisabled} />
                <IconButton icon="edit" label={`编辑${role.name}`} className="card-edit" onClick={() => onShow(role)} />
              </RoleCard>
            </li>)}
          </ul>
        </>}
    </div>
  </section>;
}
