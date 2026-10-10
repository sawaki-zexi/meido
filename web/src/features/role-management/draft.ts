import type { Role } from "../../api/types";

export const emptyDraft = { name: "", description: "", profile: "", personality: "", behaviorRules: "", responseConstraints: "", nickname: "", memoryRecallEnabled: false, todoSearchEnabled: false, otherEnabledTools: [] as string[], otherEnabledPlugins: [] as string[], otherGrantedCapabilities: [] as string[] };
export type RoleDraft = typeof emptyDraft;

const storageKey = "meido:create-role-draft";

export const draftFromRole = (role: Role): RoleDraft => ({
  name: role.name,
  description: role.description,
  ...role.profile,
  memoryRecallEnabled: role.agentConfig?.memoryRecall?.enabled ?? false,
  todoSearchEnabled: role.agentConfig?.enabledPlugins?.includes("todo") === true && role.agentConfig?.grantedCapabilities?.includes("todo.read") === true && role.agentConfig?.enabledTools?.includes("todo.search") === true,
  otherEnabledTools: (role.agentConfig?.enabledTools ?? []).filter((item) => item !== "todo.search"),
  otherEnabledPlugins: (role.agentConfig?.enabledPlugins ?? []).filter((item) => item !== "todo"),
  otherGrantedCapabilities: (role.agentConfig?.grantedCapabilities ?? []).filter((item) => item !== "todo.read"),
});

export const payloadFromDraft = (draft: RoleDraft) => ({
  name: draft.name,
  description: draft.description,
  profile: { profile: draft.profile, personality: draft.personality, behaviorRules: draft.behaviorRules, responseConstraints: draft.responseConstraints, nickname: draft.nickname },
  agentConfig: {
    memoryRecall: { enabled: draft.memoryRecallEnabled },
    enabledPlugins: [...draft.otherEnabledPlugins, ...(draft.todoSearchEnabled ? ["todo"] : [])],
    grantedCapabilities: [...draft.otherGrantedCapabilities, ...(draft.todoSearchEnabled ? ["todo.read"] : [])],
    enabledTools: [...draft.otherEnabledTools, ...(draft.todoSearchEnabled ? ["todo.search"] : [])],
  },
});

/** Restores an unsaved create-role draft kept for this browser tab. */
export function readCreateDraft(): RoleDraft | null {
  try {
    const value = sessionStorage.getItem(storageKey);
    if (!value) return null;
    const parsed = JSON.parse(value) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const readText = (field: keyof RoleDraft) => typeof parsed[field] === "string" ? parsed[field] as string : "";
    const draft: RoleDraft = {
      ...emptyDraft,
      name: readText("name"), description: readText("description"), profile: readText("profile"),
      personality: readText("personality"), behaviorRules: readText("behaviorRules"),
      responseConstraints: readText("responseConstraints"), nickname: readText("nickname"),
    };
    draft.memoryRecallEnabled = parsed.memoryRecallEnabled === true;
    draft.todoSearchEnabled = parsed.todoSearchEnabled === true;
    draft.otherEnabledTools = Array.isArray(parsed.otherEnabledTools) ? parsed.otherEnabledTools.filter((item): item is string => typeof item === "string" && item !== "todo.search") : [];
    draft.otherEnabledPlugins = Array.isArray(parsed.otherEnabledPlugins) ? parsed.otherEnabledPlugins.filter((item): item is string => typeof item === "string" && item !== "todo") : [];
    draft.otherGrantedCapabilities = Array.isArray(parsed.otherGrantedCapabilities) ? parsed.otherGrantedCapabilities.filter((item): item is string => typeof item === "string" && item !== "todo.read") : [];
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
