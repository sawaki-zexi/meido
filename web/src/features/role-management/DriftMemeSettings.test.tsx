import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { fakeBackend } from '../../test/fakeBackend';
import { AutonomyManagement as AutonomySettings } from './AutonomyManagement';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const config = { revision: 7, activeDecisionEnabled: false, driftEnabled: false,
  decisionModelConfigurationId: null, driftModelConfigurationId: 'drift-current',
  defaultTargetRef: 'desktop', allowedTargetRefs: ['desktop'], sourceGrants: [],
  driftToolGrants: [], shellRuleIds: [], consentRevision: 0 };
const models = [{ id: 'drift-current', provider: 'local', model: 'explicit-drift' },
  { id: 'drift-next', provider: 'local', model: 'next-drift' },
  { id: 'chat-active', provider: 'local', model: 'chat-only', active: true }];
const state = { config, runs: [], acknowledgements: [], cleanupPending: false, humanBusy: false };

it('旧自主配置默认关闭表情库维护，视觉用途不继承已选 Drift 或聊天连接', async () => {
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: state }),
    'GET /api/model/configurations': () => ({ body: { configurations: models, activeId: 'chat-active' } }),
    'GET /api/roles/r1/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
  });
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await userEvent.click(await screen.findByText('表情库维护', { selector: 'summary > span' }));
  expect(await screen.findByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' })).not.toBeChecked();
  expect(screen.getByRole('combobox', { name: '表情库视觉模型' })).toHaveValue('');
  expect(calls.every((call) => call.method === 'GET')).toBe(true);
});

it('更换 Drift 模型必须重新选择视觉用途，关闭维护仍可保存并继续主人普通编辑', async () => {
  const purpose = { enabled: true, managedEpoch: 'original-host-epoch', revision: 4,
    visionModelConfigurationId: 'drift-current', readEnabled: true, editEnabled: true, copyEnabled: false,
    allowedCategories: ['开心'], sourceAttachmentIds: [] };
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: { ...state, config: { ...config, driftMeme: purpose } } }),
    'GET /api/model/configurations': () => ({ body: { configurations: models } }),
    'GET /api/roles/r1/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
    'PUT /api/roles/r1/autonomy': (init) => {
      const input = JSON.parse(String(init?.body));
      expect(input.config.driftMeme).toMatchObject({ enabled: false, managedEpoch: 'original-host-epoch', revision: 4,
        visionModelConfigurationId: 'drift-next' });
      expect(input.config.driftModelConfigurationId).toBe('drift-current');
      return { body: { ...state, cleanupPending: true, config: { ...input.config, revision: 8 } } };
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await userEvent.click(await screen.findByText('表情库维护', { selector: 'summary > span' }));
  await screen.findByRole('combobox', { name: '表情库视觉模型' });
  await user.selectOptions(screen.getByRole('combobox', { name: 'Drift 模型连接' }), 'drift-next');
  expect(screen.getByRole('combobox', { name: '表情库视觉模型' })).toHaveValue('drift-current');
  expect(screen.getByRole('button', { name: '保存自主设置' })).toBeDisabled();
  await user.selectOptions(screen.getByRole('combobox', { name: '表情库视觉模型' }), 'drift-next');
  expect(screen.getByRole('button', { name: '保存自主设置' })).toBeEnabled();
  await user.selectOptions(screen.getByRole('combobox', { name: 'Drift 模型连接' }), 'drift-current');
  expect(screen.getByRole('button', { name: '保存自主设置' })).toBeDisabled();
  await user.click(screen.getByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' }));
  expect(screen.getByRole('button', { name: '保存自主设置' })).toBeEnabled();
  await user.click(screen.getByRole('button', { name: '保存自主设置' }));
  await waitFor(() => expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(1));
  expect(await screen.findByText(/关闭后，主人仍可按原方式编辑表情库/)).toBeInTheDocument();
  expect(screen.getByText(/重新开启要等待原运行资源收尾/)).toBeInTheDocument();
});

