import type { AgentToolConfig, Role } from "../../api/types";

export const emptyDraft = { name: "", description: "", profile: "", personality: "", behaviorRules: "", responseConstraints: "", nickname: "", memoryRecallEnabled: false, todoSearchEnabled: false, browserUseEnabled: false, screenPerceptionEnabled: false, screenVisionModelConfigurationId: "", spawnEnabled: false, spawnTimeoutSeconds: 120, spawnMaxTurns: 12, spawnMaxOutputChars: 20000, spawnMaxModelOutputChars: 60000, shellEnabled: false, shellAllowedCommands: "", shellTimeoutSeconds: 30, shellMaxOutputChars: 20000, preservedAgentConfig: {} as AgentToolConfig, otherEnabledTools: [] as string[], otherEnabledPlugins: [] as string[], otherGrantedCapabilities: [] as string[] };
export type RoleDraft = typeof emptyDraft;

const storageKey = "meido:create-role-draft";

export const draftFromRole = (role: Role): RoleDraft => ({
  name: role.name,
  description: role.description,
  ...role.profile,
  memoryRecallEnabled: role.agentConfig?.memoryRecall?.enabled ?? false,
  todoSearchEnabled: role.agentConfig?.enabledPlugins?.includes("todo") === true && role.agentConfig?.grantedCapabilities?.includes("todo.read") === true && role.agentConfig?.enabledTools?.includes("todo.search") === true,
  browserUseEnabled: role.agentConfig?.browserUse?.enabled === true,
  spawnEnabled: role.agentConfig?.spawn?.enabled === true,
  spawnTimeoutSeconds: role.agentConfig?.spawn?.timeoutSeconds ?? 120,
  spawnMaxTurns: role.agentConfig?.spawn?.maxTurns ?? 12,
  spawnMaxOutputChars: role.agentConfig?.spawn?.maxOutputChars ?? 20000,
  spawnMaxModelOutputChars: role.agentConfig?.spawn?.maxModelOutputChars ?? 60000,
  screenPerceptionEnabled: role.agentConfig?.screenPerception?.enabled === true,
  screenVisionModelConfigurationId: role.agentConfig?.screenPerception?.visionModelConfigurationId ?? "",
  shellEnabled: role.agentConfig?.shell?.enabled === true,
  shellAllowedCommands: (role.agentConfig?.shell?.allowedCommands ?? []).join("\n"),
  shellTimeoutSeconds: role.agentConfig?.shell?.timeoutSeconds ?? 30,
  shellMaxOutputChars: role.agentConfig?.shell?.maxOutputChars ?? 20000,
  preservedAgentConfig: structuredClone(role.agentConfig ?? {}),
  otherEnabledTools: (role.agentConfig?.enabledTools ?? []).filter((item) => item !== "todo.search"),
  otherEnabledPlugins: (role.agentConfig?.enabledPlugins ?? []).filter((item) => item !== "todo"),
  otherGrantedCapabilities: (role.agentConfig?.grantedCapabilities ?? []).filter((item) => item !== "todo.read"),
});

export const payloadFromDraft = (draft: RoleDraft) => ({
  name: draft.name,
  description: draft.description,
  profile: { profile: draft.profile, personality: draft.personality, behaviorRules: draft.behaviorRules, responseConstraints: draft.responseConstraints, nickname: draft.nickname },
  agentConfig: {
    ...draft.preservedAgentConfig,
    ...(draft.spawnEnabled || draft.preservedAgentConfig.spawn ? { spawn: {
      ...draft.preservedAgentConfig.spawn, enabled: draft.spawnEnabled,
      timeoutSeconds: draft.spawnTimeoutSeconds, maxTurns: draft.spawnMaxTurns,
      maxOutputChars: draft.spawnMaxOutputChars, maxModelOutputChars: draft.spawnMaxModelOutputChars,
    } } : {}),
    memoryRecall: { ...draft.preservedAgentConfig.memoryRecall, enabled: draft.memoryRecallEnabled },
    ...(draft.browserUseEnabled || draft.preservedAgentConfig.browserUse ? { browserUse: { ...draft.preservedAgentConfig.browserUse, enabled: draft.browserUseEnabled } } : {}),
    ...(draft.screenPerceptionEnabled || draft.screenVisionModelConfigurationId || draft.preservedAgentConfig.screenPerception ? { screenPerception: {
      ...draft.preservedAgentConfig.screenPerception, enabled: draft.screenPerceptionEnabled,
      visionModelConfigurationId: draft.screenVisionModelConfigurationId || null,
    } } : {}),
    ...(draft.shellEnabled || draft.preservedAgentConfig.shell || draft.shellAllowedCommands ? { shell: {
      ...draft.preservedAgentConfig.shell, enabled: draft.shellEnabled,
      allowedCommands: [...new Set(draft.shellAllowedCommands.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))],
      timeoutSeconds: draft.shellTimeoutSeconds, maxOutputChars: draft.shellMaxOutputChars,
    } } : {}),
    enabledPlugins: todoSetting(draft, "enabledPlugins", "todo", draft.otherEnabledPlugins),
    grantedCapabilities: todoSetting(draft, "grantedCapabilities", "todo.read", draft.otherGrantedCapabilities),
    enabledTools: todoSetting(draft, "enabledTools", "todo.search", draft.otherEnabledTools),
  },
});

function todoSetting(draft: RoleDraft, field: "enabledPlugins" | "grantedCapabilities" | "enabledTools", id: string, other: string[]): string[] {
  const previous = draft.preservedAgentConfig;
  const wasEnabled = previous.enabledPlugins?.includes("todo") === true && previous.grantedCapabilities?.includes("todo.read") === true && previous.enabledTools?.includes("todo.search") === true;
  const retained = draft.todoSearchEnabled || (draft.todoSearchEnabled === wasEnabled && previous[field]?.includes(id));
  return [...other, ...(retained ? [id] : [])];
}

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
    draft.browserUseEnabled = parsed.browserUseEnabled === true;
    draft.spawnEnabled = parsed.spawnEnabled === true;
    for (const key of ["spawnTimeoutSeconds", "spawnMaxTurns", "spawnMaxOutputChars", "spawnMaxModelOutputChars"] as const) {
      if (typeof parsed[key] === "number" && Number.isFinite(parsed[key])) draft[key] = parsed[key] as number;
    }
    draft.shellEnabled = parsed.shellEnabled === true;
    draft.shellAllowedCommands = readText("shellAllowedCommands");
    if (typeof parsed.shellTimeoutSeconds === "number" && Number.isFinite(parsed.shellTimeoutSeconds)) draft.shellTimeoutSeconds = parsed.shellTimeoutSeconds;
    if (typeof parsed.shellMaxOutputChars === "number" && Number.isFinite(parsed.shellMaxOutputChars)) draft.shellMaxOutputChars = parsed.shellMaxOutputChars;
    if (parsed.preservedAgentConfig && typeof parsed.preservedAgentConfig === "object" && !Array.isArray(parsed.preservedAgentConfig)) draft.preservedAgentConfig = parsed.preservedAgentConfig as AgentToolConfig;
    draft.screenPerceptionEnabled = typeof parsed.screenPerceptionEnabled === "boolean" ? parsed.screenPerceptionEnabled : draft.preservedAgentConfig.screenPerception?.enabled === true;
    draft.screenVisionModelConfigurationId = typeof parsed.screenVisionModelConfigurationId === "string" ? parsed.screenVisionModelConfigurationId : typeof draft.preservedAgentConfig.screenPerception?.visionModelConfigurationId === "string" ? draft.preservedAgentConfig.screenPerception.visionModelConfigurationId : "";
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
