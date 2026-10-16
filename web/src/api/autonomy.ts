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
export type AutonomyTargetAccount = {
  accountId: string; channel: string; displayName: string; enabled: boolean; platformEnabled: boolean; effectiveEnabled: boolean;
  connection: string; online: boolean | null; observedAt?: string | null; reason?: string; supportedKinds: TargetDirectoryKind[];
  capabilities: Record<string, unknown>;
};
export type TargetDirectoryKind = 'known' | 'friends' | 'groups' | 'members' | 'member';
export type TargetDirectoryQuery = { accountId: string; kind: TargetDirectoryKind; groupId?: string; memberId?: string; limit?: number; cursor?: string };
export type DirectoryRevision = { account: number; platform: number };
export type AutonomyTarget = { id: string; kind: string; displayName: string; accountId: string; chatId: string;
  capabilities: string[]; canSend?: boolean; targetRef: string; ownerEligible: boolean; reason: string;
  selectionObservationId: string | null };
export type AutonomyTargetDirectory = { targets: AutonomyTarget[]; nextCursor: string | null; coverage: 'known' | 'platform';
  complete: boolean; observedAt: string; total: number; directoryRevision: DirectoryRevision };
export type AutonomyTargetSelection = { accountId: string; targetId: string; selectionObservationId: string;
  expectedDirectoryRevision: DirectoryRevision };
export type AutonomyTargetChoice = { ref: string; displayName: string; selection: AutonomyTargetSelection };
export type AutonomyTargetGrant = { ref: string; channel: string; accountId?: string; targetId?: string; displayName: string;
  accountDisplayName?: string; current: boolean; denial: string | null; connection?: string; online?: boolean | null;
  observedAt?: string; reason?: string };
export type AutonomyDeliveryStatus = 'dispatching' | 'pending' | 'uncertain' | 'sent' | 'failed';
export type AutonomyResolutionDecision = 'reported_sent' | 'reported_not_sent' | 'keep_pending';
export type AutonomyDelivery = { intentId: string; status: AutonomyDeliveryStatus; messageId: string | null;
  target: { ref: string; channel: string; accountId?: string; targetId?: string; chatId?: string; displayName?: string };
  attemptId: string | null; platformMessageId: string | null; error: string; projectionPending: boolean; resolutionRevision: number;
  resolution: null | { requestId: string; decision: AutonomyResolutionDecision; allowFuture: boolean; note: string; reportedAt: number; revision: number } };
export type AutonomyReconciliationRequest = { requestId: string; expectedResolutionRevision: number; expectedStatus: AutonomyDeliveryStatus;
  decision: AutonomyResolutionDecision; allowFuture: boolean; note: string };
export type AutonomyState = { config: AutonomyConfiguration; nextDecisionAt?: number | null; cleanupPending: boolean; humanBusy: boolean; admissionError?: string;
  targetGrants?: AutonomyTargetGrant[]; deliveries?: AutonomyDelivery[];
  runs: { id: string; kind: string; mode?: string; status: string; runtimeAcknowledged: boolean; error: string;
    taskId?: string | null; timeoutSeconds?: number; delivery?: null | { status: string; messageId: string | null; intentId?: string } & Partial<AutonomyDelivery> }[];
  acknowledgements: { id: string; sourceId: string; status: string; attempts: number; expiresAt: number | null; permanent: boolean }[] };
export function normalizeAutonomyState(state: AutonomyState): AutonomyState {
  return { ...state, config: normalizeAutonomyConfiguration(state.config), targetGrants: state.targetGrants ?? [], deliveries: state.deliveries ?? [] };
}
const path = (roleId: string) => `/api/roles/${encodeURIComponent(roleId)}/autonomy`;
export class AutonomyOperationError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}
async function reconcile(roleId: string, intentId: string, request: AutonomyReconciliationRequest): Promise<AutonomyState> {
  let response: Response;
  try {
    response = await fetch(`${path(roleId)}/deliveries/${encodeURIComponent(intentId)}/reconciliation`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(request) });
  } catch { throw new AutonomyOperationError(503, '处置结果尚未确认，请核对原请求后重试'); }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new AutonomyOperationError(response.status, errorDetail(body, response.status));
  return normalizeAutonomyState(body);
}
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
  targetAccounts: (roleId: string) => api<{ accounts: AutonomyTargetAccount[] }>(`${path(roleId)}/target-accounts`),
  targetDirectory: (roleId: string, query: TargetDirectoryQuery) => api<AutonomyTargetDirectory>(`${path(roleId)}/target-directory`,
    { method: 'POST', body: JSON.stringify(query) }),
  reconcileDelivery: reconcile,
  driftGrantOptions: (roleId: string) => api<DriftGrantOptions>(`${path(roleId)}/drift-grant-options`),
  driftSkills: (roleId: string) => api<{ skills: DriftSkill[] }>(`${path(roleId)}/drift-skills`),
  previewDriftArtifact: (roleId: string, skillId: string) => api<DriftPreview>(`${path(roleId)}/drift-artifact-preview`,
    { method: 'POST', body: JSON.stringify({ skillId }) }),
  adoptDriftSkill: adopt,
};
