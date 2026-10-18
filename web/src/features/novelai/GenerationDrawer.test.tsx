import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { GenerationDrawer } from './GenerationDrawer';
import type { AgentToolConfig } from '../../api/types';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('global manual grant enables a default-off role and preserves the original request ID on retry', async () => {
  let attempts = 0;
  const generated = { generationId: 'gen-171', attemptId: 'attempt-171', roleId: 'r1', status: 'succeeded',
    parameters: { prompt: 'blue sky', mode: 'txt2img', width: 1024, height: 1024, seed: 171, model: 'nai-diffusion-4-5-curated', steps: 28 },
    attachments: [{ id: 'image-171', name: 'generated.png', kind: 'image', mediaType: 'image/png', size: 171 }],
    taskId: 'task-171', createdAt: '2026-10-16T04:00:00Z', errorCode: '', consumption: 'possible', deliveryStatus: 'sent' };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: { enabled: true, manualEnabled: true, manualDailyAttempts: 5, manualConsentRevision: 3, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
      allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler_ancestral'],
      defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 } }),
    'GET /api/roles/r1/generations': () => ({ body: { generations: attempts > 1 ? [generated] : [], nextCursor: null,
      usage: { day: '2026-10-16', accountAttempts: attempts > 1 ? 1 : 0, manualAttempts: attempts > 1 ? 1 : 0 } } }),
    'POST /api/roles/r1/generations': () => (++attempts === 1 ? { status: 503, body: { detail: '本地步骤尚未完成' } } : { body: { generation: generated } }),
  });
  const user = userEvent.setup();
  const refresh = vi.fn();
  render(<GenerationDrawer role={role({ id: 'r1', name: '诗织', agentConfig: { novelai: { manualEnabled: false, manualDailyAttempts: 10, consentRevision: 0 } } as AgentToolConfig })} onClose={() => {}} onGenerated={refresh} />);
  await screen.findByText('暂无生图历史。');
  expect(calls.filter((item) => item.method === 'POST')).toHaveLength(0);
  await user.type(screen.getByLabelText('英文正向 tags'), 'blue sky');
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  expect(await screen.findByText('本地步骤尚未完成')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '重查本次生成' }));
  expect(await screen.findByAltText('generated.png')).toBeInTheDocument();
  const posts = calls.filter((item) => item.method === 'POST');
  expect(posts).toHaveLength(2);
  expect((posts[0].body as { requestId: string }).requestId).toBe((posts[1].body as { requestId: string }).requestId);
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
});

it('aborts an old role request and ignores its late image after switching roles', async () => {
  let resolveGeneration: ((value: { body: unknown }) => void) | undefined;
  let generationSignal: AbortSignal | null | undefined;
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: { enabled: true, manualEnabled: true, manualDailyAttempts: 5, manualConsentRevision: 3, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
      allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler_ancestral'],
      defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 } }),
    'GET /api/roles/r[12]/generations': () => ({ body: { generations: [], nextCursor: null,
      usage: { day: '2026-10-16', accountAttempts: 0, manualAttempts: 0 } } }),
    'POST /api/roles/r1/generations': (request) => {
      generationSignal = request?.signal;
      return new Promise((resolve) => { resolveGeneration = resolve; });
    },
  });
  const first = role({ id: 'r1', name: '诗织', agentConfig: { novelai: { manualEnabled: true, consentRevision: 3 } } as AgentToolConfig });
  const second = role({ id: 'r2', name: '另一角色', agentConfig: first.agentConfig });
  const refresh = vi.fn();
  const view = render(<GenerationDrawer role={first} onClose={() => {}} onGenerated={refresh} />);
  const user = userEvent.setup();
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('英文正向 tags'), 'blue sky');
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  await waitFor(() => expect(resolveGeneration).toBeDefined());
  view.rerender(<GenerationDrawer role={second} onClose={() => {}} onGenerated={refresh} />);
  await screen.findByText('另一角色的生图与历史');
  expect(generationSignal?.aborted).toBe(true);
  resolveGeneration?.({ body: { generation: { status: 'succeeded', attachments: [{ id: 'old-image', name: 'old.png' }] } } });
  await screen.findByText('暂无生图历史。');
  expect(screen.queryByAltText('old.png')).not.toBeInTheDocument();
  expect(refresh).not.toHaveBeenCalled();
  expect(screen.getByLabelText('英文正向 tags')).toHaveValue('');
});
