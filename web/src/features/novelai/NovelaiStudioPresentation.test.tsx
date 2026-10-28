import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { NovelaiStudio } from './NovelaiStudio';

const configuration = { enabled: true, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
  allowedModels: ['nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full'], defaultModel: 'nai-diffusion-4-5-curated',
  nsfwEnabled: false, nsfwModel: 'nai-diffusion-4-5-full', allowedSamplers: ['k_euler_ancestral'],
  defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20,
  addQualityTags: false, undesiredContentPreset: 0, allowTxt2Img: true, allowImg2Img: true };
const routes = {
  'GET /api/services/novelai': () => ({ body: configuration }),
  'GET /api/roles/r1/generations': () => ({ body: { generations: [], nextCursor: null, usage: { accountAttempts: 0, manualAttempts: 0 } } }),
};
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('saves drawing preferences without generating or clearing the prompt and closes with Escape', async () => {
  let current = { ...configuration };
  const { calls } = fakeBackend({ ...routes,
    'GET /api/services/novelai': () => ({ body: current }),
    'PUT /api/services/novelai': (init) => {
      current = { ...current, ...JSON.parse(String(init?.body)), revision: 4 };
      return { body: current };
    },
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织' })]} />);
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('工作台英文正向 tags'), 'garden');
  await user.click(screen.getByRole('button', { name: '绘图偏好' }));
  const preferences = screen.getByRole('dialog', { name: '绘图偏好' });
  await user.click(within(preferences).getByLabelText('自动质量标签'));
  await waitFor(() => expect(within(preferences).getByLabelText('自动质量标签')).toBeChecked());
  expect(calls.filter((call) => call.method === 'PUT').map((call) => call.body)).toEqual([{ addQualityTags: true, expectedRevision: 3 }]);
  expect(calls.some((call) => call.method === 'POST')).toBe(false);
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '绘图偏好' })).toHaveFocus();
  expect(screen.getByLabelText('工作台英文正向 tags')).toHaveValue('garden');
});

it('allows correcting invalid prompts and sizes before any generation request is reserved', async () => {
  const { calls } = fakeBackend(routes);
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织' })]} />);
  await screen.findByText('暂无生图历史。');
  const prompt = screen.getByLabelText('工作台英文正向 tags');
  await user.type(prompt, '花园');
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  expect(await screen.findByText(/正向提示词.*英文 ASCII/)).toBeInTheDocument();
  expect(prompt).toBeEnabled();
  await user.clear(prompt); await user.type(prompt, 'garden');
  await user.selectOptions(screen.getByLabelText('工作台图片尺寸'), 'custom');
  fireEvent.change(screen.getByLabelText('工作台生图宽度'), { target: { value: '65' } });
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  expect(await screen.findByText(/自定义尺寸.*64 的倍数/)).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
  fireEvent.change(screen.getByLabelText('工作台生图宽度'), { target: { value: '64' } });
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  await waitFor(() => expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1));
  expect(calls.find((call) => call.method === 'POST')?.body).toMatchObject({ parameters: { prompt: 'garden', custom_width: 64 } });
});

it('keeps both prompt drafts across tabs and a full-page tag-library visit', async () => {
  fakeBackend({ ...routes, 'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [], revision: 1, nextCursor: null } }) });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织' })]} initialRoleId="r1" />);
  await screen.findByText('暂无生图历史。');
  expect(screen.getByRole('tab', { name: '正向提示词' })).toHaveAttribute('aria-selected', 'true');
  await user.type(screen.getByLabelText('工作台英文正向 tags'), 'garden, sunlight');
  await user.click(screen.getByRole('tab', { name: '负向提示词' }));
  await user.type(screen.getByLabelText('工作台英文负向 tags'), 'rain');
  await user.click(screen.getByRole('button', { name: '管理提示标签' }));
  expect(await screen.findByRole('region', { name: '本角色提示标签库' })).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  const createTag = screen.queryByRole('button', { name: '新建标签' });
  if (createTag) await user.click(createTag);
  await user.type(screen.getByLabelText('标签名称'), '尚未保存的标签');
  await user.click(screen.getByRole('button', { name: '返回图片工作台' }));
  expect(screen.getByLabelText('工作台英文负向 tags')).toHaveValue('rain');
  await user.click(screen.getByRole('tab', { name: '正向提示词' }));
  expect(screen.getByLabelText('工作台英文正向 tags')).toHaveValue('garden, sunlight');
  await user.click(screen.getByRole('button', { name: '管理提示标签' }));
  expect(screen.getByLabelText('标签名称')).toHaveValue('尚未保存的标签');
  await user.click(screen.getByRole('button', { name: '返回图片工作台' }));
  expect(screen.getByLabelText('工作台 Seed')).not.toBeVisible();
  await user.click(screen.getByText('高级参数'));
  expect(screen.getByLabelText('工作台 Seed')).toBeVisible();
});

it('synchronizes the actual model after a preference save loses its response', async () => {
  let current = { ...configuration };
  const { calls } = fakeBackend({ ...routes,
    'GET /api/services/novelai': () => ({ body: current }),
    'PUT /api/services/novelai': () => {
      current = { ...current, nsfwEnabled: true, revision: 4 };
      return { status: 503, body: { detail: '设置响应丢失，请重查' } };
    },
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织' })]} />);
  await screen.findByText('暂无生图历史。');
  await user.type(screen.getByLabelText('工作台英文正向 tags'), 'landscape');
  await user.click(screen.getByRole('button', { name: '绘图偏好' }));
  await user.click(screen.getByLabelText('NSFW 模式'));
  await waitFor(() => expect(screen.getByLabelText('NSFW 模式')).toBeChecked());
  await user.keyboard('{Escape}');
  await user.click(screen.getByText('高级参数'));
  expect(screen.getByLabelText('工作台生图模型')).toHaveValue('nai-diffusion-4-5-full');
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  await waitFor(() => expect(calls.find((call) => call.method === 'POST')?.body).toMatchObject({ expectedRevision: 4, parameters: { prompt: 'landscape', model: 'nai-diffusion-4-5-full' } }));
});

it('shows the uploaded base image and returns to text generation when it is removed', async () => {
  const { calls } = fakeBackend({ ...routes,
    'POST /api/roles/r1/attachments': () => ({ body: { id: 'a'.repeat(32), name: 'base.png', kind: 'image', mediaType: 'image/png', size: 4 } }),
  });
  const user = userEvent.setup(); render(<NovelaiStudio roles={[role({ id: 'r1', name: '诗织' })]} />);
  await screen.findByText('暂无生图历史。');
  await user.upload(screen.getByLabelText('工作台上传生图底图'), new File(['test'], 'base.png', { type: 'image/png' }));
  expect(await screen.findByAltText('生图参考图')).toHaveAttribute('src', '/api/roles/r1/attachments/' + 'a'.repeat(32));
  expect(screen.getByLabelText('工作台生成模式')).toHaveValue('img2img');
  fireEvent.change(screen.getByLabelText('工作台图生图强度'), { target: { value: '0.62' } });
  expect(screen.getByText('0.62')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '移除生图参考图' }));
  expect(screen.queryByAltText('生图参考图')).not.toBeInTheDocument();
  expect(screen.getByLabelText('工作台生成模式')).toHaveValue('txt2img');
  expect(calls.filter(call => call.method === 'POST').map(call => call.path)).toEqual(['/api/roles/r1/attachments']);
});
