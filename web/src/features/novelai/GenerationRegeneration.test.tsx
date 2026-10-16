import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import type { AgentToolConfig } from '../../api/types';
import { GenerationDrawer } from './GenerationDrawer';

const configuration = { enabled: true, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
  allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler_ancestral'],
  defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 };
const owner = role({ id: 'r1', name: '诗织', agentConfig: { novelai: { manualEnabled: true, manualDailyAttempts: 10, consentRevision: 3 } } as AgentToolConfig });
const old = { generationId: 'gen-172', attemptId: 'attempt-old', parentAttemptId: null, roleId: 'r1', status: 'succeeded',
  parameters: { prompt: 'blue sky', negative_prompt: 'rain', mode: 'txt2img', width: 1216, height: 832, seed: 172,
    model: 'nai-diffusion-4-5-curated', sampler: 'k_euler_ancestral', steps: 20 },
  attachments: [{ id: 'image-one', name: 'first.png', kind: 'image', mediaType: 'image/png', size: 100 },
    { id: 'image-two', name: 'selected.png', kind: 'image', mediaType: 'image/png', size: 100 }],
  taskId: 'task-old', createdAt: '2026-10-16T04:00:00Z', errorCode: '', consumption: 'possible', deliveryStatus: 'sent',
  deliveryMode: 'foreground_reply', deliveryMessageId: 'message-old', expectedAttachmentId: null };
const page = (generations: unknown[] = [old]) => ({ generations, nextCursor: null,
  usage: { day: '2026-10-16', accountAttempts: 1, manualAttempts: 1 } });

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('explains new original-channel media delivery before consent and explicitly keeps history-only results unsent', async () => {
  const channel = { ...old, deliveryMessageId: 'thread:actual-original-group:message' };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([channel]) }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={() => {}} />);
  await user.click(await screen.findByRole('button', { name: '重新生成 selected.png' }));
  expect(screen.getByRole('button', { name: '确认并重新生成' })).toBeDisabled();
  expect(screen.getByText('本次重生成会向原渠道再次发送一条新的媒体消息。')).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
  await user.selectOptions(screen.getByLabelText('重生成交付方式'), 'history_only');
  expect(screen.queryByText('本次重生成会向原渠道再次发送一条新的媒体消息。')).not.toBeInTheDocument();
  expect(screen.getByText('仅保存历史不会向原渠道或会话发送媒体。')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认并重新生成' })).toBeDisabled();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
});

it('can stop a generated background result that is still waiting to publish', async () => {
  const waiting = { ...old, attemptId: 'attempt-waiting', deliveryMode: 'source_completion', deliveryStatus: 'pending' };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([waiting]) }),
    'POST /api/roles/r1/generations/gen-172/cancel': () => ({ body: { generation: { ...waiting, deliveryStatus: 'suppressed' } } }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={() => {}} />);
  const record = await screen.findByRole('article', { name: '生成记录 attempt-waiting' });
  await user.click(within(record).getByRole('button', { name: '停止此生成' }));
  expect(await within(record).findByText('投递：已取消展示，图片保留在历史')).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST').map((call) => call.body)).toEqual([{ attemptId: 'attempt-waiting' }]);
});

