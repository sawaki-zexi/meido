import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { useChatContextStatus, type ContextStatus } from './useChatContextStatus';
import { ComposerTools } from './ComposerTools';

const status = (roleId = 'r1', changes: Partial<ContextStatus> = {}): ContextStatus => ({ roleId, sessionKey: `role:${roleId}`, status: 'ready', reason: '',
  inputTokens: 1200, capacity: 24000, source: 'local', model: 'model', configurationId: 'c1', inputLimit: 20000,
  outputReserve: 3000, safetyMargin: 1000, compactTrigger: 18000, compactTarget: 9600, ...changes });
const props = { roleId: 'r1', sessionKey: 'role:r1', enabled: true, sending: false, revision: '', modelKey: 'model' };
const response = (value: ContextStatus) => new Response(JSON.stringify(value));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it('reads without a message, coalesces pending refreshes and never sends a draft', async () => {
  let finish!: (value: Response) => void;
  const fetch = vi.fn((_path: string, _init?: RequestInit) => new Promise<Response>(resolve => { finish = resolve; }));
  vi.stubGlobal('fetch', fetch);
  const hook = renderHook(() => useChatContextStatus(props));
  expect(hook.result.current.loading).toBe(true);
  act(() => { hook.result.current.refresh(); hook.result.current.refresh(); });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0]?.[0]).toBe('/api/roles/r1/context-status');
  await act(async () => finish(response(status())));
  expect(hook.result.current.status?.inputTokens).toBe(1200);
  expect(fetch.mock.calls[0]?.[1]).not.toHaveProperty('body');
});

it('aborts an old role or model and ignores its late response', async () => {
  const requests: { resolve: (response: Response) => void; signal: AbortSignal }[] = [];
  vi.stubGlobal('fetch', vi.fn((_path, init) => new Promise<Response>(resolve => requests.push({ resolve, signal: init.signal }))));
  const hook = renderHook(value => useChatContextStatus(value), { initialProps: props });
  hook.rerender({ ...props, roleId: 'r2', sessionKey: 'role:r2' });
  expect(requests[0].signal.aborted).toBe(true);
  await act(async () => requests[1].resolve(response(status('r2', { inputTokens: 42 }))));
  await act(async () => requests[0].resolve(response(status('r1'))));
  expect(hook.result.current.status?.inputTokens).toBe(42);
  hook.rerender({ ...props, roleId: 'r2', sessionKey: 'role:r2', modelKey: 'changed' });
  expect(hook.result.current.status).toBeNull();
  await act(async () => requests[2].resolve(response(status('r2', { configurationId: 'new' }))));
  expect(hook.result.current.status?.configurationId).toBe('new');
});

it('refreshes after a turn, settings and saved-history changes, reconnect and return', async () => {
  const fetch = vi.fn(async () => response(status()));
  vi.stubGlobal('fetch', fetch);
  const hook = renderHook(value => useChatContextStatus(value), { initialProps: props });
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  hook.rerender({ ...props, sending: true });
  expect(hook.result.current.busy).toBe(true);
  expect(hook.result.current.status?.inputTokens).toBe(1200);
  expect(fetch).toHaveBeenCalledTimes(1);
  hook.rerender(props);
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  for (const revision of ['settings:1', 'message:2']) {
    hook.rerender({ ...props, revision });
    await waitFor(() => expect(hook.result.current.loading).toBe(false));
  }
  expect(fetch).toHaveBeenCalledTimes(4);
  for (const event of ['online', 'focus']) {
    act(() => window.dispatchEvent(new Event(event)));
    await waitFor(() => expect(hook.result.current.loading).toBe(false));
  }
  expect(fetch).toHaveBeenCalledTimes(6);
});

it('bounds the busy handoff retry instead of polling', async () => {
  vi.useFakeTimers();
  const fetch = vi.fn(async () => response(status('r1', { status: 'busy', inputTokens: null, source: null, reason: '正在整理' })));
  vi.stubGlobal('fetch', fetch);
  const hook = renderHook(() => useChatContextStatus(props));
  await act(async () => {});
  expect(hook.result.current.busy).toBe(true);
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(fetch).toHaveBeenCalledTimes(2);
  await act(async () => vi.advanceTimersByTimeAsync(60000));
  expect(fetch).toHaveBeenCalledTimes(2);
});

it('recovers from maintenance that outlasts the handoff using the existing session refresh', async () => {
  vi.useFakeTimers();
  let busy = true;
  const fetch = vi.fn(async () => response(status('r1', busy ? { status: 'busy', inputTokens: null, source: null, reason: '正在整理' } : {})));
  vi.stubGlobal('fetch', fetch);
  const hook = renderHook(value => useChatContextStatus(value), { initialProps: { ...props, sessionSnapshot: {} } });
  await act(async () => {});
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(hook.result.current.busy).toBe(true);
  busy = false;
  // ChatView's existing idle refresh returns a new saved session object.
  hook.rerender({ ...props, sessionSnapshot: {} });
  await act(async () => {});
  expect(hook.result.current.status?.status).toBe('ready');
  expect(fetch).toHaveBeenCalledTimes(3);
  hook.rerender({ ...props, sessionSnapshot: {} });
  await act(async () => {});
  expect(fetch).toHaveBeenCalledTimes(3);
});

