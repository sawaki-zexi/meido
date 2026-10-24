import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { AgentToolConfig } from '../../api/types';
import { fakeBackend, role } from '../../test/fakeBackend';
import { NovelaiStudio } from './NovelaiStudio';

const configuration = { enabled: true, manualEnabled: true, manualDailyAttempts: 10, manualConsentRevision: 3,
  tokenConfigured: true, credentialCleanupPending: false, revision: 3, addQualityTags: false, undesiredContentPreset: 0,
  allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated', nsfwEnabled: false, nsfwModel: 'nai-diffusion-4-5-full',
  allowedSamplers: ['k_euler_ancestral'], defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 };
const ownerConfig = { novelai: { manualEnabled: true, manualDailyAttempts: 10, consentRevision: 3 } } as AgentToolConfig;
const image = { id: 'image-one', name: 'one.png', kind: 'image' as const, mediaType: 'image/png', size: 100 };
const record = { generationId: 'gen-one', attemptId: 'attempt-one', parentAttemptId: null, roleId: 'r1', status: 'succeeded',
  parameters: { prompt: 'blue sky', negative_prompt: 'rain', mode: 'txt2img' as const, width: 1216, height: 832, seed: 321,
    model: 'nai-diffusion-4-5-curated', sampler: 'k_euler_ancestral', steps: 20, qualityToggle: true, ucPreset: 2 }, attachments: [image], taskId: 'task-one',
  createdAt: '2026-10-16T04:00:00Z', errorCode: '', consumption: 'possible', deliveryStatus: 'history_only', deliveryMode: 'history_only',
  deliveryMessageId: null, expectedAttachmentId: null };
const page = (generations: unknown[]) => ({ generations, nextCursor: null, usage: { day: '2026-10-16', accountAttempts: 1, manualAttempts: 1 } });

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('shows the selected role history on a stage and reuses parameters without consuming a request', async () => {
  const second = { ...record, generationId: 'gen-second', attemptId: 'attempt-second', parameters: { ...record.parameters, width: 1024, height: 1024, seed: 654 },
    attachments: [{ ...image, id: 'image-second', name: 'second.png' }] };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([record, second]) }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  expect(await screen.findByAltText('one.png')).toBeInTheDocument();
  expect(screen.getByText('1216 × 832')).toBeInTheDocument();
  const details = screen.getByRole('region', { name: '当前作品参数' });
  expect(within(details).getByText('blue sky')).toBeInTheDocument();
  expect(within(details).getByText('rain')).toBeInTheDocument();
  expect(within(details).getByText('自动质量标签')).toBeInTheDocument();
  expect(within(details).getByText('重度（2）')).toBeInTheDocument();
  expect(within(details).getAllByText('文生图')).not.toHaveLength(0);
  await user.click(screen.getByRole('button', { name: '选择生成记录 attempt-second' }));
  expect(await screen.findByAltText('second.png')).toBeInTheDocument();
  expect(screen.getByText('1024 × 1024')).toBeInTheDocument();
  expect(screen.queryByText('1216 × 832')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '复用参数' }));
  expect(screen.getByLabelText('工作台英文正向 tags')).toHaveValue('blue sky');
  expect(screen.getByLabelText('工作台 Seed')).toHaveValue(654);
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
});

it('keeps service preferences in plugin settings while retaining manual parameter entry', async () => {
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([]) }),
  });
  const user = userEvent.setup();
  const openSettings = vi.fn();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" onOpenSettings={openSettings} />);
  await screen.findByText('暂无生图历史。');
  await user.click(screen.getByRole('button', { name: 'NovelAI 设置' }));
  expect(openSettings).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText('工作台英文正向 tags')).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
});

it('opens the current role prompt-tag library from the studio without starting generation', async () => {
  const tag = { id: 'a'.repeat(32), name: '雨景', keywords: ['下雨'], prompt: 'rain', negative_prompt: 'sunny', rating: 'general', enabled: true };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([]) }),
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [tag], revision: 1, nextCursor: null } }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByText('暂无生图历史。');
  await user.click(screen.getByRole('button', { name: '管理提示标签' }));
  const library = await screen.findByRole('dialog', { name: '诗织的提示标签库' });
  expect(await within(library).findByRole('region', { name: '本角色提示标签库' })).toBeInTheDocument();
  expect(within(library).getByText('雨景')).toBeInTheDocument();
  await user.click(within(library).getByRole('button', { name: '关闭' }));
  expect(screen.queryByRole('dialog', { name: '诗织的提示标签库' })).not.toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
  expect(calls.some((call) => call.path === '/api/roles/r1/novelai/prompt-tags?limit=50')).toBe(true);
});

