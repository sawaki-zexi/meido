import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend } from '../../test/fakeBackend';
import { SettingsDialog } from '../settings/SettingsDialog';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('桌面控制通过全局启停操作，参数页没有角色总开关', async () => {
  let saved = { id: 'computer', name: '桌面控制', description: '窗口截图发送至当前聊天模型', enabled: false,
    revision: 0, cleanupPending: false, values: {} };
  const { calls } = fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [saved] } }),
    'PUT /api/plugin-management/computer/enabled': init => {
      saved = { ...saved, enabled: JSON.parse(String(init?.body)).enabled, revision: saved.revision + 1 };
      return { body: saved };
    },
  });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  await user.click(await screen.findByRole('switch', { name: '启用 Computer Use' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 Computer Use' })).toBeChecked());
  await user.click(screen.getByRole('button', { name: 'Computer Use 设置' }));
  expect(screen.queryByRole('checkbox', { name: '允许操作本机窗口' })).not.toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: '配置角色' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '返回插件列表' }));
  await user.click(await screen.findByRole('switch', { name: '启用 Computer Use' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 Computer Use' })).not.toBeChecked());
  expect(calls.filter(call => call.method === 'PUT').map(call => call.body)).toEqual([
    { enabled: true, expectedRevision: 0 }, { enabled: false, expectedRevision: 1 },
  ]);
  expect(calls.some(call => call.path.startsWith('/api/roles'))).toBe(false);
});
