import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import * as streamApi from '../../api/stream';
import { fakeBackend, message, role, type Reply } from '../../test/fakeBackend';
import { useChat } from './useChat';

const session = { roleId: 'r1', sessionKey: 'role:r1' };
const alice = role({ id: 'r1', name: 'Alice' });
const run: streamApi.ChatRunSnapshot = { ...session, runId: 'run-first-0-0-0-0', status: 'running',
  sequence: 2, phase: 'model', stopping: false, tools: [] };
const saved = message({ id: 'answer', role: 'assistant', content: 'saved', runId: run.runId });
const pendingReply = () => {
  let resolve!: (value: Reply) => void;
  const promise = new Promise<Reply>(done => { resolve = done; });
  return { promise, resolve };
};
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('unknown admission recovers only its exact snapshot and then subscribes from that cursor', async () => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  let emit!: Parameters<typeof streamApi.subscribeRoleRun>[3];
  const subscribe = vi.spyOn(streamApi, 'subscribeRoleRun').mockImplementation((_role, _run, _after, receive, signal) => {
    emit = receive;
    return new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true }));
  });
  let reads = 0, ready = false;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'POST /api/roles/r1/runs': () => ({ status: 503 }),
    'GET /api/roles/r1/runs/run-first-0-0-0-0': () => {
      reads += 1;
      if (!ready) throw new TypeError('unavailable');
      return { body: { session, messages: [saved], run } };
    },
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  let outcome: unknown;
  act(() => { void result.current.send('original', ['attachment']).then(value => { outcome = value; }); });
  await waitFor(() => expect(reads).toBe(1));
  expect(result.current.sending).toBe(true);
  expect(outcome).toBeUndefined();
  expect(subscribe).not.toHaveBeenCalled();
  await act(async () => { expect(await result.current.send('original')).toBe(false); });
  ready = true;
  await waitFor(() => expect(subscribe).toHaveBeenCalledOnce());
  expect(subscribe.mock.calls[0].slice(0, 3)).toEqual(['r1', run.runId, 2]);
  expect(outcome).toBe(true);
  await act(async () => { await emit({ type: 'assistant_completed', runId: run.runId, sequence: 3, message: saved }); });
  expect(result.current.sending).toBe(false);
  expect(result.current.messages).toEqual([saved]);
  expect(calls.filter(call => call.method === 'POST').map(call => call.body))
    .toEqual([{ content: 'original', attachments: ['attachment'], requestId: 'first-0-0-0-0' }]);
});

it.each(['switch role', 'reset', 'unmount'])('detaches an unknown exact read on %s without accepting or rejecting its input', async action => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  const recovery = pendingReply();
  let signal: AbortSignal | null | undefined;
  const subscribe = vi.spyOn(streamApi, 'subscribeRoleRun');
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'GET /api/roles/r2/session': () => ({ body: { session: { roleId: 'r2', sessionKey: 'role:r2' }, messages: [] } }),
    'POST /api/roles/r1/runs': () => { throw new TypeError('lost response'); },
    'GET /api/roles/r1/runs/run-first-0-0-0-0': init => { signal = init?.signal; return recovery.promise; },
  });
  const { result, unmount } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  let outcome: unknown;
  act(() => { void result.current.send('original').then(value => { outcome = value; }); });
  await waitFor(() => expect(calls.some(call => call.path.endsWith(run.runId))).toBe(true));
  if (action === 'switch role') await act(async () => { await result.current.open(role({ id: 'r2', name: 'Bella' })); });
  else if (action === 'reset') act(() => { result.current.reset(); });
  else unmount();
  await waitFor(() => expect(outcome).toBe('detached'));
  expect(signal?.aborted).toBe(true);
  await act(async () => { recovery.resolve({ body: { session, messages: [saved], run } }); await recovery.promise; });
  expect(result.current.messages).toEqual([]);
  expect(result.current.roleId).toBe(action === 'switch role' ? 'r2' : action === 'reset' ? null : 'r1');
  expect(subscribe).not.toHaveBeenCalled();
});

it('detaches a still-pending POST without letting its late receipt recover the old view', async () => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  const submission = pendingReply();
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'GET /api/roles/r2/session': () => ({ body: { session: { roleId: 'r2', sessionKey: 'role:r2' }, messages: [] } }),
    'POST /api/roles/r1/runs': () => submission.promise,
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  let outcome: unknown;
  act(() => { void result.current.send('original').then(value => { outcome = value; }); });
  await act(async () => { await result.current.open(role({ id: 'r2', name: 'Bella' })); });
  expect(outcome).toBe('detached');
  await act(async () => { submission.resolve({ body: { session, messages: [saved], run } }); await submission.promise; });
  expect(result.current.roleId).toBe('r2');
  expect(result.current.messages).toEqual([]);
  expect(calls.filter(call => call.path.includes('/runs/'))).toEqual([]);
});

