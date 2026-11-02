import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import * as streamApi from "../../api/stream";
import type { Message } from "../../api/types";
import { fakeBackend, message, role, sse } from "../../test/fakeBackend";
import { useChat } from "./useChat";

const session = { roleId: 'r1', sessionKey: 'role:r1' };
const alice = role({ id: 'r1', name: 'Alice' });
const run = (overrides: Partial<streamApi.ChatRunSnapshot> = {}): streamApi.ChatRunSnapshot => ({ ...session,
  runId: 'run-first-0-0-0-0', status: 'running', sequence: 0, phase: 'model', stopping: false, tools: [], ...overrides });
const row = (content: string) => message({ id: 'a1', role: 'assistant', content, runId: 'run-first-0-0-0-0', status: 'streaming' });
const feed = () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const cancel = vi.fn();
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; }, cancel });
  return { stream, cancel, emit: (type: string, value: object) => controller.enqueue(new TextEncoder().encode(sse(type, value))) };
};
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('reopens accepted progress and content, detaches on role switch, and rejects late callbacks', async () => {
  const subscriptions: { emit: Parameters<typeof streamApi.subscribeRoleRun>[3]; signal: AbortSignal }[] = [];
  vi.spyOn(streamApi, 'subscribeRoleRun').mockImplementation((_role, _run, _after, emit, signal) => new Promise<void>(resolve => {
    subscriptions.push({ emit, signal }); signal.addEventListener('abort', () => resolve(), { once: true });
  }));
  const active = run({ sequence: 8, phase: 'tool', tools: [{ toolCallId: 'call', toolName: 'list_dir', status: 'running', result: '读取中', droppedUpdates: 0 }] });
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [row('前段')], activeRun: active } }),
    'GET /api/roles/r2/session': () => ({ body: { session: { roleId: 'r2', sessionKey: 'role:r2' }, messages: [] } }),
  });
  const { result, unmount } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  expect(result.current.sending).toBe(true);
  expect(result.current.phase).toBe('tool');
  expect(result.current.toolProgress?.calls[0].result).toBe('读取中');
  await act(async () => { await subscriptions[0].emit({ type: 'assistant_delta', runId: 'run-first-0-0-0-0', sequence: 9, messageId: 'a1', delta: '后段' }); });
  expect(result.current.messages[0].content).toBe('前段后段');
  await act(async () => { await result.current.open(role({ id: 'r2', name: 'Bella' })); });
  expect(subscriptions[0].signal.aborted).toBe(true);
  expect(await subscriptions[0].emit({ type: 'assistant_completed', runId: 'run-first-0-0-0-0', sequence: 10, message: row('迟到') })).toBe(false);
  expect(result.current.roleId).toBe('r2');
  expect(result.current.messages).toEqual([]);
  await act(async () => { await result.current.open(alice); });
  unmount();
  expect(subscriptions[1].signal.aborted).toBe(true);
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('recovers a sequence gap from saved history without appending duplicate deltas or resubmitting', async () => {
  const events = feed();
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [row('A')], activeRun: run({ sequence: 2 }) } }),
    'GET /api/roles/r1/runs/run-first-0-0-0-0/events\\?after=2': () => ({ stream: events.stream }),
    'GET /api/roles/r1/runs/run-first-0-0-0-0': () => ({ body: { session, messages: [row('ABC')], run: run({ sequence: 5 }) } }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  await act(async () => {
    events.emit('assistant_delta', { runId: 'run-first-0-0-0-0', sequence: 5, messageId: 'a1', delta: 'C' });
    events.emit('assistant_delta', { runId: 'run-first-0-0-0-0', sequence: 6, messageId: 'a1', delta: 'D' });
  });
  await waitFor(() => expect(result.current.messages[0].content).toBe('ABCD'));
  expect(calls.filter(call => call.method === 'POST')).toEqual([]);
});

it('restores the process before a tool even when no textual placeholder exists yet', async () => {
  const events = feed();
  const process = [{ type: 'thinking', id: 'tool-group:thinking', text: '查看资料' },
    { type: 'tool', id: 'call', name: 'lookup', status: 'unknown', result: '尚无最终结果' }];
  fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [], activeRun: { ...run({ phase: 'tool' }), process } } }),
    'GET /api/roles/r1/runs/run-first-0-0-0-0/events\\?after=0': () => ({ stream: events.stream }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  expect(result.current.messages[0].metadata?.process).toEqual(process);
  await act(async () => {
    events.emit('assistant_generation_started', { runId: 'run-first-0-0-0-0', sequence: 1, messageId: 'next' });
    events.emit('assistant_thinking_delta', { runId: 'run-first-0-0-0-0', sequence: 2, messageId: 'next', delta: '查看资料' });
  });
  expect(result.current.messages[0].id).toBe('next');
  expect(result.current.messages[0].metadata).toMatchObject({ process, streamThinking: '查看资料' });
});