it('主人明确视觉、读编辑复制和登记素材范围后保存，同一角色回显宿主确认配置', async () => {
  const purpose = { enabled: false, managedEpoch: 'host-observed-epoch', revision: 3,
    visionModelConfigurationId: null, readEnabled: false, editEnabled: false, copyEnabled: false,
    allowedCategories: [], sourceAttachmentIds: [] };
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: { ...state, config: { ...config, driftMeme: purpose } } }),
    'GET /api/model/configurations': () => ({ body: { configurations: models } }),
    'GET /api/roles/r1/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
    'PUT /api/roles/r1/autonomy': (init) => {
      const input = JSON.parse(String(init?.body));
      expect(input.expectedRevision).toBe(7);
      expect(input.config.driftMeme).toEqual({ enabled: true, managedEpoch: 'host-observed-epoch', revision: 3,
        visionModelConfigurationId: 'drift-current', readEnabled: true, editEnabled: true, copyEnabled: true,
        allowedCategories: ['开心', 'surprise'], sourceAttachmentIds: ['registered-source-1', 'registered-source-2'] });
      expect(input.config.driftToolGrants).toEqual([]);
      expect(input.config.shellRuleIds).toEqual([]);
      return { body: { ...state, config: { ...input.config, revision: 8,
        driftMeme: { ...input.config.driftMeme, managedEpoch: 'server-new-epoch', revision: 4 } } } };
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await userEvent.click(await screen.findByText('表情库维护', { selector: 'summary > span' }));
  await user.click(await screen.findByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' }));
  await user.selectOptions(screen.getByRole('combobox', { name: '表情库视觉模型' }), 'drift-current');
  await user.click(screen.getByRole('checkbox', { name: '查看当前角色表情库图片' }));
  await user.click(screen.getByRole('checkbox', { name: '编辑类别、描述和别名' }));
  await user.click(screen.getByRole('checkbox', { name: '复制已登记素材到表情库' }));
  await user.type(screen.getByRole('textbox', { name: '允许维护的类别（每行一个）' }), '开心\nsurprise');
  await user.type(screen.getByRole('textbox', { name: '允许复制的已登记素材 ID（每行一个）' }), 'registered-source-1\nregistered-source-2');
  await user.click(screen.getByRole('button', { name: '保存自主设置' }));
  await waitFor(() => expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(1));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存自主设置' })).toBeEnabled());
  expect(screen.getByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' })).toBeChecked();
  expect(screen.getByRole('textbox', { name: '允许维护的类别（每行一个）' })).toHaveValue('开心\nsurprise');
  expect(screen.getByText(/仅维护当前角色的表情库/)).toBeVisible();
  expect(screen.getByText(/维护不授予图片发送许可/)).toBeVisible();
  expect(screen.queryByText(/host-observed-epoch|server-new-epoch/)).not.toBeInTheDocument();
});

it('类别和素材范围必须有限且有效，主人修正输入后可以保存', async () => {
  const purpose = { enabled: true, managedEpoch: 'host-only', revision: 2,
    visionModelConfigurationId: 'drift-current', readEnabled: true, editEnabled: true, copyEnabled: true,
    allowedCategories: ['开心'], sourceAttachmentIds: [] };
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: { ...state, config: { ...config, driftMeme: purpose } } }),
    'GET /api/model/configurations': () => ({ body: { configurations: models } }),
    'GET /api/roles/r1/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await userEvent.click(await screen.findByText('表情库维护', { selector: 'summary > span' }));
  const categories = await screen.findByRole('textbox', { name: '允许维护的类别（每行一个）' });
  const sources = screen.getByRole('textbox', { name: '允许复制的已登记素材 ID（每行一个）' });
  const save = screen.getByRole('button', { name: '保存自主设置' });
  const replace = async (field: HTMLElement, text: string) => { await user.clear(field); await user.click(field); await user.paste(text); };
  await replace(categories, '../其他角色');
  expect(save).toBeDisabled();
  await user.click(screen.getByText('表情库维护', { selector: 'summary > span' }));
  expect(categories).not.toBeVisible();
  expect(screen.getByText(/类别须为单个目录名/)).toBeVisible();
  await user.click(screen.getByText('表情库维护', { selector: 'summary > span' }));
  await replace(categories, '开心\n开心');
  expect(save).toBeDisabled();
  expect(screen.getByText(/类别不能重复/)).toBeInTheDocument();
  await replace(categories, Array.from({ length: 33 }, (_, index) => `category-${index}`).join('\n'));
  expect(save).toBeDisabled();
  await replace(categories, '开心');
  await replace(sources, 'registered-one\nregistered-one');
  expect(save).toBeDisabled();
  expect(screen.getByText(/素材 ID 不能重复/)).toBeInTheDocument();
  await replace(sources, Array.from({ length: 17 }, (_, index) => `registered-${index}`).join('\n'));
  expect(save).toBeDisabled();
  await replace(sources, 'registered-one');
  expect(save).toBeEnabled();
  expect(calls.every((call) => call.method === 'GET')).toBe(true);
});

