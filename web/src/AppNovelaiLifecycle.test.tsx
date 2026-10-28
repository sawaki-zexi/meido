import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from './App';
import { fakeBackend, role } from './test/fakeBackend';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); });

it('keeps the studio draft and original in-flight generation when navigating away and back', async () => {
  const owner = role({ id: 'r1', name: '绘图角色' });
  const generated = { generationId: 'gen1', attemptId: 'attempt1', roleId: 'r1', status: 'succeeded',
    parameters: { prompt: 'blue sky', negative_prompt: '', mode: 'txt2img', size_preset: 'square', width: 1024, height: 1024, seed: 1 },
    attachments: [{ id: 'image1', name: 'navigation.png', mediaType: 'image/png', kind: 'image', size: 100 }],
    createdAt: '2026-10-28T00:00:00Z', deliveryMode: 'history_only', deliveryStatus: 'history_only', consumption: 'possible', errorCode: '' };
  let completed = false;
  let signal: AbortSignal | null | undefined;
  let finish!: (value: { body: { generation: typeof generated } }) => void;
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [owner] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/roles/r1/illustrations': () => ({ body: { role: owner } }),
    'GET /api/services/novelai': () => ({ body: { enabled: true, manualEnabled: true, manualConsentRevision: 3, tokenConfigured: true,
      credentialCleanupPending: false, revision: 3, allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated',
      allowedSamplers: ['k_euler_ancestral'], defaultSampler: 'k_euler_ancestral', maxSteps: 28, maxPixels: 1048576, dailyAttemptLimit: 20 } }),
    'GET /api/roles/r1/generations': () => ({ body: { generations: completed ? [generated] : [], nextCursor: null, usage: { accountAttempts: 0, manualAttempts: 0 } } }),
    'POST /api/roles/r1/generations': (init) => { signal = init?.signal; return new Promise(resolve => { finish = resolve; }); },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('heading', { name: owner.name, level: 1 });
  const navigation = within(screen.getByRole('navigation', { name: '页面' }));
  await user.click(navigation.getByRole('button', { name: 'NovelAI 图片工作台' }));
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('工作台英文正向 tags'), 'blue sky');
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  await waitFor(() => expect(signal).toBeDefined());
  await user.click(navigation.getByRole('button', { name: '对话' }));
  expect(screen.getByLabelText('工作台英文正向 tags')).not.toBeVisible();
  expect(signal?.aborted).toBe(false);
  await act(async () => { completed = true; finish({ body: { generation: generated } }); });
  await user.click(navigation.getByRole('button', { name: 'NovelAI 图片工作台' }));
  expect(screen.getByLabelText('工作台英文正向 tags')).toHaveValue('blue sky');
  expect(await screen.findByAltText('navigation.png')).toBeVisible();
  expect(calls.filter(call => call.method === 'POST' && call.path === '/api/roles/r1/generations')).toHaveLength(1);
});