it('unconfirmed identity and a mismatched session snapshot do not settle unknown admission', async () => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  let reads = 0, ready = false;
  const subscribe = vi.spyOn(streamApi, 'subscribeRoleRun');
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'POST /api/roles/r1/runs': () => { throw new TypeError('lost response'); },
    'GET /api/roles/r1/runs/run-first-0-0-0-0': () => {
      if (++reads === 1) return { status: 503, headers: { 'X-Meido-Run-Id': 'other-run' } };
      return { body: { session: ready ? session : { ...session, sessionKey: 'other-session' }, messages: [saved],
        run: { ...run, status: 'completed', phase: 'finished' } } };
    },
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  let outcome: unknown;
  act(() => { void result.current.send('original').then(value => { outcome = value; }); });
  await waitFor(() => expect(reads).toBe(2));
  expect(outcome).toBeUndefined();
  expect(result.current.messages).toEqual([]);
  expect(result.current.sending).toBe(true);
  expect(subscribe).not.toHaveBeenCalled();
  ready = true;
  await waitFor(() => expect(outcome).toBe(true));
  expect(result.current.messages).toEqual([saved]);
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(1);
});

it.each(['POST 409', 'exact GET 404'])('retains the existing rejection outcome for %s', async rejection => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'POST /api/roles/r1/runs': () => {
      if (rejection === 'POST 409') return { status: 409, body: { detail: 'busy' } };
      throw new TypeError('lost response');
    },
    'GET /api/roles/r1/runs/run-first-0-0-0-0': () => ({ status: 404 }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  await act(async () => { expect(await result.current.send('original')).toBe(false); });
  expect(result.current.sending).toBe(false);
  expect(calls.filter(call => call.path.endsWith(run.runId))).toHaveLength(rejection === 'POST 409' ? 0 : 1);
});

it('Stop failure keeps the unknown identity and its single recovery path; a later Stop ends both', async () => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  const recovery = pendingReply();
  let reads = 0, stops = 0, signal: AbortSignal | null | undefined;
  const subscribe = vi.spyOn(streamApi, 'subscribeRoleRun');
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'POST /api/roles/r1/runs': () => { throw new TypeError('lost response'); },
    'GET /api/roles/r1/runs/run-first-0-0-0-0': init => {
      signal = init?.signal;
      if (++reads === 1) throw new TypeError('unavailable');
      return recovery.promise;
    },
    'POST /api/roles/r1/runs/run-first-0-0-0-0/stop': () => {
      if (++stops === 1) throw new TypeError('lost stop response');
      return { body: { session, messages: [saved] } };
    },
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  let outcome: unknown;
  act(() => { void result.current.send('original').then(value => { outcome = value; }); });
  await waitFor(() => expect(reads).toBe(2));
  await act(async () => { result.current.stop(); });
  expect(result.current.sending).toBe(true);
  expect(result.current.stopping).toBe(false);
  expect(outcome).toBeUndefined();
  expect(subscribe).not.toHaveBeenCalled();
  await act(async () => { result.current.stop(); result.current.stop(); });
  expect(outcome).toBe(true);
  expect(signal?.aborted).toBe(true);
  expect(result.current.sending).toBe(false);
  expect(result.current.messages).toEqual([saved]);
  await act(async () => { recovery.resolve({ status: 503 }); await recovery.promise; });
  expect(reads).toBe(2);
  expect(subscribe).not.toHaveBeenCalled();
  expect(calls.filter(call => call.path.endsWith('/stop')).map(call => call.path))
    .toEqual([`/api/roles/r1/runs/${run.runId}/stop`, `/api/roles/r1/runs/${run.runId}/stop`]);
  expect(calls.filter(call => call.path.endsWith('/runs'))).toHaveLength(1);
});

it('Stop retries the same ID when acceptance is still unknown after an early 404', async () => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('first-0-0-0-0');
  let reads = 0, stops = 0;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'POST /api/roles/r1/runs': () => { throw new TypeError('lost response'); },
    'GET /api/roles/r1/runs/run-first-0-0-0-0': () => { reads += 1; throw new TypeError('unavailable'); },
    'POST /api/roles/r1/runs/run-first-0-0-0-0/stop': () => ++stops === 1 ? { status: 404 }
      : { body: { session, messages: [saved] } },
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(alice); });
  let outcome: unknown;
  act(() => { void result.current.send('original').then(value => { outcome = value; }); });
  await waitFor(() => expect(reads).toBe(1));
  act(() => { result.current.stop(); });
  await waitFor(() => expect(outcome).toBe(true));
  expect(stops).toBe(2);
  expect(result.current.messages).toEqual([saved]);
  expect(result.current.sending).toBe(false);
  expect(calls.filter(call => call.path.endsWith('/runs'))).toHaveLength(1);
});
