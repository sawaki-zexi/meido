import { fakeChatBackend as fakeBackend } from '../../test/fakeChatBackend';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { message, role, sse } from '../../test/fakeBackend';
import { ComposerTools, readContextUsage } from './ComposerTools';
import { ChatView } from './ChatView';
import type { ChatState } from './useChat';

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(['global', 'role'])('clears busy context after %s settings change while preserving the draft', async (change) => {
  const alice = role({ id: 'r1', name: '爱丽丝' });
  let capacity = 64000;
  fakeBackend({
    'GET /api/model/configurations': () => ({ body: { configurations: [{ id: 'old', model: 'old-model' }] } }),
    'GET /api/roles/r1/model-configuration': () => ({ body: { configurationId: null, effectiveConfigurationId: 'old' } }),
    'GET /api/roles/r1/context-status': () => ({ body: { roleId: 'r1', sessionKey: 'role:r1', status: 'ready',
      reason: '', inputTokens: 1200, capacity, source: 'local', model: capacity === 64000 ? 'old-model' : 'new-model' } }),
  });
  const chat: ChatState = { roleId: 'r1', session: { sessionKey: 'role:r1', roleId: 'r1', createdAt: alice.createdAt, updatedAt: alice.updatedAt },
    messages: [], toolProgress: null, phase: null, opening: false, sending: false, stopping: false, error: '',
    setError: vi.fn(), open: vi.fn(), reset: vi.fn(), send: vi.fn(), stop: vi.fn(), refresh: vi.fn() };
  const props = { role: alice, chat, onBack: vi.fn(), onShowProfile: vi.fn(), onShowMemories: vi.fn(), onShowChannels: vi.fn() };
  const view = render(<ChatView {...props} />);
  const user = userEvent.setup();
  const input = screen.getByRole('textbox', { name: '消息内容' });
  await user.type(input, '保留这份草稿');
  await user.hover(screen.getByRole('button', { name: /压缩上下文/ }));
  await waitFor(() => expect(screen.getByRole('tooltip')).toHaveTextContent('64,000'));
  const active = { ...chat, sending: true, messages: [message({ id: 'active', role: 'assistant', content: '', status: 'streaming' })] };
  view.rerender(<ChatView {...props} chat={active} />);
  expect(screen.getByRole('tooltip')).toHaveTextContent('上次用量');
  expect(screen.getByRole('tooltip')).toHaveTextContent('64,000');
  const changed = change === 'global' ? { settingsVersion: 1 } : { role: { ...alice, updatedAt: '2026-11-01T18:00:00Z' } };
  view.rerender(<ChatView {...props} {...changed} chat={active} />);
  expect(screen.getByRole('tooltip')).not.toHaveTextContent('64,000');
  expect(screen.getByRole('tooltip')).toHaveTextContent('上下文用量未知');
  expect(screen.getByRole('textbox', { name: '消息内容' })).toBe(input);
  expect(input).toHaveValue('保留这份草稿');
  capacity = 120000;
  view.rerender(<ChatView {...props} {...changed} chat={{ ...active, sending: false }} />);
  await waitFor(() => expect(screen.getByRole('tooltip')).toHaveTextContent('120,000'));
});

it.each([
  ['actual', '实际'], ['anchor_delta', '锚点估算'], ['local', '本地估算'],
] as const)('shows %s context provenance in the Shiori tooltip', async (source, label) => {
  const usage = readContextUsage({ inputTokens: 1234, capacity: 12000, source, configurationId: 'model' });
  render(<ComposerTools usage={usage} onEmoji={() => {}} />);
  const user = userEvent.setup();
  await user.hover(screen.getByRole('button', { name: /压缩上下文/ }));
  const panel = screen.getByRole('tooltip');
  expect(panel).toHaveTextContent('1,234 / 12,000，10%');
  expect(panel).toHaveTextContent(label);
});

it('shows Shiori typing feedback before the server accepts a message and clears it at completion', async () => {
  let accept!: (value: { stream: string[] }) => void;
  const pending = new Promise<{ stream: string[] }>(resolve => { accept = resolve; });
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '爱丽丝' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { roleId: 'r1', sessionKey: 'role:r1' }, messages: [] } }),
    'CHAT /api/roles/r1': () => pending,
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('heading', { name: '爱丽丝' });
  await user.type(screen.getByRole('textbox', { name: '消息内容' }), '你好{Enter}');
  expect(screen.getByRole('status', { name: '回复状态' })).toHaveTextContent('正在输入…');
  await act(async () => accept({ stream: [sse('assistant_completed', { message: message({ id: 'a1', role: 'assistant', content: '你好' }) })] }));
  await screen.findByText('你好', { selector: '.bubble p' });
  expect(screen.queryByRole('status', { name: '回复状态' })).toBeNull();
});