it('a lost acceptance response resolves its exact identity even when identical text belongs to other runs', async () => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  const accepted = message({ id: 'new-user', role: 'user', content: 'same', runId: 'run-first-0-0-0-0' });
  const saved = message({ ...row('finished'), status: 'completed' });
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [message({ id: 'old-user', role: 'user', content: 'same', runId: 'other' })] } }),
    'POST /api/roles/r1/runs': () => { throw new TypeError('lost response'); },
    'GET /api/roles/r1/runs/run-first-0-0-0-0': () => ({ body: { session, messages: [accepted, saved], run: run({ status: 'completed', phase: 'finished', sequence: 12 }) } }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  await act(async () => { expect(await result.current.send('same')).toBe(true); });
  expect(result.current.messages.map(item => item.id)).toEqual(['new-user', 'a1']);
  expect(result.current.sending).toBe(false);
  expect(calls.filter(call => call.path.endsWith('/session'))).toHaveLength(1);
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(1);
});

it.each(['receipt', 'lost response'])('keeps accepted input while snapshots are busy after %s', async mode => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  let ready = false, reads = 0;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'POST /api/roles/r1/runs': () => {
      if (mode === 'lost response') throw new TypeError('response lost after acceptance');
      return { status: 202, body: { snapshotPending: true, run: run({ sequence: 100 }) } };
    },
    'GET /api/roles/r1/runs/run-first-0-0-0-0': () => {
      reads += 1;
      return ready ? { body: { session, messages: [message({ id: 'accepted-user', role: 'user', content: 'accepted', runId: run().runId }),
        message({ ...row('saved'), status: 'completed', sequence: 2 })], run: run({ status: 'completed', phase: 'finished', sequence: 101 }) } }
        : { status: 503, headers: { 'X-Meido-Run-Id': run().runId, 'Retry-After': '1' }, body: { detail: '回复仍在更新' } };
    },
    'GET /api/roles/r1/runs/run-first-0-0-0-0/events\\?after=0': () => ({ stream: [sse('run_snapshot', run({ sequence: 100 }))] }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  await act(async () => { expect(await result.current.send('accepted')).toBe(true); });
  await waitFor(() => expect(reads).toBeGreaterThan(0));
  expect(result.current.sending).toBe(true);
  await act(async () => { expect(await result.current.send('accepted')).toBe(false); });
  ready = true;
  await waitFor(() => expect(result.current.sending).toBe(false));
  expect(result.current.messages.map(item => item.content)).toEqual(['accepted', 'saved']);
  expect(calls.filter(call => call.method === 'POST').map(call => call.path)).toEqual(['/api/roles/r1/runs']);
  expect(calls.filter(call => call.path.endsWith('/session'))).toHaveLength(1);
});

it.each(['resume', 'switch role'])('retries a busy session snapshot without blocking %s', async mode => {
  let ready = false, reads = 0;
  const events = feed();
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => {
      reads += 1;
      return ready ? { body: { session, messages: [row('saved partial')], activeRun: run() } }
        : { status: 503, headers: { 'Retry-After': '1' }, body: { detail: '回复仍在更新，正在重新同步' } };
    },
    'GET /api/roles/r2/session': () => ({ body: { session: { roleId: 'r2', sessionKey: 'role:r2' }, messages: [] } }),
    'GET /api/roles/r1/runs/run-first-0-0-0-0/events\\?after=0': () => ({ stream: events.stream }),
  });
  const { result } = renderHook(useChat);
  let opening!: Promise<Message[] | null>;
  act(() => { opening = result.current.open(alice); });
  await waitFor(() => expect(reads).toBe(1));
  expect(result.current.opening).toBe(true);
  if (mode === 'switch role') {
    await act(async () => { await result.current.open(role({ id: 'r2', name: 'Bella' })); });
    ready = true;
    await act(async () => { await opening; });
    expect(reads).toBe(1);
    expect(result.current.roleId).toBe('r2');
    expect(result.current.messages).toEqual([]);
  } else {
    ready = true;
    await act(async () => { await opening; });
    expect(result.current.messages[0].content).toBe('saved partial');
    expect(result.current.sending).toBe(true);
    expect(result.current.error).toBe('');
  }
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(0);
});

