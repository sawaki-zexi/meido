import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend } from '../../test/fakeBackend';
import { NovelaiSettings } from './NovelaiSettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('shows ready configuration and saves parameter changes without another consumption checkbox', async () => {
  let config = { enabled: true, manualEnabled: false, manualConsentRevision: 0,
    tokenConfigured: true, credentialCleanupPending: false, revision: 5,
    allowTxt2Img: true, allowImg2Img: true, allowedModels: ['nai-diffusion-4-5-curated'],
    defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler'], defaultSampler: 'k_euler',
    maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20, manualDailyAttempts: 5 };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: config }),
    'PUT /api/services/novelai': init => {
      const { expectedRevision: _revision, ...values } = JSON.parse(String(init?.body));
      config = { ...config, ...values, revision: config.revision + 1 };
      return { body: config };
    },
  });
  const user = userEvent.setup();
  render(<NovelaiSettings />);
  expect(await screen.findByText('可生图')).toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: '我确认生图消费' })).not.toBeInTheDocument();
  await user.clear(screen.getByLabelText('最多采样步数'));
  await user.type(screen.getByLabelText('最多采样步数'), '12');
  await user.click(screen.getByRole('button', { name: '保存生图设置' }));
  expect(await screen.findByText('NovelAI 生图设置已保存。')).toBeInTheDocument();
  expect(screen.getByText('可生图')).toBeInTheDocument();
  const save = calls.find(item => item.method === 'PUT')!;
  expect(save.body).not.toHaveProperty('confirmManualConsumption');
  expect(save.body).not.toHaveProperty('manualEnabled');
});

it('saves ordinary settings without reading or resubmitting the existing token', async () => {
  let config = { enabled: false, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
    allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated',
    nsfwEnabled: false, nsfwModel: 'nai-diffusion-4-5-full',
    allowedSamplers: ['k_euler_ancestral', 'k_euler'], defaultSampler: 'k_euler_ancestral',
    maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: config }),
    'PUT /api/services/novelai': (init) => {
      const { expectedRevision, ...changes } = JSON.parse(String(init?.body));
      expect(expectedRevision).toBe(3);
      config = { ...config, ...changes, revision: 4 };
      return { body: config };
    },
  });
  const user = userEvent.setup();
  render(<NovelaiSettings />);
  expect(await screen.findByText('独立凭据已配置')).toBeInTheDocument();
  expect(screen.getByLabelText('新的 NovelAI token')).toHaveValue('');
  await user.clear(screen.getByLabelText('最多采样步数'));
  await user.type(screen.getByLabelText('最多采样步数'), '12');
  await user.click(screen.getByRole('button', { name: '保存生图设置' }));
  expect(await screen.findByText(/NovelAI 生图设置已保存/)).toBeInTheDocument();
  const saves = calls.filter((item) => item.method !== 'GET');
  expect(saves).toHaveLength(1);
  expect(saves[0].body).not.toHaveProperty('token');
  expect(saves[0].body).not.toHaveProperty('enabled');
  expect(calls.every((item) => !item.path.includes('generate'))).toBe(true);
});

it('saves prompt-quality options and reloads the owner-selected values', async () => {
  let config = { enabled: false, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
    allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated',
    nsfwEnabled: false, nsfwModel: 'nai-diffusion-4-5-full',
    allowedSamplers: ['k_euler_ancestral', 'k_euler'], defaultSampler: 'k_euler_ancestral',
    maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20, addQualityTags: false, undesiredContentPreset: 0 };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: config }),
    'PUT /api/services/novelai': (init) => {
      const { expectedRevision, ...changes } = JSON.parse(String(init?.body));
      expect(expectedRevision).toBe(config.revision);
      config = { ...config, ...changes, revision: config.revision + 1 };
      return { body: config };
    },
  });
  const user = userEvent.setup();
  const first = render(<NovelaiSettings />);
  expect(await screen.findByLabelText('自动添加质量标签')).not.toBeChecked();
  await user.click(screen.getByLabelText('自动添加质量标签'));
  await user.selectOptions(screen.getByLabelText('负向预设'), '2');
  await user.click(screen.getByRole('button', { name: '保存生图设置' }));
  expect(await screen.findByText(/NovelAI 生图设置已保存/)).toBeInTheDocument();
  expect(config.addQualityTags).toBe(true);
  expect(config.undesiredContentPreset).toBe(2);
  first.unmount();

  render(<NovelaiSettings />);
  expect(await screen.findByLabelText('自动添加质量标签')).toBeChecked();
  expect(screen.getByLabelText('负向预设')).toHaveValue('2');
  expect(calls.filter((item) => item.method === 'PUT')).toHaveLength(1);
});