it('keeps an img2img source and background flag when reusing historical parameters', async () => {
  const img2img = { ...record, generationId: 'gen-img2img', attemptId: 'attempt-img2img', parameters: {
    ...record.parameters, mode: 'img2img' as const, base_attachment_id: 'base-uploaded-earlier', strength: .62, noise: .18, background: true,
  } };
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([img2img]) }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByAltText('one.png');
  const details = within(screen.getByRole('region', { name: '当前作品参数' }));
  expect(details.getAllByText('图生图')).toHaveLength(2);
  expect(details.getByText('base-uploaded-earlier')).toBeInTheDocument();
  expect(details.getByText('0.62')).toBeInTheDocument();
  expect(details.getByText('0.18')).toBeInTheDocument();
  expect(details.getByText('是')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '复用参数' }));
  expect(screen.getByLabelText('工作台生成模式')).toHaveValue('img2img');
  expect(screen.getByLabelText('工作台本角色底图')).toHaveValue('base-uploaded-earlier');
  expect(screen.getByRole('option', { name: '历史底图（需仍可访问）' })).toBeInTheDocument();
  expect(screen.getByLabelText('后台生成，结果仅保存在历史')).toBeChecked();
});

it('requires fresh consumption consent before regenerating the selected historical image', async () => {
  const regenerated = { ...record, generationId: 'gen-regenerated', attemptId: 'attempt-regenerated', parentAttemptId: record.attemptId,
    parameters: { ...record.parameters, seed: 654 }, attachments: [{ ...image, id: 'image-regenerated', name: 'regenerated.png' }], deliveryMode: 'history_only' };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([record]) }),
    'POST /api/roles/r1/generations/gen-one/regenerate': () => ({ body: { generation: regenerated } }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByAltText('one.png');
  await user.click(screen.getByRole('button', { name: '重新生成' }));
  expect(screen.getByRole('button', { name: '确认并重新生成' })).toBeDisabled();
  expect(screen.getByText('将基于 one.png 再次生成；本次会重新消费一次尝试额度。')).toBeInTheDocument();
  await user.click(screen.getByLabelText('我确认本次重生成可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  expect(await screen.findByAltText('regenerated.png')).toBeInTheDocument();
  const posts = calls.filter((call) => call.method === 'POST');
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({ sourceAttemptId: 'attempt-one', expectedAttachmentId: 'image-one', deliveryMode: 'history_only' });
});

it('clears a pending regeneration source when selecting another history record', async () => {
  const second = { ...record, generationId: 'gen-second', attemptId: 'attempt-second', attachments: [{ ...image, id: 'image-second', name: 'second.png' }] };
  const regenerated = { ...second, generationId: 'gen-regenerated-second', attemptId: 'attempt-regenerated-second', parentAttemptId: second.attemptId,
    attachments: [{ ...image, id: 'image-regenerated-second', name: 'regenerated-second.png' }] };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([record, second]) }),
    'POST /api/roles/r1/generations/gen-second/regenerate': () => ({ body: { generation: regenerated } }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByAltText('one.png');
  await user.click(screen.getByRole('button', { name: '重新生成' }));
  expect(screen.getByText('将基于 one.png 再次生成；本次会重新消费一次尝试额度。')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '选择生成记录 attempt-second' }));
  expect(await screen.findByAltText('second.png')).toBeInTheDocument();
  expect(screen.queryByText('将基于 one.png 再次生成；本次会重新消费一次尝试额度。')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '重新生成' }));
  expect(screen.getByText('将基于 second.png 再次生成；本次会重新消费一次尝试额度。')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '确认并重新生成' })).toBeDisabled();
  await user.click(screen.getByLabelText('我确认本次重生成可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  expect(await screen.findByAltText('regenerated-second.png')).toBeInTheDocument();
  const posts = calls.filter((call) => call.method === 'POST');
  expect(posts).toHaveLength(1);
  expect(posts[0].path).toBe('/api/roles/r1/generations/gen-second/regenerate');
  expect(posts[0].body).toMatchObject({ sourceAttemptId: 'attempt-second', expectedAttachmentId: 'image-second' });
});

