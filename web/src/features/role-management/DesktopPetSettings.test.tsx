import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { fakeBackend } from '../../test/fakeBackend';
import { DesktopPetSettings } from './DesktopPetSettings';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('独立保存桌宠开关并显示原生状态与动作回执', async () => {
  let state = { revision: 1, enabled: false, selectedPackageId: 'fixture', unavailableReason: '角色桌宠已关闭',
    packages: [{ id: 'fixture', displayName: '测试包', description: '主人素材', actions: { wave: 'waving' } }],
    host: { status: 'stopped', reason: '' }, history: [] as { type: string; actionId: string }[] };
  fakeBackend({ 'GET /api/roles/r1/desktop-pet': () => ({ body: state }),
    'PUT /api/roles/r1/desktop-pet': (init) => {
      expect(JSON.parse(String(init?.body))).toEqual({ expectedRevision: 1, packageId: 'fixture', enabled: true });
      state = { ...state, revision: 2, enabled: true, unavailableReason: '', host: { status: 'ready', reason: '' },
        history: [{ type: 'accepted', actionId: 'owned-action' }, { type: 'started', actionId: 'owned-action' },
          { type: 'finished', actionId: 'owned-action' }] };
      return { body: state };
    } });
  render(<DesktopPetSettings roleId="r1" />);
  expect(await screen.findByText('可用动作：wave；支持移动到五个桌面位置。')).toBeTruthy();
  const toggle = screen.getByRole('checkbox', { name: '开启角色桌宠' });
  expect(toggle).not.toBeChecked();
  await userEvent.click(toggle);
  expect(await screen.findByText('窗口已就绪')).toBeTruthy();
  expect(toggle).toBeChecked();
  expect(screen.getByText('开始播放 · owned-ac')).toBeTruthy();
  expect(screen.getByText('已完成 · owned-ac')).toBeTruthy();
});
