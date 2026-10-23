import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend } from '../../test/fakeBackend';
import { AdvancedSettings } from './AdvancedSettings';

const values = { maxTokens: 8192, maxIterations: 40, searchEnabled: true, spawnEnabled: true,
  memoryWindow: 40, memoryOptimizerEnabled: true, memoryOptimizerIntervalSeconds: 64800,
  summaryTokenLimit: 2000, compactionRetainedTurns: 2, contextTriggerRatio: .75, contextTargetRatio: .4,
  contextSafetyMarginTokens: 4096, devMode: false };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('展示十三项并串行保存，旧响应不覆盖保存期间的新编辑', async () => {
  const user = userEvent.setup();
  let finish!: () => void;
  let revision = 0;
  let saved = { ...values };
  const { calls } = fakeBackend({
    'GET /api/settings/advanced': () => ({ body: { revision, values: saved } }),
    'PUT /api/settings/advanced': async init => {
      const body = JSON.parse(String(init?.body));
      if (revision === 0) await new Promise<void>(resolve => { finish = resolve; });
      expect(body.expectedRevision).toBe(revision);
      saved = { ...saved, ...body.values }; revision++;
      return { body: { revision, values: saved } };
    },
  });
  render(<AdvancedSettings />);
  await waitFor(() => expect(screen.getByRole('spinbutton', { name: /单次回复长度/ })).toHaveValue(8192));
  expect(screen.getAllByRole('spinbutton')).toHaveLength(9);
  expect(screen.getAllByRole('switch')).toHaveLength(4);
  const input = screen.getByRole('spinbutton', { name: /单次回复长度/ });
  await user.clear(input); await user.type(input, '6000');
  await waitFor(() => expect(finish).toBeTypeOf('function'));
  await user.clear(input); await user.type(input, '7000');
  await act(async () => finish());
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('已保存'));
  expect(input).toHaveValue(7000);
  const writes = calls.filter(call => call.method === 'PUT');
  expect(writes).toHaveLength(2);
  expect(writes[0].body).toMatchObject({ values: { maxTokens: 6000 } });
  expect(writes[1].body).toMatchObject({ expectedRevision: 1, values: { maxTokens: 7000 } });
});

it('结果不明时保留操作 ID，重试原操作后再保存新草稿', async () => {
  const user = userEvent.setup();
  let attempts = 0;
  const { calls } = fakeBackend({
    'GET /api/settings/advanced': () => ({ body: { revision: 0, values } }),
    'PUT /api/settings/advanced': init => {
      if (++attempts === 1) throw new Error('网络中断');
      return { body: { revision: 1, values: { ...values, ...JSON.parse(String(init?.body)).values } } };
    },
  });
  render(<AdvancedSettings />);
  const toggle = screen.getByRole('switch', { name: '创建子任务' });
  await waitFor(() => expect(toggle).toBeEnabled());
  await user.click(toggle);
  await screen.findByRole('alert');
  expect(toggle).not.toBeChecked();
  await user.click(screen.getByRole('button', { name: '重试保存' }));
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('已保存'));
  const writes = calls.filter(call => call.method === 'PUT');
  expect(writes).toHaveLength(2);
  expect(writes[0].body).toEqual(writes[1].body);
});

it('冲突保留数字草稿，主人选择重新读取才放弃', async () => {
  const user = userEvent.setup();
  fakeBackend({
    'GET /api/settings/advanced': () => ({ body: { revision: 0, values } }),
    'PUT /api/settings/advanced': () => ({ status: 409, body: { detail: '其他窗口已修改' } }),
  });
  render(<AdvancedSettings />);
  const input = screen.getByRole('spinbutton', { name: /单次任务最多步数/ });
  await waitFor(() => expect(input).toBeEnabled());
  await user.clear(input); await user.type(input, '12');
  expect(await screen.findByText('其他窗口已修改')).toBeVisible();
  expect(input).toHaveValue(12);
  await user.click(screen.getByRole('button', { name: '放弃草稿并重新读取' }));
  await waitFor(() => expect(input).toHaveValue(40));
});

it('确认写入但回读正文失败时只重读，不重复提交', async () => {
  const user = userEvent.setup();
  let saved = { ...values };
  let revision = 0;
  const { calls } = fakeBackend({
    'GET /api/settings/advanced': () => ({ body: { revision, values: saved } }),
    'PUT /api/settings/advanced': init => {
      saved = { ...saved, ...JSON.parse(String(init?.body)).values }; revision++;
      return { body: { revision, values: saved } };
    },
  });
  const original = globalThis.fetch;
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const response = await original(input, init);
    return init?.method === 'PUT' ? new Response('', { status: 200 }) : response;
  });
  render(<AdvancedSettings />);
  const toggle = screen.getByRole('switch', { name: '创建子任务' });
  await waitFor(() => expect(toggle).toBeEnabled());
  await user.click(toggle);
  await screen.findByRole('alert');
  await user.click(screen.getByRole('button', { name: '重试保存' }));
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('已保存'));
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1);
  expect(calls.filter(call => call.method === 'GET')).toHaveLength(2);
  expect(toggle).not.toBeChecked();
});
