import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { defaultDriftAuthoring, type AutonomyConfiguration, type AutonomyState, type AutonomyTarget,
  type AutonomyTargetAccount, type AutonomyTargetDirectory } from '../../api/autonomy';
import { fakeBackend } from '../../test/fakeBackend';
import { AutonomySettings } from './AutonomySettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const config: AutonomyConfiguration = { revision: 0, activeDecisionEnabled: false, driftEnabled: false,
  decisionModelConfigurationId: null, driftModelConfigurationId: null, defaultTargetRef: 'desktop', allowedTargetRefs: ['desktop'],
  sourceGrants: [], driftToolGrants: [], shellRuleIds: [], consentRevision: 0, driftAuthoring: defaultDriftAuthoring,
  driftMcpGrants: [], driftReadResourceIds: [], driftAttachmentIds: [] };
const state: AutonomyState = { config, runs: [], acknowledgements: [], cleanupPending: false, humanBusy: false };
const account = (id: string, online: boolean | null = null): AutonomyTargetAccount => ({ accountId: id, channel: 'telegram', displayName: id,
  enabled: true, platformEnabled: true, effectiveEnabled: true, connection: 'connected', online, observedAt: '2026-10-16T15:00:00Z',
  reason: '', supportedKinds: ['known', 'member'], capabilities: { private: true, media: false } });
const owner = (id: string): AutonomyTarget => ({ id, kind: 'private', displayName: id, accountId: 'a1', chatId: id, capabilities: ['private'],
  canSend: true, targetRef: `ref-${id}`, ownerEligible: true, reason: '', selectionObservationId: `observe-${id}` });
const directory = (targets: AutonomyTarget[], revision = 3, nextCursor: string | null = null): AutonomyTargetDirectory => ({ targets,
  nextCursor, coverage: 'known', complete: !nextCursor, observedAt: '2026-10-16T15:01:00Z', total: targets.length,
  directoryRevision: { account: revision, platform: 5 } });
const baseRoutes = {
  'GET /api/roles/r1/autonomy': () => ({ body: state }),
  'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
  'GET /api/roles/r1/autonomy/target-accounts': () => ({ body: { accounts: [account('a1')] } }),
};

