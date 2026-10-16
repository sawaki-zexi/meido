import { api } from './client';
import type { ChatAttachment } from './types';

export type NovelaiRoleConfig = {
  manualEnabled: boolean; manualDailyAttempts: number; sceneCgEnabled: boolean; sceneCgDailyAttempts: number;
  scenePromptModelConfigurationId: string | null; autoWritebackRoleAssets: boolean; consentRevision: number;
};
export const defaultNovelaiRoleConfig: NovelaiRoleConfig = {
  manualEnabled: false, manualDailyAttempts: 10, sceneCgEnabled: false, sceneCgDailyAttempts: 3,
  scenePromptModelConfigurationId: null, autoWritebackRoleAssets: false, consentRevision: 0,
};
export type NovelaiConfiguration = {
  enabled: boolean; tokenConfigured: boolean; credentialCleanupPending: boolean; revision: number;
  allowedModels: string[]; defaultModel: string; allowedSamplers: string[]; defaultSampler: string;
  maxPixels: number; maxSteps: number; dailyAttemptLimit: number;
};
export type GenerationParameters = {
  prompt: string; mode: 'txt2img' | 'img2img'; negative_prompt?: string; base_attachment_id?: string;
  size_preset?: 'square' | 'landscape' | 'portrait' | 'custom'; custom_width?: number; custom_height?: number;
  model?: string; sampler?: string; steps?: number; seed?: number; strength?: number; noise?: number; background?: boolean;
};
export type GenerationView = {
  generationId: string; attemptId: string; roleId: string; status: string; parameters: GenerationParameters & { width: number; height: number; seed: number };
  attachments: ChatAttachment[]; taskId: string | null; createdAt: string; errorCode: string; consumption: string; deliveryStatus: string;
  parentAttemptId: string | null; deliveryMode: 'foreground_reply' | 'source_completion' | 'replacement' | 'history_only';
  deliveryMessageId: string | null; expectedAttachmentId: string | null;
};
export type GenerationPage = { generations: GenerationView[]; nextCursor: string | null; usage: { day: string; accountAttempts: number; manualAttempts: number } };
export type GenerationSelection = { generation: GenerationView; image: ChatAttachment };
export const novelai = {
  configuration: () => api<NovelaiConfiguration>('/api/services/novelai'),
  saveConfiguration: (configuration: Omit<NovelaiConfiguration, 'revision' | 'tokenConfigured' | 'credentialCleanupPending'>, expectedRevision: number) =>
    api<NovelaiConfiguration>('/api/services/novelai', { method: 'PUT', body: JSON.stringify({ ...configuration, expectedRevision }) }),
  setToken: (token: string) => api<NovelaiConfiguration>('/api/services/novelai/token', { method: 'PUT', body: JSON.stringify({ token }) }),
  clearToken: () => api<NovelaiConfiguration>('/api/services/novelai/token', { method: 'DELETE' }),
  history: (roleId: string, cursor?: string) => api<GenerationPage>(`/api/roles/${encodeURIComponent(roleId)}/generations${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
  forAttachment: (roleId: string, attachmentId: string, messageId: string, signal?: AbortSignal) =>
    api<{ generation: GenerationView }>(`/api/roles/${encodeURIComponent(roleId)}/generations/attachment/${encodeURIComponent(attachmentId)}?messageId=${encodeURIComponent(messageId)}`, { signal }),
  generate: (roleId: string, requestId: string, expectedRevision: number, parameters: GenerationParameters, signal?: AbortSignal,
    deliveryMode?: 'history_only') =>
    api<{ generation: GenerationView }>(`/api/roles/${encodeURIComponent(roleId)}/generations`, { method: 'POST', body: JSON.stringify({ requestId, expectedRevision, parameters, deliveryMode }), signal }),
  regenerate: (roleId: string, generationId: string, requestId: string, expectedRevision: number, sourceAttemptId: string,
    expectedAttachmentId: string, parameters: GenerationParameters, signal?: AbortSignal, deliveryMode: 'replacement' | 'history_only' = 'replacement') =>
    api<{ generation: GenerationView }>(`/api/roles/${encodeURIComponent(roleId)}/generations/${encodeURIComponent(generationId)}/regenerate`,
      { method: 'POST', body: JSON.stringify({ requestId, expectedRevision, sourceAttemptId, expectedAttachmentId, parameters, deliveryMode }), signal }),
  get: (roleId: string, generationId: string, attemptId: string) =>
    api<{ generation: GenerationView }>(`/api/roles/${encodeURIComponent(roleId)}/generations/${encodeURIComponent(generationId)}?attemptId=${encodeURIComponent(attemptId)}`),
  cancel: (roleId: string, generationId: string, attemptId: string) =>
    api<{ generation: GenerationView }>(`/api/roles/${encodeURIComponent(roleId)}/generations/${encodeURIComponent(generationId)}/cancel`,
      { method: 'POST', body: JSON.stringify({ attemptId }) }),
};