it('uses a quiet empty reply cursor through internal phases and preserves real reasoning and tool feedback', async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  const active = message({ id: 'pending', role: 'assistant', content: '', runId: 'run-waiting', status: 'streaming' });
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '爱丽丝' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { roleId: 'r1', sessionKey: 'role:r1' }, messages: [active],
      activeRun: { runId: 'run-waiting', roleId: 'r1', sessionKey: 'role:r1', sequence: 0, status: 'running', phase: 'preparing', stopping: false, tools: [] } } }),
    'GET /api/roles/r1/runs/run-waiting/events\\?after=0': () => ({ stream }),
  });
  render(<App />);
  await screen.findByRole('status', { name: '正在回复' });
  expect(screen.getByRole('status', { name: '回复状态' })).toHaveTextContent('正在输入…');
  expect(screen.getByRole('button', { name: '停止回复' })).toBeEnabled();
  let sequence = 0;
  const emit = (type: string, data: object) => act(() => controller.enqueue(new TextEncoder().encode(sse(type, { runId: 'run-waiting', sequence: ++sequence, ...data }))));
  for (const phase of ['queued', 'summarizing', 'model', 'tool', 'delivering', 'finishing']) {
    emit('assistant_phase', { phase });
    expect(screen.queryByText(/等待上一条回复收尾|准备上下文|整理历史|模型回复中|工具执行中|发送回复中|正在收尾|生成中/)).toBeNull();
    expect(screen.getByRole('status', { name: '正在回复' })).toBeVisible();
  }
  emit('assistant_thinking_delta', { messageId: 'pending', delta: '真实的推理内容' });
  expect(await screen.findByText('真实的推理内容')).toBeVisible();
  expect(screen.queryByRole('status', { name: '正在回复' })).toBeNull();
  emit('assistant_presentation', { process: [{ type: 'thinking', id: 'pending:thinking', text: '真实的推理内容' },
    { type: 'tool', id: 'lookup', name: 'web_search', status: 'running', result: '', argumentsPreview: '{"query":"fixture"}' }] });
  expect(await screen.findByRole('button', { name: /web_search/ })).toBeVisible();
  expect(screen.getByRole('button', { name: '停止回复' })).toBeEnabled();
});

it('shows only identity in the header, a recent message in the list, and inserts emoji at the caret before sending', async () => {
  const alice = role({ id: 'r1', name: '爱丽丝', description: '这是角色介绍' });
  const reply = message({ id: 'a1', role: 'assistant', content: '晚安，明天见' });
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [alice] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { roleId: 'r1', sessionKey: 'role:r1' }, messages: [reply] } }),
    'CHAT /api/roles/r1': () => ({ stream: [sse('user_message_accepted', { message: message({ id: 'u2', role: 'user', content: '你好😊呀' }) }), sse('assistant_completed', { message: message({ id: 'a2', role: 'assistant', content: '收到' }) })] }),
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
  expect(calls.find(call => call.path.endsWith('/runs'))?.body).toMatchObject({ content: '你好😊呀' });
});

