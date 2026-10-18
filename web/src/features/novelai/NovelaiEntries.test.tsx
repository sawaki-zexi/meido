import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { fakeBackend, role } from '../../test/fakeBackend';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); });

it('exposes actual service settings, role permission, and current-role generation history in the app', async () => {
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '诗织' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/services/novelai': () => ({ body: { enabled: false, tokenConfigured: false, credentialCleanupPending: false, revision: 1,
      allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler_ancestral'],
      defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 } }),
    'GET /api/roles/r1/generations': () => ({ body: { generations: [], nextCursor: null, usage: { day: '2026-10-16', accountAttempts: 0, manualAttempts: 0 } } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
    'GET /api/roles/r1/tools': () => ({ body: { tools: [], plugins: [] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('heading', { name: '诗织', level: 1 });
  await user.click(screen.getByRole('button', { name: '生图与历史' }));
  const history = await screen.findByRole('dialog', { name: '诗织的生图与历史' });
  expect(await within(history).findByText('暂无生图历史。')).toBeInTheDocument();
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('button', { name: '设置' }));
  await user.click(screen.getByRole('button', { name: '插件' }));
  await user.click(await screen.findByRole('button', { name: 'NovelAI 生图 设置' }));
  await user.click(screen.getByRole('button', { name: '角色参数' }));
  expect(await screen.findByRole('group', { name: '角色生图许可' })).toBeInTheDocument();
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('button', { name: '设置' }));
  await user.click(screen.getByRole('button', { name: '插件' }));
  await user.click(screen.getByRole('button', { name: '服务与连接' }));
  await user.click(screen.getByRole('button', { name: 'NovelAI 生图' }));
  expect(await screen.findByRole('button', { name: '启动 NovelAI' })).toBeInTheDocument();
  expect(calls.some((item) => item.method === 'POST')).toBe(false);
});