it('原资源未退时显示保存拒绝原因，并保留主人草稿', async () => {
  const purpose = { enabled: false, managedEpoch: 'host-only', revision: 2,
    visionModelConfigurationId: 'drift-current', readEnabled: true, editEnabled: true, copyEnabled: false,
    allowedCategories: ['开心'], sourceAttachmentIds: [] };
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: { ...state, config: { ...config, driftMeme: purpose } } }),
    'GET /api/model/configurations': () => ({ body: { configurations: models } }),
    'GET /api/roles/r1/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
    'PUT /api/roles/r1/autonomy': () => ({ status: 409, body: { detail: '旧维护资源仍在退出，请稍后重新开启' } }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await userEvent.click(await screen.findByText('表情库维护', { selector: 'summary > span' }));
  const panel = await screen.findByRole('group', { name: 'Drift 表情库维护' });
  await user.click(within(panel).getByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' }));
  await user.click(screen.getByRole('button', { name: '保存自主设置' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('旧维护资源仍在退出，请稍后重新开启');
  expect(within(panel).getByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' })).toBeChecked();
  expect(within(panel).getByRole('textbox', { name: '允许维护的类别（每行一个）' })).toHaveValue('开心');
  expect(within(panel).getByText(/重新开启要等待原运行资源收尾/)).toBeVisible();
  expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(1);
});

it.each([
  [422, '所选 Drift 模型不支持图片，请选择支持图片的已保存连接'],
  [503, '保存结果暂未确认，请重新读取自主设置后核对'],
])('宿主返回 %s 时保留许可草稿，主人可重新读取确认且不会自动再提交', async (status, detail) => {
  const purpose = { enabled: false, managedEpoch: 'host-confirmed', revision: 2,
    visionModelConfigurationId: 'drift-current', readEnabled: true, editEnabled: true, copyEnabled: false,
    allowedCategories: ['开心'], sourceAttachmentIds: [] };
  let complete: ((value: { status: number; body: unknown }) => void) | undefined;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: { ...state, config: { ...config, driftMeme: purpose } } }),
    'GET /api/model/configurations': () => ({ body: { configurations: models } }),
    'GET /api/roles/r1/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
    'PUT /api/roles/r1/autonomy': () => new Promise((resolve) => { complete = resolve; }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await userEvent.click(await screen.findByText('表情库维护', { selector: 'summary > span' }));
  await user.click(await screen.findByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' }));
  await user.click(screen.getByRole('button', { name: '保存自主设置' }));
  await waitFor(() => expect(complete).toBeDefined());
  expect(screen.getByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' })).toBeDisabled();
  complete!({ status, body: { detail } });
  expect(await screen.findByRole('alert')).toHaveTextContent(detail);
  expect(screen.getByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' })).toBeChecked();
  await user.click(screen.getByRole('button', { name: '重新读取自主设置' }));
  await waitFor(() => expect(screen.getByRole('checkbox', { name: '允许 Drift 维护当前角色表情库' })).not.toBeChecked());
  expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(1);
});
