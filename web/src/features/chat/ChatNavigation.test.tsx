import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { fakeBackend, message, role } from '../../test/fakeBackend';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

it('从聊天头像和名字打开资料，待办移到全局导航且保留提醒数', async () => {
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '诗织' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/todos/settings': () => ({ body: { enabled: true, remindersEnabled: true, timezone: 'Asia/Shanghai' } }),
    'GET /api/todos/reminders': () => ({ body: { reminders: [], unreadCount: 3 } }),
  });
  const user = userEvent.setup();
  render(<App />);
  const heading = await screen.findByRole('heading', { name: '诗织', level: 1 });
  const header = within(heading.closest('header')!);
  const profile = header.getByRole('button', { name: '角色资料' });
  expect(profile).toContainElement(heading);
  expect(within(profile).getByText('诗织')).toBeInTheDocument();
  const navigation = within(screen.getByRole('navigation', { name: '页面' }));
  expect(await navigation.findByRole('button', { name: '待办提醒 3' })).toBeInTheDocument();
  expect(header.queryByRole('button', { name: /待办/ })).not.toBeInTheDocument();
  expect(header.getByRole('button', { name: '角色记忆' })).toBeInTheDocument();
  expect(header.getByRole('button', { name: '更多' })).toBeInTheDocument();
  expect(header.queryByRole('button', { name: '生图与历史' })).not.toBeInTheDocument();
  await user.click(profile);
  expect(await screen.findByRole('dialog', { name: '角色资料' })).toBeVisible();
});

it('更多菜单按当前插件状态显示生图，支持键盘返回且不会提前读取历史', async () => {
  let enabled = true;
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '诗织' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/services/novelai': () => ({ body: { enabled } }),
  });
  const user = userEvent.setup();
  render(<App />);
  const trigger = await screen.findByRole('button', { name: '更多' });
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  trigger.focus();
  await user.keyboard('{ArrowDown}');
  const generate = await screen.findByRole('menuitem', { name: '生图与历史' });
  await waitFor(() => expect(generate).toHaveFocus());
  await user.keyboard('{Escape}');
  expect(trigger).toHaveFocus();
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  enabled = false;
  await user.click(trigger);
  expect(await screen.findByText('暂无可用操作')).toBeVisible();
  expect(screen.queryByRole('menuitem', { name: '生图与历史' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '角色' }));
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  expect(calls.some(call => call.path.endsWith('/generations') || call.method !== 'GET')).toBe(false);
});

it('没有角色时仍能从全局入口查看待办', async () => {
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [] } }),
    'GET /api/todos/settings': () => ({ body: { enabled: true, remindersEnabled: false, timezone: 'Asia/Shanghai' } }),
    'GET /api/todos': () => ({ body: { todos: [] } }),
    'GET /api/todos/diagnostics': () => ({ body: { items: [] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole('button', { name: '待办' }));
  expect(await screen.findByRole('dialog', { name: '待办' })).toBeVisible();
  expect(await screen.findByText('提醒已暂停，待办清单仍可继续使用。')).toBeVisible();
  expect(calls.some(call => call.path.endsWith('/session') || call.method !== 'GET')).toBe(false);
});

it('全局待办的来源仍跳到原角色消息，不受当前聊天角色影响', async () => {
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '诗织' }), role({ id: 'r2', name: '爱丽丝' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/roles/r2/session': () => ({ body: { session: { sessionKey: 'role:r2', roleId: 'r2' },
      messages: [message({ id: 'source', sessionKey: 'role:r2', content: '明天交报告', role: 'user' })] } }),
    'GET /api/todos/settings': () => ({ body: { enabled: true, remindersEnabled: false, timezone: 'Asia/Shanghai' } }),
    'GET /api/todos/diagnostics': () => ({ body: { items: [] } }),
    'GET /api/todos': () => ({ body: { todos: [{ id: 'one', title: '交报告', description: '', status: 'inbox',
      sourceRoleId: 'r2', sourceSessionKey: 'role:r2', sourceMessageId: 'source', sourceAvailable: true }] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('heading', { name: '诗织', level: 1 });
  await user.click(await screen.findByRole('button', { name: '待办' }));
  await user.click(await screen.findByRole('button', { name: '查看来源' }));
  expect(await screen.findByRole('heading', { name: '爱丽丝', level: 1 })).toBeVisible();
  expect(screen.queryByRole('dialog', { name: '待办' })).not.toBeInTheDocument();
  expect(await screen.findByText('明天交报告')).toBeVisible();
  expect(screen.getByText('记忆来源')).toBeInTheDocument();
});

it('从角色聊天打开按角色隔离的多端会话视图', async () => {
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: '诗织' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/roles/r1/channels/accounts': () => ({ body: { accounts: [] } }),
    'GET /api/roles/r1/channels/threads\\?networkOnly=true': () => ({ body: { threads: [{
      threadId: 'telegram-private', sessionKey: 'thread:telegram-private', accountId: 'account-1',
      channel: 'telegram', chatId: '42', chatType: 'private', title: 'Alice',
      lastActivity: '2026-10-23T00:00:00Z', viaAccount: { platform: 'telegram', displayName: 'Shiori Bot' },
      latestMessage: { content: '外部消息', role: 'user', status: 'completed', createdAt: '2026-10-23T00:00:00Z' },
    }] } }),
    'GET /api/roles/r1/channels/threads/telegram-private/messages\\?limit=50&networkOnly=true': () => ({ body: {
      thread: { threadId: 'telegram-private', sessionKey: 'thread:telegram-private', channel: 'telegram', chatType: 'private', title: 'Alice' },
      messages: [{ id: 'm1', sequence: 1, role: 'user', content: '外部消息', createdAt: '2026-10-23T00:00:00Z', metadata: { threadId: 'telegram-private', senderName: 'Alice' } }],
      hasMore: false, nextBefore: null,
    } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole('heading', { name: '诗织', level: 1 });

  await user.click(screen.getByRole('button', { name: '多端会话' }));

  const phone = await screen.findByRole('dialog', { name: '诗织的多端会话' });
  expect(phone).toBeVisible();
  expect(phone).toHaveAttribute('aria-modal', 'true');
  expect(await screen.findByRole('heading', { name: '诗织 · 手机会话' })).toBeVisible();
  expect(screen.getByRole('complementary', { name: '渠道会话列表' })).toBeVisible();
  const thread = screen.getByRole('button', { name: /Alice/ });
  expect(thread).toBeVisible();
  await user.click(thread);
  expect(await within(screen.getByRole('region', { name: '会话消息' })).findByText('外部消息')).toBeVisible();
  expect(screen.queryByText('会话设置')).not.toBeInTheDocument();

  await user.click(screen.getByRole('button', { name: '返回聊天' }));
  expect(screen.queryByRole('dialog', { name: '诗织的多端会话' })).not.toBeInTheDocument();
  expect(await screen.findByRole('heading', { name: '诗织', level: 1 })).toBeVisible();
});
