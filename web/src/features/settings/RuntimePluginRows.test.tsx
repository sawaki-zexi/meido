import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { fakeBackend } from '../../test/fakeBackend';
import { RuntimePluginRows } from './RuntimePluginRows';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('关闭自主行为与桌宠读取最新版本，只关闭该角色并保留原配置', async () => {
  let autonomy = { config: { revision: 1, activeDecisionEnabled: true, driftEnabled: true,
    driftModelConfigurationId: 'keep-model', sourceGrants: [{ sourceId: 'keep-source' }] } };
  let pet = { revision: 2, enabled: true, selectedPackageId: 'keep-package', host: { status: 'ready' } };
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: autonomy }),
    'GET /api/roles/r1/desktop-pet': () => ({ body: pet }),
    'PUT /api/roles/r1/autonomy': init => { const data = JSON.parse(String(init?.body));
      expect(data).toMatchObject({ expectedRevision: 3, config: { driftModelConfigurationId: 'keep-model', sourceGrants: [{ sourceId: 'keep-source' }], activeDecisionEnabled: false, driftEnabled: false } });
      autonomy = { config: { ...data.config, revision: 4 } }; return { body: autonomy }; },
    'PUT /api/roles/r1/desktop-pet': init => { expect(JSON.parse(String(init?.body))).toEqual({ expectedRevision: 5, packageId: 'keep-package', enabled: false });
      pet = { ...pet, revision: 6, enabled: false, host: { status: 'stopped' } }; return { body: pet }; },
  });
  const onConfigure = vi.fn(); const user = userEvent.setup();
  render(<RuntimePluginRows roleId="r1" disabled={false} onConfigure={onConfigure} />);
  await screen.findByRole('button', { name: '关闭 自主行为' });
  await screen.findByRole('button', { name: '关闭 桌宠' });
  autonomy = { config: { ...autonomy.config, revision: 3 } }; pet = { ...pet, revision: 5 };
  await user.click(screen.getByRole('button', { name: '关闭 自主行为' }));
  await screen.findByRole('button', { name: '启动 自主行为' });
  await user.click(screen.getByRole('button', { name: '关闭 桌宠' }));
  await screen.findByRole('button', { name: '启动 桌宠' });
  await user.click(screen.getByRole('button', { name: '启动 自主行为' }));
  expect(onConfigure).toHaveBeenCalledWith('autonomy');
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(2);
});

it('状态读取失败时不声称插件已关闭，并可进入配置重读', async () => {
  fakeBackend({ 'GET /api/roles/r1/autonomy': () => ({ status: 503 }), 'GET /api/roles/r1/desktop-pet': () => ({ status: 503 }) });
  const onConfigure = vi.fn(); const user = userEvent.setup(); render(<RuntimePluginRows roleId="r1" disabled={false} onConfigure={onConfigure} />);
  await waitFor(() => expect(screen.getAllByText('状态读取失败')).toHaveLength(2));
  expect(screen.getByRole('button', { name: '启动 桌宠' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: '桌宠 设置' })); expect(onConfigure).toHaveBeenCalledWith('pet');
});
