import type { Role } from "../../api/types";
import { payloadFromDraft } from "../role-management/draft";
import { RoleEditor } from "../role-management/RoleEditor";
import type { RolesState } from "../role-management/useRoles";
import { Avatar } from "../../ui/Avatar";
import { Icon } from "../../ui/Icon";
import { RoleCard } from "./RoleCard";

type Props = { state: RolesState; number: number; onSubmit: () => void; onCancel: () => void; onBack: () => void; backLabel: string };

/**
 * Creation occupies the main pane, with a live card preview beside the form.
 * Back keeps the draft; the form's Cancel action discards it.
 */
export function CreateRolePage({ state, number, onSubmit, onCancel, onBack, backLabel }: Props) {
  const { name, description, profile } = payloadFromDraft(state.draft);
  const existing = state.creating ? null : state.selected;
  const preview: Role = {
    id: existing?.id ?? "preview", name: name.trim() || "？", description, profile, createdAt: "", updatedAt: "",
    avatarUrl: state.avatarPreviewUrl ?? (state.avatarRemoved ? null : existing?.avatarUrl ?? null),
    avatarOriginalUrl: existing?.avatarOriginalUrl ?? null,
    cardImageUrl: state.cardImagePreviewUrl ?? (state.cardImageRemoved ? null : existing?.cardImageUrl ?? null),
  };
  return <section className="create-role-page" aria-label="创建角色">
    <header className="role-page-heading">
      <button type="button" className="ghost" disabled={state.busy} onClick={onBack}><Icon name="back" size={18} />{backLabel}</button>
      <div><h1>创建角色</h1><p>写下她的故事，开始你们的日常。</p></div>
    </header>
    <div className="create-role-scroll">
      <header className="shiori-role-identity" aria-label="角色身份">
        <Avatar id={preview.id} name={preview.name} avatarUrl={preview.avatarUrl} size="lg" />
        <div className="shiori-role-identity-fields">
          <input aria-label="名称（角色名称）" required disabled={state.busy} value={state.draft.name} placeholder="给她起个名字" onChange={event => state.setDraft({ ...state.draft, name: event.target.value })} />
          <input aria-label="角色简介" disabled={state.busy} value={state.draft.description} placeholder="添加一行角色简介" onChange={event => state.setDraft({ ...state.draft, description: event.target.value })} />
        </div>
      </header>
      <div className="create-role-layout">
        <div className="shiori-role-editor-body">
          <RoleEditor state={state} onSubmit={onSubmit} onCancel={onCancel} onDelete={() => undefined} hideHeaderFields />
        </div>
        <aside className="shiori-create-preview" aria-label="卡牌预览"><span>卡牌预览</span><RoleCard role={preview} number={number} className={existing ? undefined : "is-draft"} /></aside>
      </div>
    </div>
  </section>;
}