it('Stop can finish the exact accepted run while the submission response is still missing', async () => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  const saved = message({ ...row('partial'), status: 'failed', metadata: { runtimeReason: 'cancelled' } });
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'POST /api/roles/r1/runs': init => new Promise((_resolve, reject) => init?.signal?.addEventListener('abort', () => reject(new TypeError('aborted')), { once: true })),
    'POST /api/roles/r1/runs/run-first-0-0-0-0/stop': () => ({ body: { session, messages: [saved] } }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  let pending!: Promise<boolean>;
  act(() => { pending = result.current.send('same'); });
  await act(async () => { result.current.stop(); expect(await pending).toBe(true); });
  expect(result.current.sending).toBe(false);
  expect(result.current.messages).toEqual([saved]);
  expect(calls.filter(call => call.path.endsWith('/stop')).map(call => call.path)).toEqual(['/api/roles/r1/runs/run-first-0-0-0-0/stop']);
  expect(calls.some(call => call.path.includes('current'))).toBe(false);
});

it('late Stop completion and old terminal events cannot replace the next run', async () => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce('first-0-0-0-0').mockReturnValueOnce('second-0-0-0-0');
  const old = feed(), next = feed();
  let completeStop!: (value: { body: unknown }) => void;
  const stopped = new Promise<{ body: unknown }>(resolve => { completeStop = resolve; });
  fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'POST /api/roles/r1/runs': init => ({ body: { session, messages: [], run: run({ runId: 'run-' + JSON.parse(String(init?.body)).requestId }) } }),
    'GET /api/roles/r1/runs/run-first-0-0-0-0/events\\?after=0': () => ({ stream: old.stream }),
    'GET /api/roles/r1/runs/run-second-0-0-0-0/events\\?after=0': () => ({ stream: next.stream }),
    'POST /api/roles/r1/runs/run-first-0-0-0-0/stop': () => stopped,
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  await act(async () => { await result.current.send('first'); });
  act(() => { result.current.stop(); });
  await act(async () => { old.emit('assistant_completed', { runId: 'run-first-0-0-0-0', sequence: 1, message: message({ ...row('saved'), status: 'completed' }) }); });
  await waitFor(() => expect(result.current.sending).toBe(false));
  await act(async () => { await result.current.send('second'); });
  await act(async () => {
    next.emit('assistant_generation_started', { runId: 'run-second-0-0-0-0', sequence: 1, messageId: 'second' });
    next.emit('assistant_delta', { runId: 'run-second-0-0-0-0', sequence: 2, messageId: 'second', delta: 'new' });
    next.emit('assistant_completed', { runId: 'run-first-0-0-0-0', sequence: 3, message: row('stale') });
    completeStop({ body: { session, messages: [row('old stop')] } }); await stopped;
  });
  expect(result.current.sending).toBe(true);
  expect(result.current.messages.at(-1)?.content).toBe('new');
});

it('natural segments upsert by durable ID and stay active until the real terminal', async () => {
  const events = feed();
  fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [row('')], activeRun: run() } }),
    'GET /api/roles/r1/runs/run-first-0-0-0-0/events\\?after=0': () => ({ stream: events.stream }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  const first = message({ ...row('one'), status: 'completed' });
  await act(async () => {
    events.emit('assistant_message_committed', { runId: 'run-first-0-0-0-0', sequence: 1, message: first });
    events.emit('assistant_message_committed', { runId: 'run-first-0-0-0-0', sequence: 1, message: first });
  });
  await waitFor(() => expect(result.current.messages).toEqual([first]));
  expect(result.current.sending).toBe(true);
  await act(async () => { events.emit('assistant_completed', { runId: 'run-first-0-0-0-0', sequence: 2,
    message: message({ id: 'two', role: 'assistant', content: 'two', runId: 'run-first-0-0-0-0', sequence: 2 }) }); });
  await waitFor(() => expect(result.current.sending).toBe(false));
  expect(result.current.messages.map(item => item.content)).toEqual(['one', 'two']);
});

