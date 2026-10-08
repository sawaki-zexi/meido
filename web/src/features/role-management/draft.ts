import type { Role } from "../../api/types";

export const emptyDraft = { name: "", description: "", profile: "", personality: "", behaviorRules: "", responseConstraints: "", nickname: "", memoryRecallEnabled: false };
export type RoleDraft = typeof emptyDraft;

const storageKey = "meido:create-role-draft";
const fields = Object.keys(emptyDraft) as (keyof RoleDraft)[];

export const draftFromRole = (role: Role): RoleDraft => ({
  name: role.name,
  description: role.description,
  ...role.profile,
  memoryRecallEnabled: role.agentConfig?.memoryRecall?.enabled ?? false,
});

export const payloadFromDraft = (draft: RoleDraft) => ({
  name: draft.name,
  description: draft.description,
  profile: { profile: draft.profile, personality: draft.personality, behaviorRules: draft.behaviorRules, responseConstraints: draft.responseConstraints, nickname: draft.nickname },
  agentConfig: { memoryRecall: { enabled: draft.memoryRecallEnabled } },
});

/** Restores an unsaved create-role draft kept for this browser tab. */
export function readCreateDraft(): RoleDraft | null {
  try {
    const value = sessionStorage.getItem(storageKey);
    if (!value) return null;
    const parsed = JSON.parse(value) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const draft = fields.filter((field) => field !== "memoryRecallEnabled").reduce((draft, field) => {
      const candidate = parsed[field];
      draft[field] = typeof candidate === "string" ? candidate : "";
      return draft;
    }, { ...emptyDraft });
    draft.memoryRecallEnabled = parsed.memoryRecallEnabled === true;
    return draft;
  } catch {
    return null;
  }
}

export function writeCreateDraft(draft: RoleDraft): void {
  try { sessionStorage.setItem(storageKey, JSON.stringify(draft)); } catch { /* storage may be unavailable */ }
}

export function clearCreateDraft(): void {
  try { sessionStorage.removeItem(storageKey); } catch { /* storage may be unavailable */ }
}
