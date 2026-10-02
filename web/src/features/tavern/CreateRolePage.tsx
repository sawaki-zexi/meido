import type { Role } from "../../api/types";
import { payloadFromDraft } from "../role-management/draft";
import { RoleEditor } from "../role-management/RoleEditor";
import type { RolesState } from "../role-management/useRoles";
import { RoleCard } from "./RoleCard";

type Props = { state: RolesState; number: number; onSubmit: () => void; onCancel: () => void };

/** Role form beside a live preview of her card. The card's own hue and motif are revealed once she is saved. */
export function CreateRolePage({ state, number, onSubmit, onCancel }: Props) {
  const { name, description, profile } = payloadFromDraft(state.draft);
  const preview: Role = { id: "preview", name: name.trim() || "？", description, profile, createdAt: "", updatedAt: "" };
  return <div className="create-layout">
    <aside className="create-preview" aria-label="卡牌预览">
      <RoleCard role={preview} number={number} className="is-draft" />
      <p className="muted">保存后，她会得到专属的卡面。</p>
    </aside>
    <RoleEditor state={state} onSubmit={onSubmit} onCancel={onCancel} onDelete={() => undefined} />
  </div>;
}