it.each([false, true])('ignores an older idle refresh after newer history (obsolete active run: %s)', async obsoleteRun => {
  const subscribe = vi.spyOn(streamApi, 'subscribeRoleRun').mockImplementation((_role, _run, _after, _emit, signal) =>
    new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true })));
  const existing = message({ id: 'existing', role: 'assistant', content: 'saved history' });
  const newest = message({ id: 'background', role: 'assistant', content: 'new background message', sequence: 2 });
  const responses: ((value: { body: unknown }) => void)[] = [];
  let loads = 0;
  fakeBackend({
    'GET /api/roles/r1/session': () => ++loads === 1 ? { body: { session, messages: [existing] } }
      : new Promise<{ body: unknown }>(resolve => { responses.push(resolve); }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  let older!: Promise<void>, newer!: Promise<void>;
  act(() => { older = result.current.refresh(); newer = result.current.refresh(); });
  await act(async () => {
    responses[1]({ body: { session, messages: [existing, newest] } }); await newer;
  });
  expect(result.current.messages).toEqual([existing, newest]);
  await act(async () => {
    responses[0]({ body: { session, messages: [existing], activeRun: obsoleteRun ? run() : null } }); await older;
  });
  expect(result.current.messages).toEqual([existing, newest]);
  expect(result.current.sending).toBe(false);
  expect(subscribe).not.toHaveBeenCalled();
});

it('keeps current history when the newest refresh fails and accepts a later successful refresh', async () => {
  const existing = message({ id: 'existing', role: 'assistant', content: 'saved history' });
  const visible = message({ id: 'visible', role: 'assistant', content: 'already visible', sequence: 2 });
  const newest = message({ id: 'newest', role: 'assistant', content: 'later update', sequence: 3 });
  const responses: ((value: { body: unknown; status?: number }) => void)[] = [];
  let loads = 0;
  fakeBackend({
    'GET /api/roles/r1/session': () => ++loads === 1 ? { body: { session, messages: [existing, visible] } }
      : new Promise<{ body: unknown; status?: number }>(resolve => { responses.push(resolve); }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  let older!: Promise<void>, newer!: Promise<void>, next!: Promise<void>;
  act(() => { older = result.current.refresh(); newer = result.current.refresh(); });
  await act(async () => { responses[1]({ status: 503, body: { detail: 'temporary read failure' } }); await newer; });
  expect(result.current.messages).toEqual([existing, visible]);
  await act(async () => { responses[0]({ body: { session, messages: [existing] } }); await older; });
  expect(result.current.messages).toEqual([existing, visible]);
  act(() => { next = result.current.refresh(); });
  await act(async () => { responses[2]({ body: { session, messages: [existing, visible, newest] } }); await next; });
  expect(result.current.messages).toEqual([existing, visible, newest]);
  expect(result.current.error).toBe('');
});

it('an idle refresh resumes the valid active run and receives its next event', async () => {
  const events = feed();
  let loads = 0;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: ++loads === 1 ? { session, messages: [] }
      : { session, messages: [row('saved partial')], activeRun: run({ sequence: 2 }) } }),
    'GET /api/roles/r1/runs/run-first-0-0-0-0/events\\?after=2': () => ({ stream: events.stream }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  await act(async () => { await result.current.refresh(); });
  expect(result.current.sending).toBe(true);
  expect(result.current.messages[0].content).toBe('saved partial');
  await act(async () => {
    events.emit('assistant_delta', { runId: run().runId, sequence: 3, messageId: 'a1', delta: ' continued' });
  });
  await waitFor(() => expect(result.current.messages[0].content).toBe('saved partial continued'));
  expect(calls.filter(call => call.method === 'POST')).toEqual([]);
});

it.each(['switch role', 'reset', 'send', 'unmount'])('a delayed history refresh cannot revive old state after %s', async action => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  const subscribe = vi.spyOn(streamApi, 'subscribeRoleRun').mockImplementation((_role, _run, _after, _emit, signal) =>
    new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true })));
  const sent = message({ id: 'sent', role: 'assistant', content: 'new completed reply' });
  let respond!: (value: { body: unknown }) => void;
  const delayed = new Promise<{ body: unknown }>(resolve => { respond = resolve; });
  let loads = 0;
  fakeBackend({
    "GET /api/roles/r1/session": () => ++loads === 1 ? { body: { session: { roleId: "r1", sessionKey: "role:r1" }, messages: [] } } : delayed,
    "GET /api/roles/r2/session": () => ({ body: { session: { roleId: "r2", sessionKey: "role:r2" }, messages: [] } }),
    'POST /api/roles/r1/runs': () => ({ body: { session, messages: [sent], run: run({ status: 'completed', phase: 'finished' }) } }),
  });
  const { result, unmount } = renderHook(useChat);
  await act(async () => { await result.current.open(role({ id: "r1", name: "Alice" })); });
  let refreshing!: Promise<void>;
  act(() => { refreshing = result.current.refresh(); });
  if (action === 'switch role') await act(async () => { await result.current.open(role({ id: 'r2', name: 'Bella' })); });
  else if (action === 'send') await act(async () => { expect(await result.current.send('new input')).toBe(true); });
  else if (action === 'reset') act(() => { result.current.reset(); });
  else unmount();
  await act(async () => {
    respond({ body: { session, messages: [message({ id: 'old', role: 'assistant', content: 'old history' })], activeRun: run() } });
    await refreshing;
  });
  expect(result.current.roleId).toBe(action === 'switch role' ? 'r2' : action === 'reset' ? null : 'r1');
  expect(result.current.session?.sessionKey).toBe(action === 'switch role' ? 'role:r2' : action === 'reset' ? undefined : 'role:r1');
  expect(result.current.messages).toEqual(action === 'send' ? [sent] : []);
  expect(result.current.toolProgress).toBeNull();
  expect(result.current.sending).toBe(false);
  expect(subscribe).not.toHaveBeenCalled();
});
