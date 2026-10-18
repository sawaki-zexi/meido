import { cleanup, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { SettingsDialog } from './SettingsDialog';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('顶层仅保留四个设置领域，服务在插件子页按需进入并可返回', async () => {
  const { calls } = fakeBackend({ 'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '角色' })] } }) });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  const nav = within(screen.getByRole('navigation', { name: '设置分类' }));
  expect(nav.getAllByRole('button').map(button => button.textContent)).toEqual(['模型', '记忆', '插件', '我的身份']);
  await user.click(screen.getByRole('button', { name: '服务与连接' }));
  expect(calls.some(call => call.path === '/api/coding/configuration')).toBe(false);
  await user.click(screen.getByRole('button', { name: /编程委托/ }));
  expect(await screen.findByRole('heading', { name: '编程委托', level: 2 })).toBeInTheDocument();
  expect(nav.getByRole('button', { name: '插件' })).toHaveAttribute('aria-current', 'page');
  await user.click(screen.getByRole('button', { name: '返回插件' }));
  expect(screen.getByRole('button', { name: /编程委托/ })).toBeInTheDocument();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it.each(['直接返回', '经模型页返回'])('角色插件与服务往返保留角色和草稿，并重新读取服务配置：%s', async (path) => {
  const roles = [role({ id: 'r1', name: '角色一' }), role({ id: 'r2', name: '角色二' })];
  let enabled = false;
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles } }),
    'GET /api/coding/configuration': () => ({ body: { enabled, repositories: [], profiles: [], nativeBuild: null } }),
    'GET /api/model/providers': () => ({ body: { providers: [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [], activeId: null } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  await user.selectOptions(await screen.findByRole('combobox', { name: '配置角色' }), 'r2');
  await user.click(screen.getByRole('button', { name: '编程委托 设置' }));
  const budget = screen.getByLabelText('每天最多编程尝试次数');
  await user.clear(budget); await user.type(budget, '7');
  await user.click(screen.getByRole('button', { name: '编程服务设置' }));
  await screen.findByRole('button', { name: '启动编程委托' });
  enabled = true;
  if (path === '经模型页返回') {
    const nav = within(screen.getByRole('navigation', { name: '设置分类' }));
    await user.click(nav.getByRole('button', { name: '模型' }));
    await user.click(nav.getByRole('button', { name: '插件' }));
  } else await user.click(screen.getByRole('button', { name: '返回插件' }));
  expect(screen.getByRole('combobox', { name: '配置角色' })).toHaveValue('r2');
  expect(screen.getByLabelText('每天最多编程尝试次数')).toHaveValue(7);
  await waitFor(() => expect(calls.filter(call => call.path === '/api/coding/configuration')).toHaveLength(3));
  expect(screen.queryByText(/先在插件.*编程委托启用全局/)).not.toBeInTheDocument();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});
