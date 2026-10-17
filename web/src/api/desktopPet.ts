import { api } from './client';
export type PetPackage = { id: string; displayName: string; description: string; actions: Record<string, string> };
export type PetState = { revision: number; enabled: boolean; selectedPackageId: string | null;
  packages: PetPackage[]; unavailableReason: string; host: { status: string; reason: string };
  history: { type: string; actionId?: string; reason?: string }[] };
const path = (roleId: string) => `/api/roles/${encodeURIComponent(roleId)}/desktop-pet`;
export const desktopPet = {
  status: (roleId: string) => api<PetState>(path(roleId)),
  import: (roleId: string, file: File) => api<PetState>(`${path(roleId)}/packages`,
    { method: 'POST', headers: { 'content-type': 'application/zip' }, body: file }),
  configure: (roleId: string, state: PetState, packageId: string | null, enabled: boolean) => api<PetState>(path(roleId),
    { method: 'PUT', body: JSON.stringify({ expectedRevision: state.revision, packageId, enabled }) }),
  restart: (roleId: string) => api<PetState>(`${path(roleId)}/restart`, { method: 'POST' }),
  remove: (roleId: string, state: PetState, id: string) => api<PetState>(
    `${path(roleId)}/packages/${encodeURIComponent(id)}?expectedRevision=${state.revision}`, { method: 'DELETE' }),
  preview: (roleId: string, id: string) => `${path(roleId)}/packages/${encodeURIComponent(id)}/assets/preview`,
};