it('enables NSFW default-model mode only after full is explicitly allowed', async () => {
  let config = { enabled: false, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
    allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated',
    nsfwEnabled: false, nsfwModel: 'nai-diffusion-4-5-full',
    allowedSamplers: ['k_euler_ancestral'], defaultSampler: 'k_euler_ancestral',
    maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20, addQualityTags: false, undesiredContentPreset: 0 };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: config }),
    'PUT /api/services/novelai': (init) => {
      const { expectedRevision, ...changes } = JSON.parse(String(init?.body));
      expect(expectedRevision).toBe(config.revision);
      config = { ...config, ...changes, revision: config.revision + 1 };
      return { body: config };
    },
  });
  const user = userEvent.setup();
  render(<NovelaiSettings />);
  expect(await screen.findByLabelText('NSFW 默认模型模式')).toBeDisabled();
  await user.click(screen.getByLabelText('允许 nai-diffusion-4-5-full'));
  expect(screen.getByLabelText('NSFW 默认模型模式')).not.toBeDisabled();
  await user.click(screen.getByLabelText('NSFW 默认模型模式'));
  await user.click(screen.getByRole('button', { name: '保存生图设置' }));
  expect(await screen.findByText(/NovelAI 生图设置已保存/)).toBeInTheDocument();
  expect(config).toMatchObject({ allowedModels: ['nai-diffusion-4-5-curated', 'nai-diffusion-4-5-full'], nsfwEnabled: true,
    nsfwModel: 'nai-diffusion-4-5-full' });
  expect(calls.find((item) => item.method === 'PUT')?.body).toMatchObject({ nsfwEnabled: true, nsfwModel: 'nai-diffusion-4-5-full' });
});

it('persists independent txt2img and img2img mode switches', async () => {
  let config = { enabled: true, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
    allowTxt2Img: true, allowImg2Img: true, allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated',
    nsfwEnabled: false, nsfwModel: 'nai-diffusion-4-5-full', allowedSamplers: ['k_euler_ancestral'], defaultSampler: 'k_euler_ancestral',
    maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20, addQualityTags: false, undesiredContentPreset: 0 };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: config }),
    'PUT /api/services/novelai': (init) => {
      const { expectedRevision, ...changes } = JSON.parse(String(init?.body));
      expect(expectedRevision).toBe(config.revision);
      config = { ...config, ...changes, revision: config.revision + 1 };
      return { body: config };
    },
  });
  const user = userEvent.setup();
  render(<NovelaiSettings />);
  expect(await screen.findByLabelText('允许文生图')).toBeChecked();
  expect(screen.getByLabelText('允许图生图')).toBeChecked();
  await user.click(screen.getByLabelText('允许图生图'));
  await user.click(screen.getByRole('button', { name: '保存生图设置' }));
  expect(await screen.findByText(/NovelAI 生图设置已保存/)).toBeInTheDocument();
  expect(config.allowTxt2Img).toBe(true);
  expect(config.allowImg2Img).toBe(false);
  expect(calls.find((item) => item.method === 'PUT')?.body).toMatchObject({ allowTxt2Img: true, allowImg2Img: false });
});

it('persists the owner-selected default sample count', async () => {
  let config = { enabled: true, tokenConfigured: true, credentialCleanupPending: false, revision: 3,
    allowTxt2Img: true, allowImg2Img: true, allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated',
    nsfwEnabled: false, nsfwModel: 'nai-diffusion-4-5-full', allowedSamplers: ['k_euler_ancestral'], defaultSampler: 'k_euler_ancestral',
    maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20, defaultSamples: 1, addQualityTags: false, undesiredContentPreset: 0 };
  const { calls } = fakeBackend({
    'GET /api/services/novelai': () => ({ body: config }),
    'PUT /api/services/novelai': (init) => {
      const { expectedRevision, ...changes } = JSON.parse(String(init?.body));
      expect(expectedRevision).toBe(config.revision);
      config = { ...config, ...changes, revision: config.revision + 1 };
      return { body: config };
    },
  });
  const user = userEvent.setup();
  render(<NovelaiSettings />);
  await screen.findByLabelText('默认生成张数');
  await user.selectOptions(screen.getByLabelText('默认生成张数'), '3');
  await user.click(screen.getByRole('button', { name: '保存生图设置' }));
  expect(await screen.findByText(/NovelAI 生图设置已保存/)).toBeInTheDocument();
  expect(config.defaultSamples).toBe(3);
  expect(calls.find((item) => item.method === 'PUT')?.body).toMatchObject({ defaultSamples: 3 });
});
