import { api } from './client';

export type SkillPolicy = 'automatic' | 'manual' | 'constant';
export type OrdinarySkill = {
  id: string; name: string; description: string; source: string; ref: string; scope: string;
  contentHash: string; enabled: boolean; effective: boolean; editable: boolean;
  status: 'available' | 'unavailable' | 'disabled' | 'shadowed'; policy: SkillPolicy; legacyPolicy: string;
  problems: string[]; ignoredMetadata: string[]; supportingFiles: { path: string; size: number }[];
};
export type SkillCatalog = { enabled: boolean; revision: number; skills: OrdinarySkill[];
  diagnostics: { skillId?: string; error: string; source?: string }[];
  migration: { ref: string; name: string; from: string; to: SkillPolicy }[] };
export type SkillDocument = { ref: string; content: string; contentHash: string; source: string; ignoredMetadata: string[] };
export type SkillResource = { path: string; content: string; encoding: 'utf-8' | 'base64' };
const path = (roleId: string) => `/api/roles/${encodeURIComponent(roleId)}/skills`;
export const skillApi = {
  list: (roleId: string) => api<SkillCatalog>(path(roleId)),
  document: (roleId: string, ref: string) => api<SkillDocument>(`${path(roleId)}/document?ref=${encodeURIComponent(ref)}`),
  save: (roleId: string, content: string, document?: SkillDocument, resources: SkillResource[] = []) =>
    api<SkillDocument & { saved: boolean; verified: boolean; problems: string[] }>(path(roleId), {
      method: 'POST', body: JSON.stringify({ content, ref: document?.ref, expectedHash: document?.contentHash,
        resources: Object.fromEntries(resources.map(resource => [resource.path, { content: resource.content, encoding: resource.encoding }])) }),
    }),
  settings: (roleId: string, expectedRevision: number, values: { enabled?: boolean; ref?: string; active?: boolean; policy?: SkillPolicy }) =>
    api(`${path(roleId)}/settings`, { method: 'PUT', body: JSON.stringify({ expectedRevision, ...values }) }),
  remove: (roleId: string, ref: string, expectedHash: string) => api(`${path(roleId)}/remove`, {
    method: 'POST', body: JSON.stringify({ ref, expectedHash }),
  }),
  publish: (roleId: string, ref: string, expectedHash: string) => api(`${path(roleId)}/publish`, {
    method: 'POST', body: JSON.stringify({ ref, expectedHash }),
  }),
};
