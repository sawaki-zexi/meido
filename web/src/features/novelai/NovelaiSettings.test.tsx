import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend } from '../../test/fakeBackend';
import { NovelaiSettings } from './NovelaiSettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('saves ordinary settings without reading or resubmitting the existing token', async () => {
  let config = { enabled: false, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
    allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated',
    allowedSamplers: ['k_euler_ancestral', 'k_euler'], defaultSampler: 'k_euler_ancestral',
    maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: config }),
    'PUT /api/services/novelai': (init) => {
      const { expectedRevision, ...changes } = JSON.parse(String(init?.body));
      expect(expectedRevision).toBe(3);
      config = { ...config, ...changes, revision: 4 };
      return { body: config };
    },
  });
  const user = userEvent.setup();
  render(<NovelaiSettings />);
  expect(await screen.findByText('独立凭据已配置')).toBeInTheDocument();
  expect(screen.getByLabelText('新的 NovelAI token')).toHaveValue('');
  await user.click(screen.getByRole('checkbox', { name: '启用 NovelAI 服务' }));
  await user.click(screen.getByRole('button', { name: '保存生图设置' }));
  expect(await screen.findByText(/生图设置已保存/)).toBeInTheDocument();
  const saves = calls.filter((item) => item.method !== 'GET');
  expect(saves).toHaveLength(1);
  expect(saves[0].body).not.toHaveProperty('token');
  expect(calls.every((item) => !item.path.includes('generate'))).toBe(true);
});
