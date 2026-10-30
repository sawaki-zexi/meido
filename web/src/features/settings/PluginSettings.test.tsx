import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsDialog } from './SettingsDialog';
import { fakeBackend } from '../../test/fakeBackend';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const browser = { id: 'browser', name: '浏览器', description: '会话分别归属角色', enabled: false,
  revision: 0, cleanupPending: false, values: { headed: false, timeout_seconds: 45 } };

it('Herdr 现有 session 参数与启用独立，状态显示手动代理和真实错误', async () => {
  const saved = { id: 'herdr', name: 'Herdr 代理', description: '操作已有代理', enabled: false,
    revision: 0, cleanupPending: false,
    values: { executable: 'herdr', session: 'default', cli_timeout_seconds: 45, task_timeout_seconds: 7200 } };
  let checks = 0;
  const { calls } = fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [saved] } }),
    'PUT /api/plugin-management/herdr/configuration': init => ({ body: { ...saved, revision: 1,
      values: JSON.parse(String(init?.body)).values } }),
    'GET /api/plugin-management/herdr/status': () => ({ body: ++checks === 1
      ? { ok: true, session: 'manual', workspaces: [{ workspace_id: 'w', label: '工作区', agents: [
        { pane_id: 'pane-1', title: '手动 Codex', status: 'blocked', cwd: 'D:/work', handler: { role_id: 'r', role_name: '诗织' } }] }] }
      : { ok: false, error: { code: 'server_unavailable', message: 'Herdr session 未运行' } } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  await user.click(await screen.findByRole('button', { name: 'Herdr 代理 设置' }));
  expect(screen.getByLabelText('Herdr Session')).toHaveValue('default');
  expect(screen.getByLabelText('CLI 超时（秒）')).toHaveValue(45);
  expect(screen.getByLabelText('代理追踪超时（秒）')).toHaveValue(7200);
  await user.clear(screen.getByLabelText('Herdr Session')); await user.type(screen.getByLabelText('Herdr Session'), 'manual');
  await user.click(screen.getByRole('button', { name: '保存参数' }));
  await screen.findByText('公共参数已保存');
  expect(calls.find(call => call.method === 'PUT')?.body).toEqual({ expectedRevision: 0, values: { ...saved.values, session: 'manual' } });
  expect(calls.some(call => call.path.endsWith('/enabled'))).toBe(false);
  await user.click(screen.getByRole('button', { name: '检查已保存的 session' }));
  expect(await screen.findByText(/手动 Codex/)).toHaveTextContent('blocked');
  expect(screen.getByText(/接手角色：诗织/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '检查已保存的 session' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Herdr session 未运行');
  expect(screen.queryByText(/手动 Codex/)).not.toBeInTheDocument();
});

it('已启用但缺少凭据的 NovelAI 在插件列表显示待配置', async () => {
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: { enabled: true, tokenConfigured: false,
      credentialCleanupPending: false, revision: 1 } }),
  });
  render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  expect(await screen.findByText('待配置凭据')).toBeInTheDocument();
  expect(screen.getByRole('switch', { name: '启用 NovelAI 生图' })).toBeChecked();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('插件全局启动，无需选择角色或改写角色文件，普通工具不出现在列表', async () => {
  let saved = browser;
  const { calls } = fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [saved] } }),
    'PUT /api/plugin-management/browser/enabled': init => {
      const body = JSON.parse(String(init?.body));
      saved = { ...saved, enabled: body.enabled, revision: 1 };
      return { body: saved };
    },
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  await user.click(await screen.findByRole('switch', { name: '启用 Browser Use' }));
  expect(await screen.findByRole('switch', { name: '启用 Browser Use' })).toBeChecked();
  expect(screen.queryByRole('combobox', { name: '配置角色' })).not.toBeInTheDocument();
  expect(screen.queryByText(/read_file|默认自带|工具目录/)).not.toBeInTheDocument();
  expect(calls.some(call => call.path.startsWith('/api/roles'))).toBe(false);
  expect(calls.filter(call => call.method === 'PUT').map(call => call.body)).toEqual([{ enabled: true, expectedRevision: 0 }]);
});

it('保存公共浏览器参数不同时启用插件，失败保留草稿并显示错误', async () => {
  const { calls } = fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [browser] } }),
    'PUT /api/plugin-management/browser/configuration': () => ({ status: 409, body: { detail: '设置已变化，请刷新' } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  await user.click(await screen.findByRole('button', { name: 'Browser Use 设置' }));
  await user.click(screen.getByRole('checkbox', { name: '显示浏览器窗口' }));
  await user.click(screen.getByRole('button', { name: '保存参数' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('设置已变化');
  expect(screen.getByRole('checkbox', { name: '显示浏览器窗口' })).toBeChecked();
  await waitFor(() => expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1));
  expect(calls.find(call => call.method === 'PUT')?.body).toEqual({ values: { headed: true, timeout_seconds: 45 }, expectedRevision: 0 });
});

it('凭据就绪后可直接生图，参数保存不切换插件且不写角色 CG', async () => {
  let configuration = { enabled: true, manualEnabled: false, manualDailyAttempts: 10, manualConsentRevision: 0,
    tokenConfigured: true, credentialCleanupPending: false, revision: 3, allowedModels: ['nai-diffusion-4-5-curated'],
    defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler_ancestral'], defaultSampler: 'k_euler_ancestral',
    maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'PUT /api/services/novelai': init => {
      const { confirmManualConsumption, expectedRevision: _, ...changes } = JSON.parse(String(init?.body));
      configuration = { ...configuration, ...changes, revision: 4, manualConsentRevision: confirmManualConsumption ? 4 : 0 };
      return { body: configuration };
    },
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  await user.click(await screen.findByRole('button', { name: 'NovelAI 生图 设置' }));
  await screen.findByText('可生图');
  expect(screen.queryByRole('checkbox', { name: '我确认生图消费' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '保存生图设置' }));
  await screen.findByText('NovelAI 生图设置已保存。');
  expect(calls.find(call => call.method === 'PUT')?.body).toMatchObject({ expectedRevision: 3 });
  expect(calls.find(call => call.method === 'PUT')?.body).not.toHaveProperty('confirmManualConsumption');
  expect(calls.find(call => call.method === 'PUT')?.body).not.toHaveProperty('enabled');
  expect(calls.some(call => call.path.startsWith('/api/roles'))).toBe(false);
  await user.clear(screen.getByLabelText('最多采样步数')); await user.type(screen.getByLabelText('最多采样步数'), '12');
  expect(screen.getByText('可生图')).toBeInTheDocument();
});

it('待办启停只写全局开关，不回填提醒和时区参数', async () => {
  const { calls } = fakeBackend({
    'GET /api/todos/settings': () => ({ body: { enabled: false, remindersEnabled: true, timezone: 'Asia/Tokyo' } }),
    'PUT /api/todos/settings': () => ({ body: { enabled: true, remindersEnabled: false, timezone: 'UTC' } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  await user.click(await screen.findByRole('switch', { name: '启用 待办' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 待办' })).toBeChecked());
  expect(calls.find(call => call.method === 'PUT')?.body).toEqual({ enabled: true });
});