it('requires fresh consumption consent after starting another regeneration from the same image', async () => {
  const regenerated = { ...record, generationId: 'gen-regenerated-again', attemptId: 'attempt-regenerated-again', parentAttemptId: record.attemptId,
    attachments: [{ ...image, id: 'image-regenerated-again', name: 'regenerated-again.png' }] };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([record]) }),
    'POST /api/roles/r1/generations/gen-one/regenerate': () => ({ body: { generation: regenerated } }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByAltText('one.png');
  await user.click(screen.getByRole('button', { name: '重新生成' }));
  await user.click(screen.getByLabelText('我确认本次重生成可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  await screen.findByAltText('regenerated-again.png');
  await user.click(screen.getByRole('button', { name: '另开一次生成' }));
  expect(screen.getByRole('button', { name: '确认并重新生成' })).toBeDisabled();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
  await user.click(screen.getByLabelText('我确认本次重生成可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  expect(await screen.findByAltText('regenerated-again.png')).toBeInTheDocument();
  const posts = calls.filter((call) => call.method === 'POST');
  expect(posts).toHaveLength(2);
  expect((posts[0].body as { requestId: string }).requestId).not.toBe((posts[1].body as { requestId: string }).requestId);
});

it('submits the selected parameters once and keeps new work in studio history by default', async () => {
  const generated = { ...record, generationId: 'gen-new', attemptId: 'attempt-new', attachments: [{ ...image, id: 'image-new', name: 'new.png' }] };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([record]) }),
    'POST /api/roles/r1/generations': () => ({ body: { generation: generated } }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByAltText('one.png');
  await user.click(screen.getByRole('button', { name: '复用参数' }));
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  expect(await screen.findByAltText('new.png')).toBeInTheDocument();
  const posts = calls.filter((call) => call.method === 'POST');
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({ expectedRevision: 3, deliveryMode: 'history_only',
    parameters: { prompt: 'blue sky', negative_prompt: 'rain', seed: 321, size_preset: 'landscape' } });
});

it('sends the selected sample count and renders every returned image', async () => {
  const generated = { ...record, generationId: 'gen-multi', attemptId: 'attempt-multi', parameters: { ...record.parameters, samples: 3 },
    attachments: [1, 2, 3].map((index) => ({ ...image, id: `image-multi-${index}`, name: `multi-${index}.png` })) };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([]) }),
    'POST /api/roles/r1/generations': () => ({ body: { generation: generated } }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('工作台英文正向 tags'), 'blue sky');
  await user.selectOptions(screen.getByLabelText('工作台生成张数'), '3');
  await user.click(screen.getByRole('button', { name: '生成 3 张图片' }));
  expect(await screen.findByAltText('multi-1.png')).toBeInTheDocument();
  expect(screen.getByAltText('multi-2.png')).toBeInTheDocument();
  expect(screen.getByAltText('multi-3.png')).toBeInTheDocument();
  expect(screen.getByLabelText('当前作品 3 张图片')).toBeInTheDocument();
  const posts = calls.filter((call) => call.method === 'POST');
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({ parameters: { samples: 3 } });
});

it('starts new work with the service default sample count', async () => {
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: { ...configuration, defaultSamples: 3 } }),
    'GET /api/roles/r1/generations': () => ({ body: page([]) }),
    'POST /api/roles/r1/generations': () => ({ body: { generation: { ...record, generationId: 'gen-default-samples', attemptId: 'attempt-default-samples', parameters: { ...record.parameters, samples: 3 }, attachments: [{ ...image, id: 'image-default-samples', name: 'default-samples.png' }] } } }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByText('暂无生图历史。');
  expect(screen.getByLabelText('工作台生成张数')).toHaveValue('3');
  await user.type(screen.getByLabelText('工作台英文正向 tags'), 'blue sky');
  await user.click(screen.getByRole('button', { name: '生成 3 张图片' }));
  expect(await screen.findByAltText('default-samples.png')).toBeInTheDocument();
  expect(calls.find((call) => call.method === 'POST')?.body).toMatchObject({ parameters: { samples: 3 } });
});

it('rejects a multi-image replacement while keeping the historical slot untouched', async () => {
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([record]) }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByAltText('one.png');
  await user.click(screen.getByRole('button', { name: '重新生成' }));
  await user.selectOptions(screen.getByLabelText('工作台结果交付'), 'replacement');
  await user.selectOptions(screen.getByLabelText('工作台生成张数'), '2');
  expect(screen.getByText('替换单张历史图片时只能生成一张；多张结果请选择仅保存历史。')).toBeInTheDocument();
  await user.click(screen.getByLabelText('我确认本次重生成可能产生费用'));
  await user.click(screen.getByRole('button', { name: '确认并重新生成' }));
  expect(await screen.findByText('替换历史中的单张图片时只能生成一张；请选择仅保存历史，或将生成张数改为 1。')).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
});

it('rechecks a failed request with its original request ID', async () => {
  let submissions = 0;
  const generated = { ...record, generationId: 'gen-retried', attemptId: 'attempt-retried', attachments: [{ ...image, id: 'image-retried', name: 'retried.png' }] };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([]) }),
    'POST /api/roles/r1/generations': () => ++submissions === 1
      ? { status: 503, body: { detail: '本地步骤尚未完成' } }
      : { body: { generation: generated } },
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('工作台英文正向 tags'), 'blue sky');
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  expect(await screen.findByText('本地步骤尚未完成')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '重查本次生成' }));
  expect(await screen.findByAltText('retried.png')).toBeInTheDocument();
  const posts = calls.filter((call) => call.method === 'POST');
  expect(posts).toHaveLength(2);
  expect((posts[0].body as { requestId: string }).requestId).toBe((posts[1].body as { requestId: string }).requestId);
});

