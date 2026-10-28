import { api } from './client';
import type { Role } from './types';
const path = (roleId: string) => `/api/roles/${encodeURIComponent(roleId)}`;
export const roleAssets = {
  list: (roleId: string, signal?: AbortSignal) => api<{ role: Role }>(`${path(roleId)}/illustrations`, { signal }),
  upload: (roleId: string, file: File, category: string) => api<{ role: Role }>(`${path(roleId)}/illustrations?${new URLSearchParams({ name: file.name, category })}`,
    { method: 'POST', body: file, headers: { 'content-type': file.type || ({ png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp' }[file.name.split('.').pop()?.toLowerCase() ?? ''] ?? 'application/octet-stream') } }),
  importAttachment: (roleId: string, attachmentId: string, setBackground = false) => api<{ role: Role }>(`${path(roleId)}/illustrations/from-attachment`,
    { method: 'POST', body: JSON.stringify({ attachmentId, setBackground }) }),
  background: (roleId: string, illustrationId: string | null) => api<{ role: Role }>(`${path(roleId)}/chat-background`,
    { method: 'PUT', body: JSON.stringify({ illustrationId }) }),
  avatar: (roleId: string, illustrationId: string) => api<{ role: Role }>(`${path(roleId)}/avatar-from-illustration`,
    { method: 'PUT', body: JSON.stringify({ illustrationId }) }),
  remove: (roleId: string, id: string) => api<{ role: Role }>(`${path(roleId)}/illustrations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  update: (roleId: string, changes: { categories?: string[]; bindings?: Record<string, string>; categorySend?: Record<string, boolean>; moodBindings?: Record<string, string>; defaultMood?: string }) => api<{ role: Role }>(`${path(roleId)}/illustrations`,
    { method: 'PATCH', body: JSON.stringify(changes) }),
};
