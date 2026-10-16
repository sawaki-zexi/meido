import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { fakeBackend, message, role } from '../../test/fakeBackend';
import type { AgentToolConfig } from '../../api/types';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); });

it('offers chat regeneration only for a host-verified generated image and preserves other message attachments', async () => {
  const owner = role({ id: 'r1', name: '诗织', agentConfig: { novelai: { manualEnabled: true, consentRevision: 3 } } as AgentToolConfig });
  const generated = { id: 'image-generated', name: 'generated.png', kind: 'image', mediaType: 'image/png', size: 100 };
  const uploaded = { ...generated, id: 'image-uploaded', name: 'uploaded.png' };
  const document = { ...generated, id: 'document', name: 'notes.txt', kind: 'file', mediaType: 'text/plain' };
  const source = { generationId: 'gen-chat', attemptId: 'attempt-chat', roleId: 'r1', status: 'succeeded',
    parameters: { prompt: 'blue sky', negative_prompt: '', mode: 'txt2img', width: 1024, height: 1024, seed: 172,
      model: 'nai-diffusion-4-5-curated', sampler: 'k_euler_ancestral', steps: 28 }, attachments: [generated],
    taskId: 'task-chat', createdAt: '2026-10-16T04:00:00Z', errorCode: '', consumption: 'possible', deliveryStatus: 'sent',
    parentAttemptId: null, deliveryMode: 'foreground_reply', deliveryMessageId: 'message-chat', expectedAttachmentId: null };
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [owner] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [
      message({ id: 'message-chat', role: 'assistant', content: '图片和文件', metadata: {
        attachments: [generated, uploaded, document], generationAttemptIds: ['attempt-chat'] } }),
      message({ id: 'message-upload', role: 'user', content: '', metadata: { attachments: [{ ...uploaded, id: 'user-upload', name: 'user-upload.png' }] } }),
    ] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
    'GET /api/roles/r1/model-configuration': () => ({ body: { configurationId: null, effectiveConfigurationId: null } }),
    'GET /api/services/novelai': () => ({ body: { enabled: true, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
      allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler_ancestral'],
      defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 } }),
    'GET /api/roles/r1/generations': () => ({ body: { generations: [], nextCursor: null,
      usage: { day: '2026-10-16', accountAttempts: 1, manualAttempts: 1 } } }),
    'GET /api/roles/r1/generations/attachment/image-generated\\?messageId=message-chat': () => ({ body: { generation: source } }),
    'GET /api/roles/r1/generations/attachment/image-uploaded\\?messageId=message-chat': () => ({ status: 404, body: { detail: '不是生成图片' } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole('button', { name: '重新生成 generated.png' }));
  const drawer = screen.getByRole('dialog', { name: '诗织的生图与历史' });
  expect(await within(drawer).findByText('替换 generated.png；其他图片和附件保持不变。')).toBeInTheDocument();
  expect(within(drawer).getByLabelText('英文正向 tags')).toHaveValue('blue sky');
  expect(within(drawer).getByLabelText('生图 Seed')).toHaveValue(null);
  expect(screen.queryByRole('button', { name: '重新生成 uploaded.png' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '重新生成 user-upload.png' })).not.toBeInTheDocument();
  expect(screen.getByAltText('uploaded.png')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'notes.txt' })).toBeInTheDocument();
  expect(calls.some((call) => call.path.includes('user-upload?'))).toBe(false);
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
});

it('shows a new internal completion in the original chat after observing accepted background work', async () => {
  let complete = false;
  const owner = role({ id: 'r1', name: '诗织', agentConfig: { novelai: { manualEnabled: true, consentRevision: 3 } } as AgentToolConfig });
  const image = { id: 'background-image', name: 'background-chat.png', kind: 'image', mediaType: 'image/png', size: 100 };
  const accepted = { generationId: 'gen-background', attemptId: 'attempt-background', roleId: 'r1', status: 'dispatching',
    parameters: { prompt: 'blue sky', negative_prompt: '', mode: 'txt2img', width: 1024, height: 1024, seed: 172,
      model: 'nai-diffusion-4-5-curated', sampler: 'k_euler_ancestral', steps: 28, background: true }, attachments: [],
    taskId: 'task-background', createdAt: '2026-10-16T04:00:00Z', errorCode: '', consumption: 'possible', deliveryStatus: 'pending',
    parentAttemptId: null, deliveryMode: 'source_completion', deliveryMessageId: null, expectedAttachmentId: null };
  const finished = { ...accepted, status: 'succeeded', deliveryStatus: 'sent', deliveryMessageId: 'message-completion', attachments: [image] };
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [owner] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: complete ? [
      message({ id: 'message-completion', role: 'assistant', content: '后台图片已完成', messageType: 'internal_completion',
        metadata: { attachments: [image], generationAttemptIds: ['attempt-background'] } }),
    ] : [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
    'GET /api/roles/r1/model-configuration': () => ({ body: { configurationId: null, effectiveConfigurationId: null } }),
    'GET /api/services/novelai': () => ({ body: { enabled: true, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
      allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler_ancestral'],
      defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 } }),
    'GET /api/roles/r1/generations': () => ({ body: { generations: [], nextCursor: null,
      usage: { day: '2026-10-16', accountAttempts: 0, manualAttempts: 0 } } }),
    'POST /api/roles/r1/generations': () => ({ status: 202, body: { generation: accepted } }),
    'GET /api/roles/r1/generations/gen-background\\?attemptId=attempt-background': () => { complete = true; return { body: { generation: finished } }; },
    'GET /api/roles/r1/generations/attachment/background-image\\?messageId=message-completion': () => ({ body: { generation: finished } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole('button', { name: '生图与历史' }));
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('英文正向 tags'), 'blue sky');
  await user.click(screen.getByLabelText('在后台生成，完成后交付原来源'));
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  await screen.findByText('后台图片已完成', {}, { timeout: 3500 });
  await user.keyboard('{Escape}');
  expect(screen.getByAltText('background-chat.png')).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
});