it('失效目标和目录错误仍保留原目标，重新观察前不可保存且不回退桌面', async () => {
  const original = { ...state, config: { ...config, allowedTargetRefs: ['ref-old'], defaultTargetRef: 'ref-old' },
    targetGrants: [{ ref: 'ref-old', channel: 'telegram', accountId: 'a1', targetId: 'old', displayName: '原主人私聊', current: false,
      denial: '主人绑定已撤销', online: false }] };
  const { calls } = fakeBackend({ ...baseRoutes, 'GET /api/roles/r1/autonomy': () => ({ body: original }),
    'POST /api/roles/r1/autonomy/target-directory': () => ({ status: 409, body: { detail: '目录观察已过期', complete: false, error: 'stale_directory' } }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await userEvent.click(await screen.findByText('添加渠道目标', { selector: 'summary > span' }));
  await screen.findByRole('option', { name: 'a1 · telegram' });
  expect(screen.getByText(/目标许可已失效：主人绑定已撤销/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存自主设置' })).toBeDisabled();
  await user.selectOptions(screen.getByRole('combobox', { name: '自主发送账号' }), 'a1');
  await user.click(screen.getByRole('button', { name: '读取目标目录' }));
  expect(await screen.findByText(/目录观察已过期；已保存目标仍保留/)).toBeInTheDocument();
  expect(screen.getByRole('combobox', { name: '自主默认目标' })).toHaveValue('ref-old');
  expect(screen.getByRole('checkbox', { name: '保留目标 原主人私聊' })).toBeChecked();
  expect(screen.getByRole('checkbox', { name: '许可目标 当前角色桌面会话' })).not.toBeChecked();
  expect(screen.queryByText('本次目录没有目标。')).not.toBeInTheDocument();
  expect(calls.filter((call) => call.method !== 'GET')).toHaveLength(1);
});

it('桌面计入四个许可上限，移除默认目标后须明确重新选择，成员不能变成私聊', async () => {
  fakeBackend({ ...baseRoutes, 'POST /api/roles/r1/autonomy/target-directory': () => ({ body: directory([
    owner('主人一'), owner('主人二'), owner('主人三'), owner('主人四'),
    { ...owner('成员资料'), kind: 'group_member', ownerEligible: false, canSend: false, selectionObservationId: null, reason: '成员资料不授予私聊许可' },
  ]) }) });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await userEvent.click(await screen.findByText('添加渠道目标', { selector: 'summary > span' }));
  await screen.findByRole('option', { name: 'a1 · telegram' });
  await user.selectOptions(screen.getByRole('combobox', { name: '自主发送账号' }), 'a1');
  expect(within(screen.getByRole('combobox', { name: '目标目录类型' })).queryByRole('option', { name: '好友' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '读取目标目录' }));
  await screen.findByRole('checkbox', { name: '许可目标 主人一' });
  expect(screen.getByRole('checkbox', { name: '许可目标 成员资料' })).toBeDisabled();
  for (const name of ['主人一', '主人二', '主人三']) await user.click(screen.getByRole('checkbox', { name: `许可目标 ${name}` }));
  expect(screen.getByText('已选 4/4')).toBeVisible();
  expect(screen.getByRole('checkbox', { name: '许可目标 主人四' })).toBeDisabled();
  await user.selectOptions(screen.getByRole('combobox', { name: '自主默认目标' }), 'ref-主人一');
  await user.click(screen.getByRole('checkbox', { name: '保留目标 主人一' }));
  expect(screen.getByRole('combobox', { name: '自主默认目标' })).toHaveValue('');
  expect(screen.getByRole('button', { name: '保存自主设置' })).toBeDisabled();
  await user.selectOptions(screen.getByRole('combobox', { name: '自主默认目标' }), 'desktop');
  expect(screen.getByRole('button', { name: '保存自主设置' })).toBeEnabled();
});

it('目录分页与查询绑定，账号A到B再到A后旧页不能污染新观察或保存', async () => {
  let completeOldPage: ((value: { body: AutonomyTargetDirectory }) => void) | undefined;
  let firstQueries = 0;
  const { calls } = fakeBackend({ ...baseRoutes,
    'GET /api/roles/r1/autonomy/target-accounts': () => ({ body: { accounts: [account('a1', true), account('a2', false)] } }),
    'POST /api/roles/r1/autonomy/target-directory': (init) => {
      const query = JSON.parse(String(init?.body));
      if (query.cursor) return new Promise((resolve) => { completeOldPage = resolve; });
      if (query.accountId === 'a2') return { body: directory([{ ...owner('账号B'), accountId: 'a2' }]) };
      return { body: ++firstQueries === 1 ? directory([owner('旧A第一页')], 3, 'old-page') : directory([owner('新A目标')], 7) };
    },
    'PUT /api/roles/r1/autonomy': (init) => {
      const input = JSON.parse(String(init?.body));
      expect(input.targetSelections).toEqual([{ accountId: 'a1', targetId: '新A目标', selectionObservationId: 'observe-新A目标', expectedDirectoryRevision: { account: 7, platform: 5 } }]);
      return { body: { ...state, config: { ...input.config, revision: 1 } } };
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await userEvent.click(await screen.findByText('添加渠道目标', { selector: 'summary > span' }));
  await screen.findByRole('option', { name: 'a1 · telegram' });
  await user.selectOptions(screen.getByRole('combobox', { name: '自主发送账号' }), 'a1');
  expect(screen.getByText('在线')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '读取目标目录' }));
  await user.click(await screen.findByRole('button', { name: '读取下一页目标' }));
  await waitFor(() => expect(completeOldPage).toBeDefined());
  await user.selectOptions(screen.getByRole('combobox', { name: '自主发送账号' }), 'a2');
  expect(screen.getByText('离线')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '读取目标目录' }));
  await screen.findByRole('checkbox', { name: '许可目标 账号B' });
  await user.selectOptions(screen.getByRole('combobox', { name: '自主发送账号' }), 'a1');
  await user.click(screen.getByRole('button', { name: '读取目标目录' }));
  await screen.findByRole('checkbox', { name: '许可目标 新A目标' });
  await act(async () => { completeOldPage!({ body: directory([owner('旧A迟到页')]) }); });
  expect(screen.queryByRole('checkbox', { name: '许可目标 旧A迟到页' })).not.toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: '许可目标 旧A第一页' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('checkbox', { name: '许可目标 新A目标' }));
  await user.click(screen.getByRole('button', { name: '保存自主设置' }));
  await waitFor(() => expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(1));
});

it('角色A到B再到A时旧账号观察不覆盖新角色加载', async () => {
  let finishOld: ((value: { body: { accounts: AutonomyTargetAccount[] } }) => void) | undefined;
  let accountReads = 0;
  fakeBackend({ ...baseRoutes,
    'GET /api/roles/r2/autonomy': () => ({ body: state }),
    'GET /api/roles/r2/autonomy/target-accounts': () => ({ body: { accounts: [account('角色B账号')] } }),
    'GET /api/roles/r1/autonomy/target-accounts': () => ++accountReads === 1
      ? new Promise((resolve) => { finishOld = resolve; }) : { body: { accounts: [account('新A账号')] } },
  });
  const view = render(<AutonomySettings roleId="r1" disabled={false} />);
  await waitFor(() => expect(finishOld).toBeDefined());
  view.rerender(<AutonomySettings roleId="r2" disabled={false} />);
  await screen.findByRole('option', { name: '角色B账号 · telegram' });
  view.rerender(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole('option', { name: '新A账号 · telegram' });
  await act(async () => { finishOld!({ body: { accounts: [account('旧A账号')] } }); });
  expect(screen.queryByRole('option', { name: '旧A账号 · telegram' })).not.toBeInTheDocument();
  expect(screen.getByRole('option', { name: '新A账号 · telegram' })).toBeInTheDocument();
});
