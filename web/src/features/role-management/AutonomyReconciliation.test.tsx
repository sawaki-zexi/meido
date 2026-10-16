import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { defaultDriftAuthoring, type AutonomyConfiguration, type AutonomyDelivery, type AutonomyState } from '../../api/autonomy';
import { fakeBackend } from '../../test/fakeBackend';
import { AutonomySettings } from './AutonomySettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const config: AutonomyConfiguration = { revision: 0, activeDecisionEnabled: false, driftEnabled: false,
  decisionModelConfigurationId: null, driftModelConfigurationId: null, defaultTargetRef: 'desktop', allowedTargetRefs: ['desktop'],
  sourceGrants: [], driftToolGrants: [], shellRuleIds: [], consentRevision: 0, driftAuthoring: defaultDriftAuthoring,
  driftMcpGrants: [], driftReadResourceIds: [], driftAttachmentIds: [] };
const delivery: AutonomyDelivery = { intentId: 'one', status: 'uncertain', messageId: null,
  target: { ref: 'external-owner', channel: 'telegram', accountId: 'a1', targetId: 'owner', displayName: '原主人私聊' },
  attemptId: 'attempt-one', platformMessageId: null, error: '', projectionPending: false, resolutionRevision: 0, resolution: null };
const state: AutonomyState = { config, runs: [], acknowledgements: [], cleanupPending: false, humanBusy: false, deliveries: [delivery] };
const baseRoutes = {
  'GET /api/roles/r1/autonomy': () => ({ body: state }),
  'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
  'GET /api/roles/r1/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
};

it('原意图待恢复但没有attempt时不声称回执已保留，未知结果继续如实显示', async () => {
  fakeBackend({ ...baseRoutes, 'GET /api/roles/r1/autonomy': () => ({ body: {
    ...state, deliveries: [{ ...delivery, projectionPending: true, attemptId: null, status: 'uncertain' }],
  } }) });
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const panel = await screen.findByRole('group', { name: '投递处置 one' });
  expect(within(panel).getByText('结果不确定，不会再次发送')).toBeInTheDocument();
  expect(within(panel).getByText(/原 attempt：尚无本地回执 · 平台消息 ID：尚未确认/)).toBeInTheDocument();
  expect(within(panel).getByText('原意图仍有本地恢复责任；只核对原回执并恢复本地记录，不会重新发送。')).toBeInTheDocument();
  expect(within(panel).queryByText(/原回执已保留/)).not.toBeInTheDocument();
});

