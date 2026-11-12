import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { NovelaiConfiguration, PromptTag, PromptTagInput } from '../../api/novelai';
import { fakeBackend, role } from '../../test/fakeBackend';
import { GenerationDrawer } from './GenerationDrawer';
import { NovelaiStudio } from './NovelaiStudio';
import { PromptTagManager } from './PromptTagManager';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const v45 = 'nai-diffusion-4-5-curated';
const v5 = 'nai-diffusion-5-full';
const prompt = '1.2::青い瞳, 蓝色长发::, cafe\u0301, 🐈';
const negative = '[[雨, 暗い空]], blurry';
const configuration: NovelaiConfiguration = {
  enabled: true, revision: 3, tokenConfigured: true, credentialCleanupPending: false,
  allowTxt2Img: true, allowImg2Img: true, allowedModels: [v45, v5], defaultModel: v5,
  nsfwEnabled: false, nsfwModel: v5, allowedSamplers: ['k_euler_ancestral'], defaultSampler: 'k_euler_ancestral',
  maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20, defaultSamples: 1, addQualityTags: false, undesiredContentPreset: 0,
  modelCatalog: [{ id: v45, label: 'V4.5 Curated', supportsUnicode: false }, { id: v5, label: 'V5 Full', supportsUnicode: true }],
};
const generation = {
  generationId: 'generation', attemptId: 'attempt', roleId: 'r1', status: 'succeeded',
  parameters: { prompt, negative_prompt: negative, mode: 'txt2img', model: v5, width: 1024, height: 1024, seed: 123,
    sampler: 'k_euler_ancestral', steps: 28, effective_prompt: prompt, effective_negative_prompt: negative },
  attachments: [{ id: 'image', name: 'unicode.png', kind: 'image', mediaType: 'image/png', size: 100 }],
  createdAt: '2026-11-12T00:00:00Z', errorCode: '', consumption: 'possible', deliveryStatus: 'history_only', deliveryMode: 'history_only',
};
const page = { generations: [], nextCursor: null, usage: { accountAttempts: 0, manualAttempts: 0 } };

it('keeps multilingual studio drafts on V4.5 and restores V5 history prompts for reuse', async () => {
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page }),
    'POST /api/roles/r1/generations': () => ({ body: { generation } }),
  });
  const user = userEvent.setup();
  render(<NovelaiStudio roles={[role({ id: 'r1', name: '角色' })]} initialRoleId="r1" />);
  await screen.findByText('暂无生图历史。');
  fireEvent.change(screen.getByLabelText('工作台正向提示词'), { target: { value: prompt } });
  await user.click(screen.getByRole('tab', { name: '负向提示词' }));
  fireEvent.change(screen.getByLabelText('工作台负向提示词'), { target: { value: negative } });
  await user.click(screen.getByText('高级参数'));
  await user.selectOptions(screen.getByLabelText('工作台生图模型'), v45);
  expect(screen.getByLabelText('工作台英文负向 tags')).toHaveValue(negative);
  expect(screen.getByText(/正向提示词只能使用英文 ASCII/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(0);
  await user.selectOptions(screen.getByLabelText('工作台生图模型'), v5);
  expect(screen.queryByText(/正向提示词只能使用英文 ASCII/)).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  await screen.findByAltText('unicode.png');
  expect(calls.find(call => call.method === 'POST')?.body).toMatchObject({ parameters: { model: v5, prompt, negative_prompt: negative } });
  await user.click(await screen.findByRole('button', { name: '另开一次生成' }));
  fireEvent.change(screen.getByLabelText('工作台负向提示词'), { target: { value: 'new draft' } });
  await user.click(screen.getByRole('button', { name: '复用参数' }));
  expect(screen.getByLabelText('工作台负向提示词')).toHaveValue(negative);
  await user.click(screen.getByRole('tab', { name: '正向提示词' }));
  expect(screen.getByLabelText('工作台正向提示词')).toHaveValue(prompt);
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(1);
});

