import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { fakeBackend, role } from '../../test/fakeBackend';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); });

it('refreshes studio permission and defaults after saving plugin settings without losing the prompt', async () => {
  let configuration = { enabled: true, manualEnabled: false, manualDailyAttempts: 10, manualConsentRevision: 0,
    tokenConfigured: true, credentialCleanupPending: false, revision: 3, defaultSamples: 1,
    allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated',
    allowedSamplers: ['k_euler_ancestral'], defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 };
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '诗织' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: { generations: [], nextCursor: null, usage: { day: '2026-10-24', accountAttempts: 0, manualAttempts: 0 } } }),
    'PUT /api/services/novelai': (request) => {
      const { expectedRevision: _revision, confirmManualConsumption: _consent, ...values } = JSON.parse(String(request?.body));
      configuration = { ...configuration, ...values, revision: 4, manualConsentRevision: 4 };
      return { body: configuration };
    },
    'POST /api/roles/r1/generations': () => ({ status: 503, body: { detail: '请求已记录，等待核对' } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('heading', { name: '诗织', level: 1 });
  await user.click(screen.getByRole('button', { name: 'NovelAI 图片工作台' }));
  await screen.findByText('暂无生图历史。');
  expect(screen.getByRole('button', { name: '生成一张图片' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'NovelAI 设置' }));
  const settings = await screen.findByRole('dialog', { name: '设置' });
  await user.click(await within(settings).findByLabelText('允许角色在聊天中自主生成表情（同时开放手动生图）'));
  await user.selectOptions(within(settings).getByLabelText('默认生成张数'), '3');
  await user.click(within(settings).getByLabelText('我确认当前公共参数的有限生图消费'));
  await user.click(within(settings).getByRole('button', { name: '保存生图设置' }));
  await within(settings).findByText('NovelAI 生图设置已保存；场景 CG 在角色参数中单独确认。');
  await user.click(within(settings).getByRole('button', { name: '关闭' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '生成 3 张图片' })).toBeEnabled());
  await user.type(screen.getByLabelText('工作台英文正向 tags'), 'blue sky');
  await user.click(screen.getByRole('button', { name: 'NovelAI 设置' }));
  await screen.findByRole('dialog', { name: '设置' });
  await user.keyboard('{Escape}');
  expect(screen.getByLabelText('工作台英文正向 tags')).toHaveValue('blue sky');
  await waitFor(() => expect(screen.getByRole('button', { name: '生成 3 张图片' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: '生成 3 张图片' }));
  await screen.findByText('请求已记录，等待核对');
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
  expect(calls.find((call) => call.method === 'POST')?.body).toMatchObject({ expectedRevision: 4, parameters: { samples: 3, prompt: 'blue sky' } });
});

it('exposes actual service settings, role permission, and current-role generation history in the app', async () => {
  let enabled = true;
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '诗织' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/services/novelai': () => ({ body: { enabled, tokenConfigured: false, credentialCleanupPending: false, revision: 1,
      allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler_ancestral'],
      defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 } }),
    'GET /api/roles/r1/generations': () => ({ body: { generations: [], nextCursor: null, usage: { day: '2026-10-16', accountAttempts: 0, manualAttempts: 0 } } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
    'GET /api/roles/r1/tools': () => ({ body: { tools: [], plugins: [] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('heading', { name: '诗织', level: 1 });
  await user.click(screen.getByRole('button', { name: '更多' }));
  await user.click(await screen.findByRole('menuitem', { name: '生图与历史' }));
  const history = await screen.findByRole('dialog', { name: '诗织的生图与历史' });
  expect(await within(history).findByText('暂无生图历史。')).toBeInTheDocument();
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('button', { name: 'NovelAI 图片工作台' }));
  expect(await screen.findByRole('heading', { name: '图片工作台' })).toBeInTheDocument();
  expect(await screen.findByText('暂无生图历史。')).toBeInTheDocument();
  enabled = false;
  await user.click(screen.getByRole('button', { name: '设置' }));
  await user.click(screen.getByRole('button', { name: '插件' }));
  await user.click(await screen.findByRole('button', { name: 'NovelAI 生图 设置' }));
  await user.click(screen.getByRole('button', { name: '角色参数' }));
  expect(await screen.findByRole('group', { name: '角色生图许可' })).toBeInTheDocument();
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('button', { name: '设置' }));
  await user.click(screen.getByRole('button', { name: '插件' }));
  await user.click(await screen.findByRole('button', { name: 'NovelAI 生图 设置' }));
  expect(await screen.findByRole('button', { name: '启动 NovelAI' })).toBeInTheDocument();
  expect(calls.some((item) => item.method === 'POST')).toBe(false);
});
