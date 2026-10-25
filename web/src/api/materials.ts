import { api } from './client';

export type RoleMaterial = {
  id: string;
  name: string;
  category: string;
  description: string;
  aliases: string[];
  format: 'PNG' | 'JPEG' | 'WEBP' | 'GIF';
  mediaType: string;
  size: number;
  width: number;
  height: number;
  animated: boolean;
  source: 'owner_import' | 'chat' | 'drift' | 'legacy';
  enabled: boolean;
  imageUrl: string;
};
export type MaterialLibrary = {
  materials: RoleMaterial[];
  categories: { name: string; description: string; count: number; enabled: boolean }[];
  revision: string;
  storageLabel: string;
};
const path = (roleId: string) => `/api/roles/${encodeURIComponent(roleId)}/materials`;
export const materials = {
  list: (roleId: string, signal?: AbortSignal) => api<MaterialLibrary>(path(roleId), { signal }),
  import: (roleId: string, file: File, category: string, description: string, expectedRevision: string) =>
    api<{ material: RoleMaterial; deduplicated: boolean; revision: string }>(`${path(roleId)}?${new URLSearchParams({ category, description, expectedRevision })}`, {
      method: 'POST', body: file, headers: { 'content-type': file.type || ({ png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', gif: 'image/gif' }[file.name.split('.').pop()?.toLowerCase() ?? ''] ?? 'application/octet-stream'), 'x-material-filename': encodeURIComponent(file.name) },
    }),
  remove: (roleId: string, id: string, expectedRevision: string) =>
    api<{ removed: true; recoverable: true; revision: string }>(`${path(roleId)}/${encodeURIComponent(id)}?${new URLSearchParams({ expectedRevision })}`, { method: 'DELETE' }),
};
