import { api } from './client';

export interface ProactiveSettings {
  revision: number;
  enabled: boolean;
  profile: 'daily' | 'quiet';
  agentMaxSteps: number;
  agentContentLimit: number;
  agentWebFetchMaxChars: number;
  driftMaxSteps: number;
  driftMinIntervalHours: number;
}
export interface ProactiveState {
  settings: ProactiveSettings;
  modelReady: boolean;
  cleanupPending: boolean;
  problem: string;
  activityNote?: string;
}
const path = (roleId: string) => `/api/roles/${encodeURIComponent(roleId)}/autonomy/settings`;
export const proactive = {
  get: (roleId: string) => api<ProactiveState>(path(roleId)),
  save: (roleId: string, { revision, ...settings }: ProactiveSettings) => api<ProactiveState>(path(roleId), {
    method: 'PUT', body: JSON.stringify({ ...settings, expectedRevision: revision }),
  }),
};