it('switches roles without showing the previous role image or using its history', async () => {
  const second = { ...record, generationId: 'gen-two', attemptId: 'attempt-two', roleId: 'r2', attachments: [{ ...image, id: 'image-two', name: 'two.png' }] };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([record]) }),
    'GET /api/roles/r2/generations': () => ({ body: page([second]) }),
  });
  const user = userEvent.setup();
  const onRoleChange = vi.fn();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig }), role({ id: 'r2', name: '另一角色', agentConfig: ownerConfig })]} initialRoleId="r1" onRoleChange={onRoleChange} />);
  expect(await screen.findByAltText('one.png')).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('工作台角色'), 'r2');
  expect(await screen.findByAltText('two.png')).toBeInTheDocument();
  expect(screen.queryByAltText('one.png')).not.toBeInTheDocument();
  expect(onRoleChange).toHaveBeenCalledWith('r2');
  expect(calls.some((call) => call.path === '/api/roles/r2/generations')).toBe(true);
});

it('keeps the generation action disabled and explains the current authorization state', async () => {
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: { ...configuration, enabled: false } }),
    'GET /api/roles/r1/generations': () => ({ body: page([]) }),
  });
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  const form = await screen.findByRole('form', { name: '工作台生成设置' });
  expect(await within(form).findByText('手动生图尚未获准，请先完成独立服务、凭据和有限消费确认。')).toBeInTheDocument();
  expect(within(form).getByRole('button', { name: '生成一张图片' })).toBeDisabled();
  await waitFor(() => expect(screen.getByText('暂无生图历史。')).toBeInTheDocument());
});

it.each([
  ['missing token', { tokenConfigured: false }, '手动生图尚未获准，请先完成独立服务、凭据和有限消费确认。'],
  ['stale consent', { manualConsentRevision: 2 }, '手动生图尚未获准，请先完成独立服务、凭据和有限消费确认。'],
  ['credential cleanup', { credentialCleanupPending: true }, '手动生图尚未获准，请先完成独立服务、凭据和有限消费确认。'],
] as const)('keeps generation disabled when authorization gate is %s', async (_name, override, message) => {
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: { ...configuration, ...override } }),
    'GET /api/roles/r1/generations': () => ({ body: page([]) }),
  });
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  const form = await screen.findByRole('form', { name: '工作台生成设置' });
  expect(within(form).getByText(message)).toBeInTheDocument();
  expect(within(form).getByRole('button', { name: '生成一张图片' })).toBeDisabled();
});

it('shows failed history records on the stage with their no-retry explanation', async () => {
  const failed = { ...record, generationId: 'gen-unknown', attemptId: 'attempt-unknown', status: 'unknown',
    errorCode: 'transport_unknown', attachments: [] };
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page([failed]) }),
  });
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  const stage = screen.getByLabelText('当前作品');
  expect(await within(stage).findByText('消费结果不确定')).toBeInTheDocument();
  expect(within(stage).getByText('远端结果尚不能确认；原请求不会再次发送。')).toBeInTheDocument();
});

it('replaces the loading stage with a readable error when the initial history load fails', async () => {
  fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ status: 503, body: { detail: '历史暂时不可用' } }),
  });
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  expect(await screen.findByText('作品读取失败')).toBeInTheDocument();
  expect(within(screen.getByLabelText('当前作品')).getByText('历史暂时不可用')).toBeInTheDocument();
  expect(within(screen.getByRole('navigation', { name: '历史作品' })).getByText('历史读取失败')).toBeInTheDocument();
  expect(screen.queryByText('正在加载作品…')).not.toBeInTheDocument();
});

it('shows loading states while settings and history are still pending', async () => {
  let releaseConfiguration: ((value: { body: unknown }) => void) | undefined;
  let releaseHistory: ((value: { body: unknown }) => void) | undefined;
  fakeBackend({
    'GET /api/services/novelai': () => new Promise((resolve) => { releaseConfiguration = resolve; }),
    'GET /api/roles/r1/generations': () => new Promise((resolve) => { releaseHistory = resolve; }),
  });
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织', agentConfig: ownerConfig })]} initialRoleId="r1" />);
  expect(screen.getByText('正在加载生图设置…')).toBeInTheDocument();
  expect(screen.getByText('正在加载作品…')).toBeInTheDocument();
  expect(screen.getByRole('navigation', { name: '历史作品' })).toHaveTextContent('正在加载…');
  releaseConfiguration?.({ body: configuration });
  releaseHistory?.({ body: page([]) });
  await screen.findByText('暂无生图历史。');
});
