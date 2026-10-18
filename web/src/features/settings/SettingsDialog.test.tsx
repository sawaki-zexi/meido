import { cleanup, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { SettingsDialog } from './SettingsDialog';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('顶层仅保留四个设置领域，服务在插件子页按需进入并可返回', async () => {
  const { calls } = fakeBackend({ 'GET /api/plugin-management': () => ({ body: { plugins: [] } }) });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  const nav = within(screen.getByRole('navigation', { name: '设置分类' }));
  expect(nav.getAllByRole('button').map(button => button.textContent)).toEqual(['模型', '记忆', '插件', '我的身份']);
  await user.click(screen.getByRole('button', { name: '服务与连接' }));
  await user.click(screen.getByRole('button', { name: /编程委托/ }));
  expect(await screen.findByRole('heading', { name: '编程委托', level: 2 })).toBeInTheDocument();
  expect(nav.getByRole('button', { name: '插件' })).toHaveAttribute('aria-current', 'page');
  await user.click(screen.getByRole('button', { name: '返回插件' }));
  expect(screen.getByRole('button', { name: /编程委托/ })).toBeInTheDocument();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('编程插件仅在展开用途参数后选择角色，保存只更新该用途并保留最新资料', async () => {
  const saved = role({ id: 'r2', name: '角色二', agentConfig: { coding: { enabled: false, maxAttemptsPerDay: 3,
    repoGrantIds: [], profileIds: [], defaultHerdrProfileId: null, defaultCodexProfileId: null } } });
  const { calls } = fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [] } }),
    'GET /api/roles': () => ({ body: { roles: [saved] } }),
    'GET /api/roles/r2': () => ({ body: { role: { ...saved, name: '外部更新' } } }),
    'PUT /api/roles/r2': init => ({ body: { role: { ...saved, name: '外部更新', agentConfig: {
      ...saved.agentConfig, coding: { ...saved.agentConfig!.coding, ...JSON.parse(String(init?.body)).agentConfig.coding } } } } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  expect(calls.some(call => call.path === '/api/roles')).toBe(false);
  await user.click(await screen.findByRole('button', { name: '编程委托 设置' }));
  expect(screen.queryByRole('combobox', { name: '角色参数' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '角色参数' }));
  expect(await screen.findByRole('combobox', { name: '角色参数' })).toHaveValue('r2');
  expect(screen.queryByRole('checkbox', { name: '允许编程委托' })).not.toBeInTheDocument();
  const budget = await screen.findByLabelText('每天最多编程尝试次数');
  await user.clear(budget); await user.type(budget, '7');
  await user.click(screen.getByRole('button', { name: '保存角色参数' }));
  await screen.findByText('角色参数已保存');
  const changes = calls.find(call => call.method === 'PUT')?.body as { name: string; agentConfig: unknown };
  expect(changes.name).toBe('外部更新');
  expect(changes.agentConfig).toEqual({ coding: { maxAttemptsPerDay: 7 } });
  await waitFor(() => expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1));
});

it('公共参数草稿在模型页往返后保留', async () => {
  fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [{ id: 'browser', name: '浏览器', description: '', enabled: false,
      revision: 0, cleanupPending: false, values: { headed: false, timeout_seconds: 45 } }] } }),
    'GET /api/model/providers': () => ({ body: { providers: [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [], activeId: null } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  await user.click(await screen.findByRole('button', { name: '浏览器 设置' }));
  await user.click(screen.getByRole('checkbox', { name: '显示浏览器窗口' }));
  const nav = within(screen.getByRole('navigation', { name: '设置分类' }));
  await user.click(nav.getByRole('button', { name: '模型' }));
  await user.click(nav.getByRole('button', { name: '插件' }));
  expect(screen.getByRole('checkbox', { name: '显示浏览器窗口' })).toBeChecked();
});
