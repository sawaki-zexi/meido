import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { fakeBackend, message, role, sse } from '../../test/fakeBackend';

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('shows only identity in the header, a recent message in the list, and inserts emoji at the caret before sending', async () => {
  const alice = role({ id: 'r1', name: '爱丽丝', description: '这是角色介绍' });
  const reply = message({ id: 'a1', role: 'assistant', content: '晚安，明天见' });
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [alice] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { roleId: 'r1', sessionKey: 'role:r1' }, messages: [reply] } }),
    'POST /api/roles/r1/messages': () => ({ stream: [sse('user_message_accepted', { message: message({ id: 'u2', role: 'user', content: '你好😊呀' }) }), sse('assistant_completed', { message: message({ id: 'a2', role: 'assistant', content: '收到' }) })] }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('heading', { name: '爱丽丝' });
  expect(screen.getByRole('button', { name: '角色资料' }).querySelector('.chat-title')).toHaveTextContent(/^爱丽丝$/);
  expect(await within(screen.getByRole('list', { name: '角色列表' })).findByText('晚安，明天见')).toBeVisible();
  expect(screen.queryByText('这是角色介绍')).toBeNull();
  const input = screen.getByRole('textbox', { name: '消息内容' }) as HTMLTextAreaElement;
  await user.type(input, '你好呀');
  input.setSelectionRange(2, 2);
  await user.click(screen.getByRole('button', { name: '选择表情' }));
  await user.click(screen.getByRole('button', { name: '😊' }));
  expect(input).toHaveValue('你好😊呀');
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: '发送' }));
  await screen.findByText('收到', { selector: '.bubble p' });
  expect(calls.find(call => call.path.endsWith('/messages'))?.body).toEqual({ content: '你好😊呀' });
});

it('renders a persisted process in the reply and explains last request input usage', async () => {
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '爱丽丝' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { roleId: 'r1', sessionKey: 'role:r1' }, messages: [message({ id: 'a1', role: 'assistant', content: '回复正文', metadata: {
      process: [{ type: 'thinking', id: 't1', text: '整理公开资料' }, { type: 'tool', id: 'c1', name: 'web_search', status: 'failed', result: '搜索服务不可用' }],
      contextUsage: { inputTokens: 1200, capacity: 8000, source: 'actual', configurationId: null },
    } })] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText('回复正文', { selector: '.bubble p' });
  await user.click(screen.getByText('思考过程'));
  expect(screen.getByText('整理公开资料')).toBeVisible();
  await user.click(screen.getByText(/1 次工具调用/));
  expect(screen.getByText('搜索服务不可用')).toBeVisible();
  await user.click(screen.getByRole('button', { name: /上下文用量/ }));
  expect(screen.getByText(/最近一次请求/)).toBeVisible();
  expect(screen.getByText(/1,200 \/ 8,000/)).toBeVisible();
});

it('streams thinking into the active reply and retains its process across tool turns and completion', async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  let sequence = 0;
  const emit = (type: string, data: object) => controller.enqueue(new TextEncoder().encode(sse(type, { runId: 'run-1', sequence: ++sequence, ...data })));
  const process = [{ type: 'thinking', id: 't1', text: '查看资料' }, { type: 'tool', id: 'c1', name: 'lookup', status: 'succeeded', result: '已找到' }];
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '爱丽丝' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { roleId: 'r1', sessionKey: 'role:r1' }, messages: [] } }),
    'POST /api/roles/r1/messages': () => {
      emit('user_message_accepted', { message: message({ id: 'u1', role: 'user', content: '查找' }) });
      emit('assistant_generation_started', { messageId: 'a1' });
      emit('assistant_thinking_delta', { messageId: 'a1', delta: '查看资料' });
      return { stream };
    },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('heading', { name: '爱丽丝' });
  await user.type(screen.getByRole('textbox', { name: '消息内容' }), '查找{Enter}');
  expect(await screen.findByText('查看资料')).toBeVisible();
  act(() => {
    emit('assistant_presentation', { process });
    emit('assistant_generation_started', { messageId: 'a2', messageSequence: 4 });
    emit('assistant_thinking_delta', { messageId: 'a2', delta: '整理回复' });
    emit('assistant_presentation', { runId: 'old-run', process: [{ type: 'thinking', id: 'bad', text: '迟到的内容' }] });
  });
  expect(await screen.findByText('整理回复')).toBeVisible();
  expect(screen.queryByText('迟到的内容')).toBeNull();
  await user.click(screen.getByText('思考过程'));
  expect(screen.getByText('查看资料')).toBeVisible();
  act(() => emit('assistant_completed', { message: message({ id: 'a2', role: 'assistant', content: '最终回复', metadata: { process } }) }));
  await screen.findByText('最终回复', { selector: '.bubble p' });
  await user.click(screen.getByText('1 次工具调用'));
  expect(screen.getByText('已找到')).toBeVisible();
  expect(screen.queryByRole('button', { name: '停止回复' })).toBeNull();
});
