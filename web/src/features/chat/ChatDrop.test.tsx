import { act, cleanup, createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role, type Handler } from '../../test/fakeBackend';
import { ChatView } from './ChatView';
import type { ChatState } from './useChat';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const alice = role({ id: 'r1', name: '爱丽丝' });
const attachment = { id: 'att-1', name: 'notes.txt', mediaType: 'text/plain', size: 4, kind: 'file' };
const file = () => new File(['note'], 'notes.txt', { type: 'text/plain' });

function setup(state: Partial<ChatState> = {}, upload: Handler = () => ({ body: attachment })) {
  const { calls } = fakeBackend({
    'GET /api/roles/r1/illustrations': () => ({ body: { role: alice } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
    'GET /api/roles/r1/model-configuration': () => ({ body: { configurationId: null, effectiveConfigurationId: null } }),
    'GET /api/roles/r1/context-status': () => ({ body: { roleId: 'r1', sessionKey: 'role:r1', status: 'ready', inputTokens: 0, capacity: null, source: 'local' } }),
    'POST /api/roles/r1/attachments': upload,
  });
  const send = vi.fn<ChatState['send']>().mockResolvedValue(true);
  const chat: ChatState = { roleId: 'r1', session: { sessionKey: 'role:r1', roleId: 'r1', createdAt: alice.createdAt, updatedAt: alice.updatedAt },
    messages: [], toolProgress: null, phase: null, opening: false, sending: false, stopping: false, error: '',
    setError: vi.fn(), open: vi.fn(), reset: vi.fn(), send, stop: vi.fn(), refresh: vi.fn(), ...state };
  render(<ChatView role={alice} chat={chat} onBack={vi.fn()} onShowProfile={vi.fn()} onShowMemories={vi.fn()} onShowChannels={vi.fn()} />);
  return { calls, send, form: screen.getByRole('form', { name: '聊天输入' }), input: screen.getByRole('textbox', { name: '消息内容' }) };
}

function drag(target: HTMLElement, phase: 'dragOver' | 'drop', types: string[], files: File[] = []) {
  const event = createEvent[phase](target, { dataTransfer: { types, files, getData: () => 'https://example.invalid/text' } });
  fireEvent(target, event);
  return event;
}

it.each([{ types: ['text/plain'] }, { types: ['text/plain', 'text/uri-list'] }, { types: [] }])('leaves non-file types $types to native editing without attachment effects', async ({ types }) => {
  const { calls, send, form, input } = setup();
  await userEvent.setup().type(input, '已有草稿');
  expect(drag(input, 'dragOver', types).defaultPrevented).toBe(false);
  expect(form).not.toHaveClass('is-dragging');
  expect(drag(input, 'drop', types).defaultPrevented).toBe(false);
  // JSDOM does not implement native drop editing; browser QA verifies insertion.
  expect(input).toHaveValue('已有草稿');
  expect(calls.filter(call => call.method !== 'GET')).toEqual([]);
  expect(send).not.toHaveBeenCalled();
});

it.each([{ types: ['Files'] }, { types: ['Files', 'text/plain', 'text/uri-list'] }])('keeps types $types in the file flow even when dragover files are protected', async ({ types }) => {
  const { calls, send, form, input } = setup();
  const user = userEvent.setup();
  await user.type(input, '已有草稿');
  expect(drag(input, 'dragOver', types).defaultPrevented).toBe(true);
  expect(form).toHaveClass('is-dragging');
  expect(calls.filter(call => call.method !== 'GET')).toEqual([]);
  expect(drag(input, 'drop', types, [file()]).defaultPrevented).toBe(true);
  expect(await screen.findByRole('button', { name: '移除 notes.txt' })).toBeVisible();
  expect(form).not.toHaveClass('is-dragging');
  expect(input).toHaveValue('已有草稿');
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(1);
  expect(send).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '发送' }));
  expect(send).toHaveBeenCalledExactlyOnceWith('已有草稿', ['att-1']);
});

it.each(['opening', 'sending'] as const)('blocks file navigation and upload while %s', async state => {
  const { calls, send, form } = setup({ [state]: true });
  expect(drag(form, 'dragOver', ['Files']).defaultPrevented).toBe(true);
  expect(drag(form, 'drop', ['Files'], [file()]).defaultPrevented).toBe(true);
  await act(async () => {});
  expect(form).not.toHaveClass('is-dragging');
  expect(calls.filter(call => call.method !== 'GET')).toEqual([]);
  expect(send).not.toHaveBeenCalled();
});

it('blocks another file drop while the first upload is pending', async () => {
  let finish!: (reply: { body: typeof attachment }) => void;
  const pending = new Promise<{ body: typeof attachment }>(resolve => { finish = resolve; });
  const { calls, send, form } = setup({}, () => pending);
  expect(drag(form, 'drop', ['Files'], [file()]).defaultPrevented).toBe(true);
  await waitFor(() => expect(screen.getByRole('button', { name: '添加附件' })).toBeDisabled());
  expect(drag(form, 'dragOver', ['Files']).defaultPrevented).toBe(true);
  expect(drag(form, 'drop', ['Files'], [file()]).defaultPrevented).toBe(true);
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(1);
  expect(send).not.toHaveBeenCalled();
  await act(async () => { finish({ body: attachment }); });
  expect(await screen.findByRole('button', { name: '移除 notes.txt' })).toBeVisible();
  expect(screen.getByRole('button', { name: '添加附件' })).toBeEnabled();
});
