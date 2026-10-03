import type { Role } from "../../api/types";
import { payloadFromDraft } from "../role-management/draft";
import { RoleEditor } from "../role-management/RoleEditor";
import type { RolesState } from "../role-management/useRoles";
import { RoleCard } from "./RoleCard";

type Props = { state: RolesState; number: number; onSubmit: () => void; onCancel: () => void; onDelete?: () => void };

/**
 * Role form beside a live preview of her card, used both for creating and for an existing role's profile.
 * A new role's own hue and motif are revealed once she is saved.
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
  return <div className="create-layout">
    <aside className="create-preview" aria-label="卡牌预览">
      <RoleCard role={preview} number={number} className={existing ? undefined : "is-draft"} />
    </aside>
    <RoleEditor state={state} onSubmit={onSubmit} onCancel={onCancel} onDelete={onDelete} />
  </div>;
}
