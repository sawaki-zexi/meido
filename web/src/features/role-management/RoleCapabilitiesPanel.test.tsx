import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { RoleCapabilitiesPanel } from './RoleCapabilitiesPanel';
import { defaultNovelaiRoleConfig } from '../../api/novelai';

const alice = role({ id: 'r1', name: '爱丽丝' });
const push = { settings: { revision: 2, enabled: false, profile: 'quiet', agentMaxSteps: 12,
  agentContentLimit: 2, agentWebFetchMaxChars: 2000, driftMaxSteps: 5, driftMinIntervalHours: 24 },
  modelReady: true, cleanupPending: false, problem: '' };
const pet = { revision: 3, enabled: false, selectedPackageId: null, packages: [],
  unavailableReason: '', host: { status: 'stopped', reason: '' }, history: [] };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function runtimeBackend(routes: Parameters<typeof fakeBackend>[0]) {
  return fakeBackend({
    'GET /api/todos/reminder-roles': () => ({ body: { roles: ['r1', 'r2'].map(id => ({ id, name: id, enabled: false, activeToday: false, effectiveDate: '2026-10-25' })) } }),
    'GET /api/todos/reminders/today': () => ({ body: { date: '2026-10-25', paused: false } }),
    'GET /api/owner-knowledge': () => ({ body: { enabled: false, connected: false } }),
    'GET /api/memory/settings': () => ({ body: { enabled: true, embeddingEnabled: false } }),
    ...routes,
  });
}


it('能力页展示项目共享服务，待办提醒只修改当前角色资格并说明次日生效', async () => {
  const onOpenSettings = vi.fn();
  const { calls } = runtimeBackend({
    'GET /api/roles/r1/autonomy/settings': () => ({ body: push }),
    'GET /api/roles/r1/desktop-pet': () => ({ body: pet }),
    'GET /api/roles/r1': () => ({ body: { role: alice } }),
    'GET /api/todos/settings': () => ({ body: { enabled: true, remindersEnabled: true, timezone: 'Asia/Shanghai' } }),
    'GET /api/todos/reminder-roles': () => ({ body: { roles: [
      { id: 'r1', name: '爱丽丝', enabled: false, activeToday: false, effectiveDate: '2026-10-25' },
      { id: 'r2', name: '贝拉', enabled: true, activeToday: true, effectiveDate: '2026-10-25' },
    ] } }),
    'GET /api/todos/reminders/today': () => ({ body: { date: '2026-10-25', paused: false } }),
    'GET /api/owner-knowledge': () => ({ body: { enabled: false, connected: false } }),
    'GET /api/memory/settings': () => ({ body: { enabled: true, embeddingEnabled: false } }),
    'PUT /api/todos/reminder-roles/r1': () => ({ body: { roleId: 'r1', enabled: true, effectiveDate: '2026-10-26' } }),
  });
  const user = userEvent.setup();
  render(<RoleCapabilitiesPanel role={alice} onOpenSettings={onOpenSettings} />);
  const reminder = await screen.findByRole('switch', { name: '待办提醒' });
  await waitFor(() => expect(reminder).toBeEnabled());
  expect(screen.queryByRole('switch', { name: '自然聊天' })).not.toBeInTheDocument();
  expect(screen.queryByRole('switch', { name: 'NSFW 记忆' })).not.toBeInTheDocument();
  await user.click(reminder);
  expect(reminder).toHaveAccessibleDescription(/启用次日生效，关闭立即生效/);
  await user.click(screen.getByRole('button', { name: '保存' }));
  expect(await screen.findByText('2026-10-26 起生效')).toBeVisible();
  expect(calls.filter(call => call.method !== 'GET')).toEqual([
    { method: 'PUT', path: '/api/todos/reminder-roles/r1', body: { enabled: true } },
  ]);
  expect(screen.getByRole('heading', { name: '主人资料' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: '主人资料设置' }));
  expect(onOpenSettings).toHaveBeenCalledWith('owner-knowledge');
});

