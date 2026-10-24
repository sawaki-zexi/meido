import type { Role } from "../../api/types";
import { payloadFromDraft } from "../role-management/draft";
import { RoleEditor } from "../role-management/RoleEditor";
import type { RolesState } from "../role-management/useRoles";
import { Avatar } from "../../ui/Avatar";
import { RoleCard } from "./RoleCard";

type Props = { state: RolesState; number: number; onSubmit: () => void; onCancel: () => void; onDelete?: () => void };

/**
 * Shiori-style role editor: identity stays in a hero card while the profile
 * fields remain below it. The same surface is used for creation and editing.
 */
export function CreateRolePage({ state, number, onSubmit, onCancel, onDelete = () => undefined }: Props) {
  const { name, description, profile } = payloadFromDraft(state.draft);
  const existing = state.creating ? null : state.selected;
  const preview: Role = {
    id: existing?.id ?? "preview", name: name.trim() || "？", description, profile, createdAt: "", updatedAt: "",
    avatarUrl: state.avatarPreviewUrl ?? (state.avatarRemoved ? null : existing?.avatarUrl ?? null),
    avatarOriginalUrl: existing?.avatarOriginalUrl ?? null,
    cardImageUrl: state.cardImagePreviewUrl ?? (state.cardImageRemoved ? null : existing?.cardImageUrl ?? null),
  };
  return <div className="shiori-role-editor">
    <header className="shiori-role-identity" aria-label="角色身份">
      <Avatar id={preview.id} name={preview.name} avatarUrl={preview.avatarUrl} size="lg" />
      <div className="shiori-role-identity-fields">
        <input aria-label="名称（角色名称）" required value={state.draft.name} placeholder="给她起个名字" onChange={event => state.setDraft({ ...state.draft, name: event.target.value })} />
        <input aria-label="角色简介" value={state.draft.description} placeholder="添加一行角色简介" onChange={event => state.setDraft({ ...state.draft, description: event.target.value })} />
        <div className="shiori-role-identity-actions">
          <span className="role-state">角色设定</span>
          <button type="button" className="ghost" onClick={onCancel}>返回角色列表</button>
        </div>
      </div>
    </header>
    <div className="shiori-role-editor-toolbar"><span>{state.creating ? "创建角色" : "编辑角色资料"}</span><small>角色设定、性格与回复规则</small></div>
    <aside className="shiori-create-preview" aria-label="卡牌预览"><RoleCard role={preview} number={number} className={existing ? undefined : "is-draft"} /></aside>
    <div className="shiori-role-editor-body">
      <label className="field shiori-nickname-field"><span>对你的称呼</span><input aria-label="昵称" value={state.draft.nickname} placeholder="例如：主人" disabled={state.busy} onChange={event => state.setDraft({ ...state.draft, nickname: event.target.value })} /></label>
      <RoleEditor state={state} onSubmit={onSubmit} onCancel={onCancel} onDelete={onDelete} hideIdentity={!state.avatarSource && !state.cardImageSource} />
    </div>
  </div>;
}