it('regenerates only the selected historical image after new-consumption consent and retries the original operation', async () => {
  let submissions = 0;
  const replacement = { ...old, attemptId: 'attempt-new', parentAttemptId: old.attemptId,
    parameters: { ...old.parameters, seed: 173 }, attachments: [{ ...old.attachments[1], id: 'replacement', name: 'new.png' }],
    deliveryMode: 'replacement', expectedAttachmentId: 'image-two' };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page(submissions > 1 ? [replacement, old] : [old]) }),
    'POST /api/roles/r1/generations/gen-172/regenerate': () => (++submissions === 1
      ? { status: 503, body: { detail: '本地响应中断，请重查' } } : { body: { generation: replacement } }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={() => {}} />);
  await user.click(await screen.findByRole('button', { name: '重新生成 selected.png' }));
  expect(screen.getByLabelText('英文正向 tags')).toHaveValue('blue sky');
  expect(screen.getByLabelText('英文负向 tags')).toHaveValue('rain');
  expect(screen.getByLabelText('图片尺寸')).toHaveValue('landscape');
  expect(screen.getByLabelText('生图 Seed')).toHaveValue(null);
  expect(screen.getByText('替换 selected.png；其他图片和附件保持不变。')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认并重新生成' })).toBeDisabled();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
  await user.click(screen.getByLabelText('我确认本次重生成将再次消费一次尝试额度，可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  await screen.findByText('本地响应中断，请重查');
  await user.click(screen.getByRole('button', { name: '重查本次生成' }));
  await screen.findByAltText('new.png');
  const posts = calls.filter((call) => call.method === 'POST');
  expect(posts).toHaveLength(2);
  expect(posts[0].body).toEqual(posts[1].body);
  expect(posts[0].body).toMatchObject({ sourceAttemptId: 'attempt-old', expectedAttachmentId: 'image-two', expectedRevision: 3,
    parameters: { prompt: 'blue sky', size_preset: 'landscape', steps: 20 } });
  expect((posts[0].body as { parameters: object }).parameters).not.toHaveProperty('seed');
  await waitFor(() => expect(screen.getByText('Seed 173', { exact: false })).toBeInTheDocument());
});

it('accepts explicit background work and queries or stops its exact attempt without creating another paid operation', async () => {
  const accepted = { ...old, attemptId: 'attempt-background', status: 'dispatching', attachments: [],
    deliveryMode: 'source_completion', deliveryStatus: 'pending' };
  const stopped = { ...accepted, status: 'unknown', deliveryStatus: 'suppressed', errorCode: 'transport_unknown' };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([]) }),
    'POST /api/roles/r1/generations': () => ({ status: 202, body: { generation: accepted } }),
    'GET /api/roles/r1/generations/gen-172\\?attemptId=attempt-background': () => ({ body: { generation: accepted } }),
    'POST /api/roles/r1/generations/gen-172/cancel': () => ({ body: { generation: stopped } }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={() => {}} />);
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('英文正向 tags'), 'blue sky');
  await user.click(screen.getByLabelText('在后台生成，完成后交付原来源'));
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  expect(await screen.findByText('后台生成已受理。关闭此面板后仍会继续，可在历史查询或停止。')).toBeInTheDocument();
  const record = screen.getByRole('article', { name: '生成记录 attempt-background' });
  await user.click(within(record).getByRole('button', { name: '查询此生成' }));
  expect(calls.some((call) => call.path.endsWith('?attemptId=attempt-background'))).toBe(true);
  await user.click(within(record).getByRole('button', { name: '停止此生成' }));
  expect(await within(record).findByText('消费结果不确定', { exact: false })).toBeInTheDocument();
  expect(within(record).getByText('消费：已出网，可能计费；未确认实际扣费。')).toBeInTheDocument();
  expect(within(record).getByText('投递：已取消展示，图片保留在历史')).toBeInTheDocument();
  const posts = calls.filter((call) => call.method === 'POST');
  expect(posts).toHaveLength(2);
  expect(posts[0].body).toMatchObject({ parameters: { background: true } });
  expect(posts[1].body).toEqual({ attemptId: 'attempt-background' });
});

it('keeps a replacement conflict in history and only uses history-only delivery after an explicit new choice', async () => {
  const conflict = { ...old, attemptId: 'attempt-conflict', parentAttemptId: old.attemptId,
    attachments: [{ ...old.attachments[1], id: 'conflict-image', name: 'conflict.png' }],
    deliveryMode: 'replacement', deliveryStatus: 'history_only', errorCode: 'replacement_conflict' };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page() }),
    'POST /api/roles/r1/generations/gen-172/regenerate': () => ({ body: { generation: conflict } }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={() => {}} />);
  await user.click(await screen.findByRole('button', { name: '重新生成 selected.png' }));
  await user.click(screen.getByLabelText('我确认本次重生成将再次消费一次尝试额度，可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  await screen.findByText('已生成，原消息已改变；新图仅保留在历史，未替换其他附件。');
  expect(screen.getByAltText('conflict.png')).toBeInTheDocument();
  expect(screen.getByAltText('first.png')).toBeInTheDocument();
  expect(screen.getByAltText('selected.png')).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
  await user.click(screen.getByRole('button', { name: '另开一次生成' }));
  expect(screen.getByRole('button', { name: '确认并重新生成' })).toBeDisabled();
  await user.selectOptions(screen.getByLabelText('重生成交付方式'), 'history_only');
  await user.click(screen.getByLabelText('我确认本次重生成将再次消费一次尝试额度，可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  const posts = calls.filter((call) => call.method === 'POST');
  expect(posts).toHaveLength(2);
  expect(posts[1].body).toMatchObject({ deliveryMode: 'history_only' });
  expect((posts[0].body as { requestId: string }).requestId).not.toBe((posts[1].body as { requestId: string }).requestId);
});

it.each(['unmount', 'role-return'] as const)('ignores an old exact-attempt query after %s', async (change) => {
  let finish: ((value: { body: unknown }) => void) | undefined;
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r[12]/generations': () => ({ body: page() }),
    'GET /api/roles/r1/generations/gen-172\\?attemptId=attempt-old': () => new Promise((resolve) => { finish = resolve; }),
  });
  const refresh = vi.fn();
  const view = render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={refresh} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: '查询此生成' }));
  await waitFor(() => expect(finish).toBeDefined());
  if (change === 'unmount') view.unmount();
  else {
    view.rerender(<GenerationDrawer role={{ ...owner, id: 'r2', name: '另一角色' }} onClose={() => {}} onGenerated={refresh} />);
    await screen.findByRole('dialog', { name: '另一角色的生图与历史' });
    view.rerender(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={refresh} />);
    await screen.findByAltText('selected.png');
  }
  await act(async () => { finish?.({ body: { generation: { ...old, attachments: [{ ...old.attachments[0], name: 'stale.png' }] } } }); });
  expect(screen.queryByAltText('stale.png')).not.toBeInTheDocument();
  expect(refresh).not.toHaveBeenCalled();
});