it('renders a persisted process while the ring describes the current saved context', async () => {
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '爱丽丝' })] } }),
    'GET /api/roles/r1/context-status': () => ({ body: { roleId: 'r1', sessionKey: 'role:r1', status: 'ready', inputTokens: 2400, capacity: 16000, source: 'local', configurationId: null } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { roleId: 'r1', sessionKey: 'role:r1' }, messages: [message({ id: 'a1', role: 'assistant', content: '回复正文', metadata: {
      process: [{ type: 'thinking', id: 't1', text: '整理公开资料' }, { type: 'tool', id: 'c1', name: 'web_search', status: 'failed', result: '搜索服务不可用', argumentsPreview: '{"query":"公开资料"}' }],
      turnMetrics: { totalTokens: 1230, thinkingDurationMs: 11100 },
      contextUsage: { inputTokens: 1200, capacity: 8000, source: 'actual', configurationId: null },
    } })] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText('回复正文', { selector: '.bubble p' });
  expect(screen.getByRole('button', { name: '思考中' })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByText('整理公开资料')).toBeVisible();
  const failedGroup = screen.getByRole('button', { name: '1 次工具调用 1 个失败' });
  expect(failedGroup).toHaveAttribute('aria-expanded', 'false');
  expect(within(failedGroup).getByText('1 个失败')).toBeVisible();
  await user.click(screen.getByText(/1 次工具调用/));
  expect(screen.getByText('{"query":"公开资料"}')).toBeVisible();
  const tool = screen.getByRole('button', { name: /web_search/ });
  expect(tool).toHaveAttribute('aria-expanded', 'false');
  await user.click(tool);
  expect(screen.getByText('搜索服务不可用')).toBeVisible();
  expect(screen.getByText('思考 11.1 秒')).toBeVisible();
  expect(screen.getByText('1,230 tokens')).toBeVisible();
  await user.hover(screen.getByRole('button', { name: /压缩上下文/ }));
  expect(screen.getByRole('tooltip')).toHaveTextContent('点击压缩上下文');
  expect(await screen.findByText(/2,400 \/ 16,000/)).toBeVisible();
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
    'CHAT /api/roles/r1': () => {
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
  expect(screen.getByText('查看资料')).toBeVisible();
  const group = screen.getByRole('button', { name: '1 次工具调用' });
  if (group.getAttribute('aria-expanded') === 'false') await user.click(group);
  await user.click(screen.getByRole('button', { name: /lookup/ }));
  expect(screen.getByRole('button', { name: /lookup/ })).toHaveAttribute('aria-expanded', 'true');
  act(() => emit('assistant_completed', { message: message({ id: 'a2', role: 'assistant', content: '最终回复', metadata: { process } }) }));
  await screen.findByText('最终回复', { selector: '.bubble p' });
  expect(screen.getByRole('button', { name: '1 次工具调用' })).toHaveAttribute('aria-expanded', 'false');
  await user.click(screen.getByText('1 次工具调用'));
  expect(screen.getByRole('button', { name: /lookup/ })).toHaveAttribute('aria-expanded', 'false');
  await user.click(screen.getByRole('button', { name: /lookup/ }));
  expect(screen.getByText('已找到')).toBeVisible();
  expect(screen.queryByRole('button', { name: '停止回复' })).toBeNull();
});

it('restores one live thinking identity and appends deltas without deduplicating equal earlier thinking', async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  const text = '本地测试推理摘要：正在核对这次回复。';
  const process = [{ type: 'thinking', id: 'previous-tool-group:thinking', text },
    { type: 'tool', id: 'call', name: 'lookup', status: 'succeeded', result: '已找到' },
    { type: 'thinking', id: 'a2:thinking', text }];
  const active = message({ id: 'a2', role: 'assistant', content: '', runId: 'run-thinking', status: 'streaming',
    metadata: { process, streamThinking: text } });
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '爱丽丝' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { roleId: 'r1', sessionKey: 'role:r1' }, messages: [active],
      activeRun: { runId: 'run-thinking', roleId: 'r1', sessionKey: 'role:r1', sequence: 8, status: 'running', phase: 'model', stopping: false, tools: [] } } }),
    'GET /api/roles/r1/runs/run-thinking/events\\?after=8': () => ({ stream }),
  });
  render(<App />);
  await screen.findByRole('heading', { name: '爱丽丝' });
  expect(screen.getAllByText(text, { selector: '.chat-thinking-text' })).toHaveLength(2);
  expect(document.querySelectorAll('.chat-thinking-cursor')).toHaveLength(1);
  act(() => controller.enqueue(new TextEncoder().encode(sse('assistant_thinking_delta', {
    runId: 'run-thinking', sequence: 9, messageId: 'a2', delta: '继续整理。',
  }))));
  expect(await screen.findByText(text + '继续整理。', { selector: '.chat-thinking-text' })).toBeVisible();
  expect(screen.getAllByText(text, { selector: '.chat-thinking-text' })).toHaveLength(1);
  expect(document.querySelectorAll('.chat-thinking-text')).toHaveLength(2);
});