it('shows read failures and retries without a fictional percentage', async () => {
  const fetch = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(response(status('r1', { capacity: null, inputLimit: null })));
  vi.stubGlobal('fetch', fetch);
  const hook = renderHook(() => useChatContextStatus(props));
  await waitFor(() => expect(hook.result.current.error).toBeTruthy());
  expect(hook.result.current.status).toBeNull();
  act(() => hook.result.current.refresh());
  await waitFor(() => expect(hook.result.current.status?.capacity).toBeNull());
  expect(hook.result.current.status?.inputTokens).toBe(1200);
});

it('shows only a compact usage tooltip on hover, focus or touch, with no extra action', async () => {
  const refresh = vi.fn();
  render(<ComposerTools onEmoji={() => {}} inspection={{ status: status(), loading: false, busy: false, error: '', refresh }} />);
  const user = userEvent.setup();
  const trigger = screen.getByRole('button', { name: /上下文用量/ });
  await user.hover(trigger);
  expect(refresh).toHaveBeenCalledTimes(1);
  const tooltip = screen.getByRole('tooltip');
  expect(tooltip).toHaveTextContent('上下文 1,200 / 24,000，5%（本地估算）');
  expect(tooltip).toHaveTextContent('当前已保存会话，不含草稿');
  expect(within(tooltip).queryByRole('button')).toBeNull();
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.queryByText(/实际输入限额|重新检查|自动压缩阈值/)).toBeNull();
  await user.unhover(trigger);
  expect(screen.queryByRole('tooltip')).toBeNull();
  trigger.focus();
  await screen.findByRole('tooltip');
  expect(refresh).toHaveBeenCalledTimes(2);
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('tooltip')).toBeNull();
  expect(trigger).toHaveFocus();
  fireEvent.pointerDown(trigger, { pointerType: 'touch' });
  await user.click(trigger);
  expect(screen.getByRole('tooltip')).toBeVisible();
  await user.click(document.body);
  expect(screen.queryByRole('tooltip')).toBeNull();
});

it('keeps same-model usage while busy and clears it immediately for another identity', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(response(status()))
    .mockResolvedValue(response(status('r1', { status: 'busy', inputTokens: null, capacity: null, source: null, configurationId: null, model: null, reason: '正在整理上下文' })));
  vi.stubGlobal('fetch', fetch);
  const hook = renderHook(value => useChatContextStatus(value), { initialProps: props });
  await waitFor(() => expect(hook.result.current.status?.inputTokens).toBe(1200));
  act(() => hook.result.current.refresh());
  await waitFor(() => expect(hook.result.current.busy).toBe(true));
  expect(hook.result.current.status).toMatchObject({ status: 'busy', inputTokens: 1200, capacity: 24000, source: 'local' });
  hook.rerender({ ...props, sending: true, revision: 'new-input' });
  expect(hook.result.current.status?.inputTokens).toBe(1200);
  for (const changed of [{ modelKey: 'other-model' }, { roleId: 'r2', sessionKey: 'role:r2' }, { sessionKey: 'new-session' }]) {
    hook.rerender({ ...props, sending: true, ...changed });
    expect(hook.result.current.status).toBeNull();
  }
  hook.rerender({ ...props, sending: true });
  expect(hook.result.current.status).toBeNull();
});

it('leaves unknown usage unfilled and labels retained busy usage as the previous check', async () => {
  const inspection = { status: status(), loading: false, busy: true, error: '正在回复或整理上下文', refresh: vi.fn() };
  const view = render(<ComposerTools onEmoji={() => {}} inspection={inspection} />);
  const user = userEvent.setup();
  await user.hover(screen.getByRole('button', { name: /上下文用量/ }));
  expect(screen.getByRole('tooltip')).toHaveTextContent('上次用量');
  expect(screen.getByRole('tooltip')).toHaveTextContent('1,200 / 24,000');
  view.rerender(<ComposerTools onEmoji={() => {}} inspection={{ ...inspection, busy: false,
    status: status('r1', { capacity: null, reason: '模型上下文容量未知' }), error: '' }} />);
  expect(screen.getByRole('tooltip')).toHaveTextContent('上下文用量未知');
  expect(screen.getByRole('tooltip')).toHaveTextContent('模型上下文容量未知');
  expect(screen.getByRole('tooltip')).not.toHaveTextContent('%');
  expect(screen.getByRole('button', { name: /上下文用量/ }).querySelector('[stroke-dasharray]')).toBeNull();
});
