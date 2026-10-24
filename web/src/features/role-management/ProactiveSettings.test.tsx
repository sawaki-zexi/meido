import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { fakeBackend } from '../../test/fakeBackend';
import { AutonomySettings } from './AutonomySettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const settings = { revision: 3, enabled: false, profile: 'daily', agentMaxSteps: 35,
  agentContentLimit: 5, agentWebFetchMaxChars: 8000, driftMaxSteps: 20, driftMinIntervalHours: 3 };
const state = { settings, modelReady: true, cleanupPending: false, problem: '' };

it('按 Shiori 展示主动推送、策略与折叠执行参数，编辑后只保存当前角色偏好', async () => {
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy/settings': () => ({ body: state }),
    'PUT /api/roles/r1/autonomy/settings': init => ({ body: { ...state,
      settings: { ...JSON.parse(String(init?.body)), revision: 4 } } }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const toggle = await screen.findByRole('switch', { name: '主动推送' });
  expect(toggle).toHaveAttribute('aria-checked', 'false');
  expect(screen.getAllByRole('switch')).toHaveLength(1);
  expect(screen.getByRole('combobox', { name: '推送策略' })).toHaveValue('daily');
  expect(screen.queryByText('主动决策模型')).not.toBeInTheDocument();
  expect(screen.queryByText('来源许可草稿')).not.toBeInTheDocument();
  expect(screen.getByLabelText('每次推送最大步数')).not.toBeVisible();
  expect(calls.filter(call => call.method !== 'GET')).toHaveLength(0);
  await user.click(screen.getByText('执行参数', { selector: 'summary' }));
  expect(screen.getByLabelText('每次推送最大步数')).toHaveValue(35);
  expect(screen.getByLabelText('候选内容数')).toHaveValue(5);
  expect(screen.getByLabelText('网页上下文字符数')).toHaveValue(8000);
  expect(screen.getByLabelText('空闲活动最大步数')).toHaveValue(20);
  expect(screen.getByLabelText('空闲活动最小间隔（小时）')).toHaveValue(3);
  await user.clear(screen.getByLabelText('候选内容数'));
  await user.type(screen.getByLabelText('候选内容数'), '2');
  await user.click(screen.getByText('执行参数', { selector: 'summary' }));
  await user.selectOptions(screen.getByRole('combobox', { name: '推送策略' }), 'quiet');
  await user.click(toggle);
  await user.click(screen.getByRole('button', { name: '保存' }));
  expect(await screen.findByRole('status')).toHaveTextContent('已保存');
  expect(calls.find(call => call.method === 'PUT')?.body).toEqual({ expectedRevision: 3, enabled: true,
    profile: 'quiet', agentMaxSteps: 35, agentContentLimit: 2, agentWebFetchMaxChars: 8000,
    driftMaxSteps: 20, driftMinIntervalHours: 3 });
});

it('保存冲突保留草稿，不自动覆盖，并允许重新读取', async () => {
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy/settings': () => ({ body: state }),
    'PUT /api/roles/r1/autonomy/settings': () => ({ status: 409, body: { detail: '配置已变化' } }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole('switch');
  await user.selectOptions(screen.getByRole('combobox', { name: '推送策略' }), 'quiet');
  await user.click(screen.getByRole('button', { name: '保存' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('配置已变化');
  expect(screen.getByRole('combobox', { name: '推送策略' })).toHaveValue('quiet');
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1);
  await user.click(screen.getByRole('button', { name: '重新读取' }));
  await waitFor(() => expect(screen.getByRole('combobox', { name: '推送策略' })).toHaveValue('daily'));
});

it('没有聊天模型时可保存关闭状态，启用时提示配置模型', async () => {
  fakeBackend({ 'GET /api/roles/r1/autonomy/settings': () => ({ body: { ...state, modelReady: false } }) });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await user.click(await screen.findByRole('switch'));
  expect(screen.getByRole('alert')).toHaveTextContent('请先配置角色的聊天模型');
  expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  await user.click(screen.getByRole('switch'));
  expect(screen.getByRole('button', { name: '保存' })).toBeEnabled();
});

it('切换角色后，旧读取响应不会污染新角色或改变保存目标', async () => {
  let finish!: (value: { body: typeof state }) => void;
  const next = { ...state, settings: { ...settings, profile: 'quiet', agentContentLimit: 2 } };
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy/settings': () => new Promise(resolve => { finish = resolve; }),
    'GET /api/roles/r2/autonomy/settings': () => ({ body: next }),
    'PUT /api/roles/r2/autonomy/settings': () => ({ body: next }),
  });
  const user = userEvent.setup();
  const view = render(<AutonomySettings roleId="r1" disabled={false} />);
  await waitFor(() => expect(finish).toBeDefined());
  view.rerender(<AutonomySettings roleId="r2" disabled={false} />);
  await screen.findByRole('switch');
  await act(async () => finish({ body: state }));
  expect(screen.getByRole('combobox', { name: '推送策略' })).toHaveValue('quiet');
  await user.click(screen.getByRole('button', { name: '保存' }));
  await screen.findByText('已保存');
  expect(calls.filter(call => call.method === 'PUT').map(call => call.path)).toEqual(['/api/roles/r2/autonomy/settings']);
});

it('没有来源或任务时显示简短状态，避免已开启但静默不工作的误解', async () => {
  fakeBackend({ 'GET /api/roles/r1/autonomy/settings': () => ({ body: { ...state,
    settings: { ...settings, enabled: true }, activityNote: '尚无内容来源或空闲任务，可在高级设置的开发者模式中添加。' } }) });
  render(<AutonomySettings roleId="r1" disabled={false} />);
  expect(await screen.findByText(/尚无内容来源或空闲任务/)).toBeVisible();
});
