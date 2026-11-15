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
  categoryEnabled?: boolean;
  imageUrl: string;
  assetId?: string;
  semanticRevision?: number;
  meaningScope?: 'role' | 'conversation';
  semanticStatus?: 'ready' | 'pending' | 'content_changed';
  basis?: { kind: 'owner_confirmed' | 'model_observation' | 'legacy_description' | 'unverified' };
  sources?: { kind: string; scope: string }[];
  history?: { revision: number; description: string; name: string }[];
};
export type MaterialLibrary = {
  materials: RoleMaterial[];
  categories: { name: string; description: string; count: number; enabled: boolean }[];
  revision: string;
  storageLabel: string;
  total?: number;
  libraryTotal?: number;
  nextOffset?: number | null;
};
const path = (roleId: string) => `/api/roles/${encodeURIComponent(roleId)}/materials`;
export type CollectionResult = {
  material: RoleMaterial; deduplicated: boolean; revision: string;
  descriptionOutcome?: 'pending' | 'saved' | 'unchanged' | 'filled' | 'proposal_retained';
  effectiveSemanticStatus?: 'ready' | 'pending' | 'content_changed';
};
export const materials = {
  list: (roleId: string, signal?: AbortSignal, options: { query?: string; category?: string; offset?: number } = {}) => {
    const params = new URLSearchParams();
    if (options.query) params.set('query', options.query);
    if (options.category) params.set('category', options.category);
    if (options.offset) params.set('offset', String(options.offset));
    return api<MaterialLibrary>(path(roleId) + (params.size ? `?${params}` : ''), { signal });
  },
  import: (roleId: string, file: File, category: string, description: string, expectedRevision: string) =>
    api<CollectionResult>(`${path(roleId)}?${new URLSearchParams({ category, description, expectedRevision })}`, {
      method: 'POST', body: file, headers: { 'content-type': file.type || ({ png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', gif: 'image/gif' }[file.name.split('.').pop()?.toLowerCase() ?? ''] ?? 'application/octet-stream'), 'x-material-filename': encodeURIComponent(file.name) },
    }),
  remove: (roleId: string, id: string, expectedRevision: string) =>
    api<{ removed: true; recoverable: true; revision: string }>(`${path(roleId)}/${encodeURIComponent(id)}?${new URLSearchParams({ expectedRevision })}`, { method: 'DELETE' }),
  edit: (roleId: string, id: string, expectedRevision: string, changes: { name?: string; description?: string; enabled?: boolean }) =>
    api<{ material: RoleMaterial; revision: string }>(`${path(roleId)}/${encodeURIComponent(id)}`, {
      method: 'PATCH', body: JSON.stringify({ expectedRevision, ...changes }),
    }),
  saveMessage: (roleId: string, messageId: string, attachmentId: string, description: string) =>
    api<CollectionResult>(`${path(roleId)}/from-message`, {
      method: 'POST', body: JSON.stringify({ messageId, attachmentId, description }),
    }),
};
