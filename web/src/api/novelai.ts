import { api } from './client';
import type { ChatAttachment } from './types';

export type NovelaiRoleConfig = {
  manualEnabled: boolean; manualDailyAttempts: number; sceneCgEnabled: boolean; sceneCgDailyAttempts: number;
  scenePromptModelConfigurationId: string | null; sceneCgPromptInstructions: string;
  autoWritebackRoleAssets: boolean; consentRevision: number;
  promptTagsEnabled: boolean; allowedPromptTagRatings: PromptTagRating[];
};
export const defaultNovelaiRoleConfig: NovelaiRoleConfig = {
  manualEnabled: false, manualDailyAttempts: 10, sceneCgEnabled: false, sceneCgDailyAttempts: 3,
  scenePromptModelConfigurationId: null, sceneCgPromptInstructions: '', autoWritebackRoleAssets: false, consentRevision: 0,
  promptTagsEnabled: false, allowedPromptTagRatings: ['general'],
};
export type NovelaiAvailability = { available: boolean; code: string; message: string };
export type NovelaiConfiguration = {
  availability?: NovelaiAvailability;
  enabled: boolean; tokenConfigured: boolean; credentialCleanupPending: boolean; revision: number;
  allowTxt2Img: boolean; allowImg2Img: boolean;
  allowedModels: string[]; defaultModel: string; nsfwEnabled: boolean; nsfwModel: string; allowedSamplers: string[]; defaultSampler: string;
  maxPixels: number; maxSteps: number; dailyAttemptLimit: number; defaultSamples: number; addQualityTags: boolean; undesiredContentPreset: number;
  manualEnabled?: boolean; manualDailyAttempts?: number; manualConsentRevision?: number;
};
export type GenerationParameters = {
  prompt: string; mode: 'txt2img' | 'img2img'; negative_prompt?: string; base_attachment_id?: string;
  size_preset?: 'square' | 'landscape' | 'portrait' | 'custom'; custom_width?: number; custom_height?: number;
  model?: string; sampler?: string; steps?: number; seed?: number; samples?: number; strength?: number; noise?: number; background?: boolean;
};
export type GenerationView = {
  generationId: string; attemptId: string; roleId: string; status: string; parameters: (GenerationParameters & {
    width: number; height: number; seed: number; samples?: number; qualityToggle?: boolean; ucPreset?: number;
  }) | null;
  purpose?: 'manual' | 'chat_expression' | 'scene_cg'; phase?: 'auxiliary' | 'generation' | 'completed';
  auxiliaryConsumption?: 'possible' | 'not_dispatched'; auxiliaryStatus?: 'not_dispatched' | 'dispatching' | 'succeeded' | 'failed';
  writebackStatus?: 'not_requested' | 'writing' | 'uncertain' | 'applied' | 'failed';
  attachments: ChatAttachment[]; taskId: string | null; createdAt: string; errorCode: string; errorMessage?: string; consumption: string; deliveryStatus: string;
  parentAttemptId: string | null; deliveryMode: 'foreground_reply' | 'source_completion' | 'replacement' | 'history_only';
  deliveryMessageId: string | null; expectedAttachmentId: string | null;
};
export type GenerationPage = { generations: GenerationView[]; nextCursor: string | null; usage: { day: string; accountAttempts: number; manualAttempts: number; cgAttempts?: number; roleAssetBytes?: number } };
export type GenerationSelection = { generation: GenerationView; image: ChatAttachment };
export type PromptTagRating = 'general' | 'mature' | 'explicit';
export type PromptTagInput = { name: string; category: string; keywords: string[]; prompt: string; negative_prompt: string; rating: PromptTagRating; enabled: boolean; reference_attachment_id: string | null };
export type PromptTag = PromptTagInput & { id: string; referenceAttachment: ChatAttachment | null };
export type PromptTagPage = { tags: PromptTag[]; revision: number; nextCursor: string | null };
export type PromptTagPreview = { prompt: string; negative_prompt: string; matchedIds: string[]; revision: number };
const tagsPath = (roleId: string) => `/api/roles/${encodeURIComponent(roleId)}/novelai/prompt-tags`;
export const novelai = {
  promptTags: (roleId: string, cursor?: string) => api<PromptTagPage>(`${tagsPath(roleId)}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
  savePromptTag: (roleId: string, tag: PromptTagInput, id?: string) => api<{ tag: PromptTag; revision: number }>(`${tagsPath(roleId)}${id ? `/${encodeURIComponent(id)}` : ''}`,
    { method: id ? 'PUT' : 'POST', body: JSON.stringify(tag) }),
  deletePromptTag: (roleId: string, id: string) => api<{ revision: number }>(`${tagsPath(roleId)}/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  previewPromptTags: (roleId: string, text: string, prompt: string, negative_prompt: string) => api<PromptTagPreview>(`${tagsPath(roleId)}/preview`,
    { method: 'POST', body: JSON.stringify({ text, prompt, negative_prompt }) }),
  configuration: () => api<NovelaiConfiguration>('/api/services/novelai'),
  saveConfiguration: (configuration: Partial<Omit<NovelaiConfiguration, 'revision' | 'tokenConfigured' | 'credentialCleanupPending' | 'manualConsentRevision' | 'availability'>> & { confirmManualConsumption?: boolean }, expectedRevision: number) =>
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