it('核对已看到真实sent后，较早处置的迟到unknown响应不能覆盖平台事实', async () => {
  let finish: ((value: { body: AutonomyState }) => void) | undefined;
  let reads = 0;
  fakeBackend({ ...baseRoutes,
    'GET /api/roles/r1/autonomy': () => ({ body: ++reads === 1 ? state : { ...state, deliveries: [{ ...delivery, status: 'sent', platformMessageId: 'real-platform-one' }] } }),
    'POST /api/roles/r1/autonomy/deliveries/one/reconciliation': () => new Promise((resolve) => { finish = resolve; }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const panel = await screen.findByRole('group', { name: '投递处置 one' });
  await user.type(within(panel).getByRole('textbox', { name: '处置说明' }), '继续核对');
  await user.click(within(panel).getByRole('button', { name: '记录主人处置' }));
  await waitFor(() => expect(finish).toBeDefined());
  await user.click(screen.getByRole('button', { name: '刷新自主状态' }));
  expect(await screen.findByText(/已发送，平台回执已确认/)).toBeInTheDocument();
  await act(async () => { finish!({ body: { ...state, deliveries: [{ ...delivery, resolutionRevision: 1,
    resolution: { requestId: 'old-request', decision: 'keep_pending', allowFuture: false, note: '迟到旧未知', reportedAt: 1, revision: 1 } }] } }); });
  expect(screen.getByText(/已发送，平台回执已确认/)).toBeInTheDocument();
  expect(screen.getByText(/real-platform-one/)).toBeInTheDocument();
  expect(screen.queryByText(/迟到旧未知/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '记录主人处置' })).not.toBeInTheDocument();
});

it('409作废原请求并核对状态，主人重新填写才建立新身份，不自动重试', async () => {
  let reads = 0;
  const posted: { requestId: string; expectedResolutionRevision: number }[] = [];
  const latest = { ...state, deliveries: [{ ...delivery, resolutionRevision: 2 }] };
  const { calls } = fakeBackend({ ...baseRoutes,
    'GET /api/roles/r1/autonomy': () => ({ body: ++reads === 1 ? state : latest }),
    'POST /api/roles/r1/autonomy/deliveries/one/reconciliation': (init) => {
      const body = JSON.parse(String(init?.body)); posted.push(body);
      if (posted.length === 1) return { status: 409, body: { detail: '处置版本冲突' } };
      return { body: { ...state, deliveries: [{ ...delivery, resolutionRevision: 3,
        resolution: { ...body, reportedAt: 1, revision: 3 } }] } };
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const panel = await screen.findByRole('group', { name: '投递处置 one' });
  await user.type(within(panel).getByRole('textbox', { name: '处置说明' }), '初次核对');
  await user.click(within(panel).getByRole('button', { name: '记录主人处置' }));
  await within(panel).findByText(/原请求已失效，请刷新核对后重新填写/);
  await waitFor(() => expect(reads).toBe(2));
  expect(posted).toHaveLength(1);
  expect(within(panel).queryByRole('button', { name: '重试原处置请求' })).not.toBeInTheDocument();
  expect(within(panel).getByRole('textbox', { name: '处置说明' })).toBeDisabled();
  await user.click(within(panel).getByRole('button', { name: '重新填写处置' }));
  expect(within(panel).getByRole('button', { name: '记录主人处置' })).toBeDisabled();
  await user.type(within(panel).getByRole('textbox', { name: '处置说明' }), '重新核对继续等待');
  await user.click(within(panel).getByRole('button', { name: '记录主人处置' }));
  await within(panel).findByText(/重新核对继续等待 ·/);
  expect(posted).toHaveLength(2);
  expect(posted[0].requestId).not.toBe(posted[1].requestId);
  expect(posted.map((request) => request.expectedResolutionRevision)).toEqual([0, 2]);
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(2);
});

it('处置说明须非空，继续等待不允许解门，重复点击只有一份原请求', async () => {
  let finish: ((value: { body: AutonomyState }) => void) | undefined;
  let request: Record<string, unknown> | undefined;
  const { calls } = fakeBackend({ ...baseRoutes,
    'POST /api/roles/r1/autonomy/deliveries/one/reconciliation': (init) => {
      request = JSON.parse(String(init?.body));
      return new Promise((resolve) => { finish = resolve; });
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const panel = await screen.findByRole('group', { name: '投递处置 one' });
  await user.type(within(panel).getByRole('textbox', { name: '处置说明' }), '   ');
  expect(within(panel).getByRole('button', { name: '记录主人处置' })).toBeDisabled();
  await user.selectOptions(within(panel).getByRole('combobox', { name: '主人处置决定' }), 'reported_sent');
  await user.click(within(panel).getByRole('checkbox', { name: '允许后续新的自主投递' }));
  await user.selectOptions(within(panel).getByRole('combobox', { name: '主人处置决定' }), 'keep_pending');
  expect(within(panel).getByRole('checkbox', { name: '允许后续新的自主投递' })).not.toBeChecked();
  expect(within(panel).getByRole('checkbox', { name: '允许后续新的自主投递' })).toBeDisabled();
  await user.type(within(panel).getByRole('textbox', { name: '处置说明' }), '保留门继续等待  ');
  await user.dblClick(within(panel).getByRole('button', { name: '记录主人处置' }));
  await waitFor(() => expect(finish).toBeDefined());
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
  expect(request).toMatchObject({ decision: 'keep_pending', allowFuture: false, note: '保留门继续等待' });
  await act(async () => { finish!({ body: { ...state, deliveries: [{ ...delivery, resolutionRevision: 1,
    resolution: { requestId: String(request!.requestId), decision: 'keep_pending', allowFuture: false, note: '保留门继续等待', reportedAt: 1, revision: 1 } }] } }); });
  expect(within(panel).getByRole('button', { name: '记录主人处置' })).toBeDisabled();
  expect(within(panel).getByText(/主人决定：继续等待回执/)).toBeInTheDocument();
});

it('角色A到B再到A，旧处置响应不能进入重新加载的角色', async () => {
  let finish: ((value: { body: AutonomyState }) => void) | undefined;
  let reads = 0;
  fakeBackend({ ...baseRoutes,
    'GET /api/roles/r1/autonomy': () => ({ body: ++reads === 1 ? state : { ...state, deliveries: [{ ...delivery, error: '新A状态' }] } }),
    'GET /api/roles/r2/autonomy': () => ({ body: { ...state, deliveries: [] } }),
    'GET /api/roles/r2/autonomy/target-accounts': () => ({ body: { accounts: [] } }),
    'POST /api/roles/r1/autonomy/deliveries/one/reconciliation': () => new Promise((resolve) => { finish = resolve; }),
  });
  const user = userEvent.setup();
  const view = render(<AutonomySettings roleId="r1" disabled={false} />);
  const panel = await screen.findByRole('group', { name: '投递处置 one' });
  await user.type(within(panel).getByRole('textbox', { name: '处置说明' }), '原A请求');
  await user.click(within(panel).getByRole('button', { name: '记录主人处置' }));
  await waitFor(() => expect(finish).toBeDefined());
  view.rerender(<AutonomySettings roleId="r2" disabled={false} />);
  await waitFor(() => expect(screen.queryByRole('group', { name: '投递处置 one' })).not.toBeInTheDocument());
  view.rerender(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByText('新A状态');
  await act(async () => { finish!({ body: { ...state, deliveries: [{ ...delivery, error: '旧A迟到状态' }] } }); });
  expect(screen.getByText('新A状态')).toBeInTheDocument();
  expect(screen.queryByText('旧A迟到状态')).not.toBeInTheDocument();
});

it('配置版本变化会隔离旧处置结果并保留原请求供核对，不覆盖新状态', async () => {
  let finish: ((value: { body: AutonomyState }) => void) | undefined;
  let reads = 0;
  fakeBackend({ ...baseRoutes,
    'GET /api/roles/r1/autonomy': () => ({ body: ++reads === 1 ? state : { ...state, config: { ...config, revision: 1 } } }),
    'POST /api/roles/r1/autonomy/deliveries/one/reconciliation': () => new Promise((resolve) => { finish = resolve; }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const panel = await screen.findByRole('group', { name: '投递处置 one' });
  await user.type(within(panel).getByRole('textbox', { name: '处置说明' }), '核对原请求');
  await user.click(within(panel).getByRole('button', { name: '记录主人处置' }));
  await waitFor(() => expect(finish).toBeDefined());
  await user.click(screen.getByRole('button', { name: '刷新自主状态' }));
  await screen.findByText(/配置版本已变化/);
  await act(async () => { finish!({ body: { ...state, deliveries: [{ ...delivery, error: '旧配置迟到状态' }] } }); });
  expect(screen.getByText(/配置版本已变化/)).toBeInTheDocument();
  expect(screen.queryByText('旧配置迟到状态')).not.toBeInTheDocument();
  expect(within(panel).getByRole('button', { name: '重试原处置请求' })).toBeInTheDocument();
  expect(within(panel).getByText(/待核对原请求/)).toBeInTheDocument();
});

it('两条处置并发完成时，较早的完整状态快照不能撤掉另一条已确认处置', async () => {
  const waiting = new Map<string, { finish: (value: { body: AutonomyState }) => void; requestId: string }>();
  const second = { ...delivery, intentId: 'two', attemptId: 'attempt-two' };
  fakeBackend({ ...baseRoutes,
    'GET /api/roles/r1/autonomy': () => ({ body: { ...state, deliveries: [delivery, second] } }),
    'POST /api/roles/r1/autonomy/deliveries/(one|two)/reconciliation': (init, match) => new Promise((resolve) => {
      waiting.set(match[1], { finish: resolve, requestId: JSON.parse(String(init?.body)).requestId });
    }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const firstPanel = await screen.findByRole('group', { name: '投递处置 one' });
  const secondPanel = screen.getByRole('group', { name: '投递处置 two' });
  await user.type(within(firstPanel).getByRole('textbox', { name: '处置说明' }), '第一条核对');
  await user.click(within(firstPanel).getByRole('button', { name: '记录主人处置' }));
  await user.type(within(secondPanel).getByRole('textbox', { name: '处置说明' }), '第二条核对');
  await user.click(within(secondPanel).getByRole('button', { name: '记录主人处置' }));
  await waitFor(() => expect(waiting.size).toBe(2));
  const firstResolved: AutonomyDelivery = { ...delivery, resolutionRevision: 1, resolution: { requestId: waiting.get('one')!.requestId,
    decision: 'keep_pending', allowFuture: false, note: '第一条核对', reportedAt: 1, revision: 1 } };
  const secondResolved: AutonomyDelivery = { ...second, resolutionRevision: 1, resolution: { requestId: waiting.get('two')!.requestId,
    decision: 'keep_pending', allowFuture: false, note: '第二条核对', reportedAt: 2, revision: 1 } };
  await act(async () => {
    waiting.get('two')!.finish({ body: { ...state, deliveries: [firstResolved, secondResolved] } });
    waiting.get('one')!.finish({ body: { ...state, deliveries: [firstResolved, second] } });
  });
  expect(within(firstPanel).getByText(/第一条核对 ·/)).toBeInTheDocument();
  expect(within(secondPanel).getByText(/第二条核对 ·/)).toBeInTheDocument();
});