it('allows 4000 supplementary codepoints in the drawer and retains an excessive draft for correction', async () => {
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: configuration }),
    'GET /api/roles/r1/generations': () => ({ body: page }),
    'POST /api/roles/r1/generations': () => ({ body: { generation } }),
  });
  const user = userEvent.setup();
  render(<GenerationDrawer role={role({ id: 'r1', name: '角色' })} onClose={() => {}} onGenerated={() => {}} />);
  const input = await screen.findByLabelText('正向提示词');
  expect(input).not.toHaveAttribute('maxlength');
  fireEvent.change(input, { target: { value: '🐈'.repeat(4001) } });
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  expect(input).toHaveValue('🐈'.repeat(4001));
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(0);
  fireEvent.change(input, { target: { value: '🐈'.repeat(4000) } });
  fireEvent.change(screen.getByLabelText('负向提示词'), { target: { value: negative } });
  await user.selectOptions(screen.getByLabelText('生图模型'), v45);
  expect(screen.getByLabelText('英文正向 tags')).toHaveValue('🐈'.repeat(4000));
  await user.selectOptions(screen.getByLabelText('生图模型'), v5);
  await user.click(screen.getByRole('button', { name: '生成一张图片' }));
  await screen.findByAltText('unicode.png');
  expect(calls.find(call => call.method === 'POST')?.body).toMatchObject({ parameters: { prompt: '🐈'.repeat(4000), negative_prompt: negative } });
});

it('saves a Unicode tag draft under V4.5 and previews it with the selected V5 model', async () => {
  let saved: PromptTag | undefined;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: saved ? [saved] : [], revision: saved ? 2 : 1, nextCursor: null } }),
    'POST /api/roles/r1/novelai/prompt-tags': (request) => {
      saved = { ...JSON.parse(String(request?.body)) as PromptTagInput, id: 'a'.repeat(32), referenceAttachment: null };
      return { body: { tag: saved, revision: 2 } };
    },
    'POST /api/roles/r1/novelai/prompt-tags/preview': () => ({ body: { prompt, negative_prompt: negative, matchedIds: ['a'.repeat(32)], revision: 2 } }),
  });
  const user = userEvent.setup();
  const view = render(<PromptTagManager roleId="r1" expanded configuration={configuration} model={v45} />);
  await screen.findByText('暂无提示标签。');
  await user.click(screen.getByRole('button', { name: '新建标签' }));
  await user.type(screen.getByLabelText('标签名称'), '雨景');
  await user.type(screen.getByLabelText('匹配关键词（每行一个）'), 'garden');
  const input = screen.getByLabelText('标签正向 tags');
  expect(input).not.toHaveAttribute('maxlength');
  fireEvent.change(input, { target: { value: '🐈'.repeat(4000) } });
  fireEvent.change(screen.getByLabelText('标签负向 tags'), { target: { value: negative } });
  await user.click(screen.getByRole('button', { name: '保存标签' }));
  await screen.findByText('标签已保存');
  expect(saved?.prompt).toBe('🐈'.repeat(4000));
  await user.click(screen.getByRole('button', { name: '返回标签库' }));
  await user.click(screen.getByText('本地匹配预览'));
  await user.type(screen.getByLabelText('预览当前主人输入'), 'garden');
  fireEvent.change(screen.getByLabelText('预览基础正向 tags'), { target: { value: prompt } });
  await user.click(screen.getByRole('button', { name: '预览标签扩写' }));
  expect(screen.getByRole('alert')).toHaveTextContent('ASCII');
  view.rerender(<PromptTagManager roleId="r1" expanded configuration={configuration} model={v5} />);
  await user.click(screen.getByRole('button', { name: '预览标签扩写' }));
  expect(await screen.findByLabelText('标签扩写结果')).toHaveTextContent(prompt);
  expect(calls.find(call => call.path.endsWith('/preview'))?.body).toMatchObject({ model: v5, prompt });
  expect(calls.some(call => call.path.endsWith('/generations'))).toBe(false);
});

it.each([false, true])('ignores a stale preview after model V5–V4.5–V5 switching (failure=%s)', async (failure) => {
  let finish: ((value: { status?: number; body: unknown }) => void) | undefined;
  fakeBackend({
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [], revision: 1, nextCursor: null } }),
    'POST /api/roles/r1/novelai/prompt-tags/preview': () => new Promise(resolve => { finish = resolve; }),
  });
  const user = userEvent.setup();
  const view = render(<PromptTagManager roleId="r1" expanded configuration={configuration} model={v5} />);
  await screen.findByText('暂无提示标签。');
  await user.click(screen.getByText('本地匹配预览'));
  await user.type(screen.getByLabelText('预览当前主人输入'), 'garden');
  await user.click(screen.getByRole('button', { name: '预览标签扩写' }));
  await waitFor(() => expect(finish).toBeDefined());
  view.rerender(<PromptTagManager roleId="r1" expanded configuration={configuration} model={v45} />);
  view.rerender(<PromptTagManager roleId="r1" expanded configuration={configuration} model={v5} />);
  await act(async () => finish?.(failure ? { status: 422, body: { detail: '旧模型预览错误' } }
    : { body: { prompt, negative_prompt: negative, matchedIds: [], revision: 1 } }));
  expect(screen.queryByLabelText('标签扩写结果')).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByLabelText('预览当前主人输入')).toHaveValue('garden');
});
