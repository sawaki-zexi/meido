import { api } from './client';

export type HostPluginId = 'browser' | 'screen' | 'computer' | 'pet' | 'autonomy';
export type PluginId = HostPluginId | 'novelai' | 'coding' | 'todo';
export type PluginState = { id: HostPluginId; name: string; description: string; enabled: boolean;
  revision: number; cleanupPending: boolean; values: Record<string, unknown> };
export const plugins = {
  list: () => api<{ plugins: PluginState[] }>('/api/plugin-management'),
  enabled: (plugin: PluginState, enabled: boolean) => api<PluginState>(
    `/api/plugin-management/${plugin.id}/enabled`, { method: 'PUT', body: JSON.stringify({ enabled, expectedRevision: plugin.revision }) }),
  configure: (plugin: PluginState, values: Record<string, unknown>) => api<PluginState>(
    `/api/plugin-management/${plugin.id}/configuration`, { method: 'PUT', body: JSON.stringify({ values, expectedRevision: plugin.revision }) }),
};