it.each(['failed', 'cancelled'])('keeps each same-name call intent in its live and reopened parameter chip after %s', async terminal => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  let sequence = 0;
  const emit = (type: string, data: object) => controller.enqueue(new TextEncoder().encode(sse(type, {
    roleId: 'r1', sessionKey: 'role:r1', runId: 'run-intent', sequence: ++sequence, ...data,
  })));
  const previews = ['{"description":"读取运行配置","path":"config.json"}', '{"description":"检查配置结果","path":"result.json"}'];
  const calls = previews.map((argumentsPreview, index) => ({ type: 'tool', id: `c${index}`, name: 'read_config',
    argumentsPreview, status: index === 0 ? 'succeeded' : terminal, result: index === 0 ? '配置已读取' : '配置不可用' }));
  const saved = message({ id: 'a1', role: 'assistant', content: '处理结束', metadata: { process: calls } });
  let history: ReturnType<typeof message>[] = [];
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '爱丽丝' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { roleId: 'r1', sessionKey: 'role:r1' }, messages: history } }),
    'CHAT /api/roles/r1': () => {
      emit('user_message_accepted', { message: message({ id: 'u1', role: 'user', content: '检查配置' }) });
      emit('assistant_generation_started', { messageId: 'a1' });
      emit('tool_execution_start', { toolCallId: 'c0', toolName: 'read_config', argumentsPreview: previews[0] });
      return { stream, headers: { 'X-Meido-Run-Id': 'run-intent' } };
    },
  });
  const user = userEvent.setup();
  const view = render(<App />);
  await screen.findByRole('heading', { name: '爱丽丝' });
  await user.type(screen.getByRole('textbox', { name: '消息内容' }), '检查配置{Enter}');
  const firstRunning = await screen.findByRole('button', { name: 'read_config · 运行中' });
  expect(within(firstRunning).getByText(previews[0])).toBeVisible();
  expect(firstRunning).toHaveAccessibleDescription(previews[0]);
  expect(within(firstRunning).getByText(previews[0])).toHaveAttribute('title', previews[0]);
  expect(screen.queryByText('处理结束')).toBeNull();
  act(() => {
    emit('tool_execution_end', { toolCallId: 'c0', toolName: 'read_config', status: 'succeeded', result: '配置已读取', isError: false });
    emit('tool_execution_start', { toolCallId: 'c1', toolName: 'read_config', argumentsPreview: previews[1] });
  });
  await screen.findByText(previews[1]);
  const secondRunning = screen.getByRole('button', { name: 'read_config · 运行中' });
  expect(within(secondRunning).getByText(previews[1])).toBeVisible();
  expect(within(screen.getByRole('button', { name: 'read_config · 已完成' })).getByText(previews[0])).toBeVisible();
  act(() => {
    emit('tool_execution_end', { toolCallId: 'c1', toolName: 'read_config', status: terminal, result: '配置不可用', isError: true });
    emit('tool_execution_update', { toolCallId: 'c0', toolName: 'read_config', status: 'running', result: '迟到进度' });
    emit('tool_execution_start', { toolCallId: 'c1', toolName: 'read_config', argumentsPreview: '{"description":"迟到意图"}' });
  });
  const terminalLabel = terminal === 'failed' ? '失败' : '已取消';
  const secondEnded = await screen.findByRole('button', { name: `read_config · ${terminalLabel}` });
  expect(within(secondEnded).getByText(previews[1])).toBeVisible();
  const groupName = terminal === 'failed' ? '2 次工具调用 1 个失败' : '2 次工具调用';
  const liveGroup = screen.getByRole('button', { name: groupName });
  if (terminal === 'failed') expect(within(liveGroup).getByText('1 个失败')).toBeVisible();
  else expect(within(liveGroup).queryByText(/失败/)).toBeNull();
  expect(screen.queryByText(/迟到/)).toBeNull();
  act(() => {
    history = [saved];
    emit('assistant_completed', { message: saved });
    controller.close();
  });
  await screen.findByText('处理结束', { selector: '.bubble p' });
  const completedGroup = screen.getByRole('button', { name: groupName });
  expect(completedGroup).toHaveAttribute('aria-expanded', 'false');
  if (terminal === 'failed') expect(within(completedGroup).getByText('1 个失败')).toBeVisible();
  else expect(within(completedGroup).queryByText(/失败/)).toBeNull();
  view.unmount();
  render(<App />);
  await screen.findByText('处理结束', { selector: '.bubble p' });
  const restoredGroup = screen.getByRole('button', { name: groupName });
  expect(restoredGroup).toHaveAttribute('aria-expanded', 'false');
  if (terminal === 'failed') expect(within(restoredGroup).getByText('1 个失败')).toBeVisible();
  else expect(within(restoredGroup).queryByText(/失败/)).toBeNull();
  await user.click(restoredGroup);
  const first = screen.getByRole('button', { name: 'read_config · 已完成' });
  const second = screen.getByRole('button', { name: `read_config · ${terminalLabel}` });
  expect(within(first).getByText(previews[0])).toBeVisible();
  expect(within(second).getByText(previews[1])).toBeVisible();
  await user.click(first);
  expect(screen.getByText('配置已读取')).toBeVisible();
  await user.click(second);
  expect(screen.getByText('配置不可用')).toBeVisible();
});
