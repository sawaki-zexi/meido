import { api, errorDetail } from './client';

export type DriftAuthoring = { enabled: boolean; scopeId: string; revision: number; allowedTools: string[];
  allowMcp: boolean; allowShell: boolean; allowAutonomousAuthoring: boolean; allowedResourceIds: string[] };
export type DriftMcpGrant = { serverId: string; configRevision: string; grantRevision: string;
  risk: 'read_only' | 'mutating' | 'external'; operations: { name: string; definitionHash: string }[] };
export type AutonomyConfiguration = {
  revision: number; activeDecisionEnabled: boolean; driftEnabled: boolean;
  decisionModelConfigurationId: string | null; driftModelConfigurationId: string | null;
  defaultTargetRef: string; allowedTargetRefs: string[]; sourceGrants: Record<string, unknown>[];
  driftToolGrants: string[]; shellRuleIds: string[]; consentRevision: number;
  driftAuthoring: DriftAuthoring; driftMcpGrants: DriftMcpGrant[]; driftReadResourceIds: string[]; driftAttachmentIds: string[];
};
export const defaultDriftAuthoring: DriftAuthoring = { enabled: false, scopeId: '', revision: 0,
  allowedTools: [], allowMcp: false, allowShell: false, allowAutonomousAuthoring: false, allowedResourceIds: [] };
export function normalizeAutonomyConfiguration(config: AutonomyConfiguration): AutonomyConfiguration {
  return { ...config, driftAuthoring: { ...defaultDriftAuthoring, ...config.driftAuthoring },
    driftMcpGrants: config.driftMcpGrants ?? [], driftReadResourceIds: config.driftReadResourceIds ?? [],
    driftAttachmentIds: config.driftAttachmentIds ?? [] };
}
export type DriftGrantOptions = {
  tools: { name: string; available: boolean; reason?: string }[];
  resources: { id: string; name: string; available: boolean; reason?: string }[];
  shellRules: { id: string; description: string; available: boolean; reason?: string }[];
  mcpServers: { serverId: string; name: string; configRevision: string; grantRevision: string;
    risk: DriftMcpGrant['risk']; status: 'observed' | 'unobserved' | 'stale'; reason?: string;
    operations: { name: string; definitionHash: string; description: string }[] }[];
};
export type DriftSkill = { id: string; version?: string; description?: string; bundleSha256?: string;
  tools?: string[]; requiresMCP?: { serverId: string; tools: { name: string; definitionHash?: string | null }[] }[];
  status: 'untrusted' | 'invalid' | 'pending_dependency' | 'available'; missingTools?: string[];
  missingMcp?: string[]; missingResources?: string[]; missingShellRules?: string[]; reason?: string;
  provenance?: { status: 'verified' | 'unverified'; runId: string | null; authorSkillHash: string | null };
  progress?: null | { runCount: number; lastRun: number; oneLine: string; nextAction: string;
    messageResult: 'sent' | 'silent' | 'failed' | 'uncertain'; projectionPending: boolean };
};
export type DriftPreview = { previewId: string; skillId: string; bundleSha256: string;
  files: { path: string; size: number; sha256: string }[]; declaration: Record<string, unknown>;
  scopeId: string; scopeRevision: number; configRevision: number; status: DriftSkill['status']; reason?: string };
const path = (roleId: string) => `/api/roles/${encodeURIComponent(roleId)}/autonomy`;
export class DriftAdoptionError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}
// Adoption needs the HTTP status to keep a 503 request identity and invalidate a 409 preview.
async function adopt(roleId: string, preview: DriftPreview): Promise<{ skills: DriftSkill[] }> {
  let response: Response;
  try {
    response = await fetch(`${path(roleId)}/drift-skill-adoptions`, { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ previewId: preview.previewId, skillId: preview.skillId, bundleSha256: preview.bundleSha256,
        authoringScopeId: preview.scopeId, expectedScopeRevision: preview.scopeRevision, expectedRevision: preview.configRevision }) });
  } catch { throw new Error('无法连接本地后端，采用结果尚未确认；请核对原产物状态'); }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new DriftAdoptionError(response.status, errorDetail(body, response.status));
  return body;
}
export const autonomy = {
  driftGrantOptions: (roleId: string) => api<DriftGrantOptions>(`${path(roleId)}/drift-grant-options`),
  driftSkills: (roleId: string) => api<{ skills: DriftSkill[] }>(`${path(roleId)}/drift-skills`),
  previewDriftArtifact: (roleId: string, skillId: string) => api<DriftPreview>(`${path(roleId)}/drift-artifact-preview`,
    { method: 'POST', body: JSON.stringify({ skillId }) }),
  adoptDriftSkill: adopt,
};