it('从全局设置返回刷新服务状态，同时保留未保存的角色开关', async () => {
  let enabled = false;
  const { calls } = runtimeBackend({
    'GET /api/roles/r1/autonomy/settings': () => ({ body: push }),
    'GET /api/roles/r1/desktop-pet': () => ({ body: pet }),
    'GET /api/roles/r1': () => ({ body: { role: alice } }),
    'GET /api/todos/settings': () => ({ body: { enabled, remindersEnabled: true, timezone: 'Asia/Shanghai' } }),
  });
  const user = userEvent.setup();
  const page = render(<RoleCapabilitiesPanel role={alice} settingsVersion={0} />);
  const pushSwitch = screen.getByRole('switch', { name: '主动推送' });
  await waitFor(() => expect(pushSwitch).toBeEnabled());
  expect(screen.getByRole('switch', { name: '待办提醒' })).toBeDisabled();
  await user.click(pushSwitch);
  enabled = true;
  page.rerender(<RoleCapabilitiesPanel role={alice} settingsVersion={1} />);
  await waitFor(() => expect(screen.getByRole('switch', { name: '待办提醒' })).toBeEnabled());
  expect(pushSwitch).toHaveAttribute('aria-checked', 'true');
  expect(screen.getByRole('button', { name: '保存' })).toBeEnabled();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('聊天安排次日退出时说明今日仍参与，并能保存立即退出当前角色', async () => {
  const { calls } = runtimeBackend({
    'GET /api/roles/r1/autonomy/settings': () => ({ body: push }),
    'GET /api/roles/r1/desktop-pet': () => ({ body: pet }),
    'GET /api/roles/r1': () => ({ body: { role: alice } }),
    'GET /api/todos/settings': () => ({ body: { enabled: true, remindersEnabled: true, timezone: 'Asia/Shanghai' } }),
    'GET /api/todos/reminder-roles': () => ({ body: { roles: [
      { id: 'r1', name: '爱丽丝', enabled: false, activeToday: true, effectiveDate: '2026-10-26' },
    ] } }),
    'PUT /api/todos/reminder-roles/r1': () => ({ body: { roleId: 'r1', enabled: false, effectiveDate: '2026-10-25' } }),
  });
  const user = userEvent.setup();
  render(<RoleCapabilitiesPanel role={alice} />);
  expect(await screen.findByText('2026-10-26 起停用 · 今日仍参与')).toBeVisible();
  const toggle = screen.getByRole('switch', { name: '待办提醒' });
  expect(toggle).toHaveAttribute('aria-checked', 'true');
  expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  await user.click(toggle);
  await user.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存' })).toBeDisabled());
  expect(toggle).toHaveAttribute('aria-checked', 'false');
  expect(toggle).toHaveAttribute('title', '未启用');
  expect(calls.filter(call => call.method !== 'GET')).toEqual([
    { method: 'PUT', path: '/api/todos/reminder-roles/r1', body: { enabled: false } },
  ]);
});

it('运行卡片显示未配置状态，开关可重置且保存失败保留更改', async () => {
  let attempts = 0;
  const { calls } = runtimeBackend({
    'GET /api/roles/r1/autonomy/settings': () => ({ body: push }),
    'GET /api/roles/r1/desktop-pet': () => ({ body: pet }),
    'GET /api/roles/r1': () => ({ body: { role: alice } }),
    'PUT /api/roles/r1/autonomy/settings': () => ++attempts === 1
      ? { status: 503, body: { detail: '暂时无法保存推送' } }
      : { body: { ...push, settings: { ...push.settings, revision: 3, enabled: true } } },
  });
  const user = userEvent.setup();
  render(<RoleCapabilitiesPanel role={alice} />);
  const toggle = await screen.findByRole('switch', { name: '主动推送' });
  await waitFor(() => expect(toggle).toBeEnabled());
  expect(screen.getByRole('switch', { name: '桌宠' })).toBeDisabled();
  expect(screen.getByText('未配置桌宠')).toBeVisible();
  await user.click(toggle);
  expect(toggle).toHaveAccessibleDescription('使用角色聊天模型，开启后会产生模型用量。');
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: '重置' }));
  expect(toggle).toHaveAttribute('aria-checked', 'false');
  await user.click(toggle);
  await user.click(screen.getByRole('button', { name: '保存' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法保存推送');
  expect(toggle).toHaveAttribute('aria-checked', 'true');
  await user.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存' })).toBeDisabled());
  expect(calls.filter(call => call.method === 'PUT').at(-1)?.body).toEqual({
    ...push.settings, revision: undefined, expectedRevision: 2, enabled: true,
  });
});

it('场景 CG 选择辅助模型后即可保存，保存保留最新角色资料', async () => {
  let reads = 0;
  let savedRole = role({ id: 'r1', name: '爱丽丝', agentConfig: { novelai: { ...defaultNovelaiRoleConfig }, enabledTools: ['read_file'] } });
  const onSaved = vi.fn();
  const { calls } = runtimeBackend({
    'GET /api/roles/r1/autonomy/settings': () => ({ body: push }),
    'GET /api/roles/r1/desktop-pet': () => ({ body: pet }),
    'GET /api/roles/r1': () => {
      if (++reads >= 3) savedRole = { ...savedRole, description: '其他页面刚更新的简介' };
      return { body: { role: savedRole } };
    },
    'GET /api/services/novelai': () => ({ body: { enabled: true, tokenConfigured: true, credentialCleanupPending: false,
      revision: 4, dailyAttemptLimit: 20, maxPixels: 1048576, maxSteps: 28 } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [{ id: 'cg-model', provider: 'fixture', model: 'scene' }] } }),
    'PUT /api/roles/r1': init => {
      const payload = JSON.parse(String(init?.body));
      savedRole = { ...savedRole, agentConfig: { ...savedRole.agentConfig, novelai: { ...defaultNovelaiRoleConfig, ...payload.agentConfig.novelai } } };
      return { body: { role: savedRole } };
    },
  });
  const user = userEvent.setup();
  render(<RoleCapabilitiesPanel role={savedRole} onRoleSaved={onSaved} />);
  await waitFor(() => expect(screen.getByRole('switch', { name: '自动场景 CG' })).toBeEnabled());
  await user.click(screen.getByRole('switch', { name: '自动场景 CG' }));
  const dialog = within(await screen.findByRole('dialog', { name: '自动场景 CG设置' }));
  await dialog.findByText(/账户每日最多 20 次/);
  expect(dialog.queryByLabelText('角色参数')).not.toBeInTheDocument();
  await user.click(dialog.getByLabelText('允许主人回合场景 CG'));
  await user.click(dialog.getByRole('button', { name: '保存角色参数' }));
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(0);
  await user.selectOptions(dialog.getByLabelText('CG 辅助模型连接'), 'cg-model');
  expect(dialog.queryByLabelText(/我确认当前设置的有限生图消费/)).not.toBeInTheDocument();
  await user.click(dialog.getByRole('button', { name: '保存角色参数' }));
  await dialog.findByText('角色参数已保存');
  expect(calls.find(call => call.method === 'PUT')?.body).toMatchObject({ description: '其他页面刚更新的简介',
    agentConfig: { novelai: { sceneCgEnabled: true, scenePromptModelConfigurationId: 'cg-model' } } });
  expect((calls.find(call => call.method === 'PUT')!.body as { agentConfig: { novelai: object } }).agentConfig.novelai)
    .not.toHaveProperty('consentRevision');
  expect(Object.keys((calls.find(call => call.method === 'PUT')!.body as { agentConfig: object }).agentConfig)).toEqual(['novelai']);
  expect(onSaved).toHaveBeenCalledWith(savedRole);
});

it('关闭 CG 配置并切换角色后，迟到的保存结果不会更新当前角色草稿', async () => {
  const source = { ...alice, agentConfig: { novelai: { ...defaultNovelaiRoleConfig, sceneCgEnabled: true,
    scenePromptModelConfigurationId: 'cg-model', consentRevision: 4 } } };
  const bella = role({ id: 'r2', name: '贝拉' });
  let complete!: () => void;
  const saved = new Promise<{ body: { role: typeof source } }>(resolve => { complete = () => resolve({ body: { role: source } }); });
  const onSaved = vi.fn();
  const { calls } = runtimeBackend({
    'GET /api/roles/r[12]/autonomy/settings': () => ({ body: push }),
    'GET /api/roles/r[12]/desktop-pet': () => ({ body: pet }),
    'GET /api/roles/r1': () => ({ body: { role: source } }),
    'GET /api/roles/r2': () => ({ body: { role: bella } }),
    'GET /api/services/novelai': () => ({ body: { enabled: true, tokenConfigured: true, credentialCleanupPending: false,
      revision: 4, dailyAttemptLimit: 20, maxPixels: 1048576, maxSteps: 28 } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [{ id: 'cg-model', provider: 'fixture', model: 'scene' }] } }),
    'PUT /api/roles/r1': () => saved,
  });
  const user = userEvent.setup();
  const page = render(<RoleCapabilitiesPanel role={source} onRoleSaved={onSaved} />);
  await waitFor(() => expect(screen.getByRole('button', { name: '自动场景 CG设置' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: '自动场景 CG设置' }));
  const dialog = within(screen.getByRole('dialog'));
  await dialog.findByText(/账户每日最多/);
  await user.type(dialog.getByLabelText('角色 CG 补充要求'), '柔和的光线');
  await user.click(dialog.getByRole('button', { name: '保存角色参数' }));
  await waitFor(() => expect(calls.some(call => call.method === 'PUT')).toBe(true));
  await user.click(dialog.getByRole('button', { name: '关闭' }));
  page.rerender(<RoleCapabilitiesPanel role={bella} onRoleSaved={onSaved} />);
  await act(async () => { complete(); await saved; });
  expect(onSaved).not.toHaveBeenCalled();
  expect(screen.getByRole('switch', { name: '自动场景 CG' })).toHaveAttribute('aria-checked', 'false');
});