it('observes a background attempt until its image reaches the original source without another POST', async () => {
  const running = { ...old, attemptId: 'attempt-running', status: 'dispatching', attachments: [],
    deliveryMode: 'source_completion', deliveryStatus: 'pending' };
  const finished = { ...running, status: 'succeeded', deliveryStatus: 'sent', attachments: [{ ...old.attachments[0], name: 'background.png' }] };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([running]) }),
    'GET /api/roles/r1/generations/gen-172\\?attemptId=attempt-running': () => ({ body: { generation: finished } }),
  });
  const refresh = vi.fn();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={refresh} />);
  expect(await screen.findByAltText('background.png', {}, { timeout: 3500 })).toBeInTheDocument();
  expect(screen.getByText('投递：已交付原来源')).toBeInTheDocument();
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
});

it('copies actual image parameters without inheriting an old background or scene authorization', async () => {
  const previous = { ...old, parameters: { ...old.parameters, background: true, intent: 'scene_cg', scene_key: 'old-scene' } };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([previous]) }),
    'POST /api/roles/r1/generations/gen-172/regenerate': () => ({ body: { generation: { ...old, attemptId: 'manual-new' } } }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={() => {}} />);
  await user.click(await screen.findByRole('button', { name: '重新生成 selected.png' }));
  expect(screen.getByLabelText('在后台生成，完成后交付原来源')).not.toBeChecked();
  await user.click(screen.getByLabelText('我确认本次重生成将再次消费一次尝试额度，可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  const submitted = (calls.find((call) => call.method === 'POST')!.body as { parameters: object }).parameters;
  expect(submitted).not.toHaveProperty('intent');
  expect(submitted).not.toHaveProperty('scene_key');
  expect(submitted).not.toHaveProperty('seed');
});

it('retains an accepted background attempt when the following history refresh is unavailable', async () => {
  let reads = 0;
  const accepted = { ...old, attemptId: 'attempt-accepted', status: 'dispatching', attachments: [],
    deliveryMode: 'source_completion', deliveryStatus: 'pending' };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => (++reads === 1 ? { body: page([]) } : { status: 503, body: { detail: '暂时无法读取历史' } }),
    'POST /api/roles/r1/generations': () => ({ status: 202, body: { generation: accepted } }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={() => {}} />);
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('英文正向 tags'), 'blue sky');
  await user.click(screen.getByLabelText('在后台生成，完成后交付原来源'));
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  await screen.findByText('暂时无法读取历史');
  expect(screen.getByRole('article', { name: '生成记录 attempt-accepted' })).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
});

it('keeps an explicit stop visible when an earlier exact-attempt query returns late', async () => {
  let finishQuery: ((value: { body: unknown }) => void) | undefined;
  const running = { ...old, status: 'dispatching', attachments: [], deliveryMode: 'source_completion', deliveryStatus: 'pending' };
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([running]) }),
    'GET /api/roles/r1/generations/gen-172\\?attemptId=attempt-old': () => new Promise((resolve) => { finishQuery = resolve; }),
    'POST /api/roles/r1/generations/gen-172/cancel': () => ({ body: { generation: { ...running, status: 'unknown', deliveryStatus: 'suppressed' } } }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={() => {}} />);
  await user.click(await screen.findByRole('button', { name: '查询此生成' }));
  await waitFor(() => expect(finishQuery).toBeDefined());
  await user.click(screen.getByRole('button', { name: '停止此生成' }));
  await screen.findByText('投递：已取消展示，图片保留在历史');
  await act(async () => { finishQuery?.({ body: { generation: running } }); });
  expect(screen.getByText('投递：已取消展示，图片保留在历史')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '停止此生成' })).not.toBeInTheDocument();
});

it('requires choosing a currently allowed model instead of silently displaying a different model for an old image', async () => {
  const unavailable = { ...old, parameters: { ...old.parameters, model: 'nai-diffusion-4-5-full' } };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([unavailable]) }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={() => {}} />);
  await user.click(await screen.findByRole('button', { name: '重新生成 selected.png' }));
  expect(screen.getByLabelText('生图模型')).toHaveValue('');
  expect(screen.getByLabelText('生图模型')).toBeInvalid();
  await user.click(screen.getByLabelText('我确认本次重生成将再次消费一次尝试额度，可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
  await user.selectOptions(screen.getByLabelText('生图模型'), 'nai-diffusion-4-5-curated');
  expect(screen.getByLabelText('生图模型')).toBeValid();
});

it('refreshes the original chat after foreground success even if reading history then fails', async () => {
  let reads = 0;
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => (++reads === 1 ? { body: page([]) } : { status: 503, body: { detail: '历史读取失败' } }),
    'POST /api/roles/r1/generations': () => ({ body: { generation: old } }),
  });
  const refresh = vi.fn();
  const user = userEvent.setup();
  render(<GenerationDrawer role={owner} onClose={() => {}} onGenerated={refresh} />);
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('英文正向 tags'), 'blue sky');
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  await screen.findByText('历史读取失败');
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
});
