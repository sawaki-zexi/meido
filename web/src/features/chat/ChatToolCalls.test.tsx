import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it } from 'vitest';
import { ChatToolCalls, type ToolEntry } from './ChatToolCalls';

afterEach(cleanup);

it('keeps the explicit failed count visible through running, collapse, and completion', async () => {
  const calls: ToolEntry[] = [
    { type: 'tool', id: 'failed-1', name: 'web_search', status: 'failed', result: '搜索服务不可用' },
    { type: 'tool', id: 'failed-2', name: 'web_fetch', status: 'failed', result: '网页暂时不可用' },
    { type: 'tool', id: 'cancelled', name: 'read_file', status: 'cancelled', result: '已取消读取' },
    { type: 'tool', id: 'unknown', name: 'shell', status: 'unknown', result: '结果不确定' },
    { type: 'tool', id: 'running', name: 'lookup', status: 'running', result: '' },
  ];
  const user = userEvent.setup();
  const view = render(<ChatToolCalls calls={calls} streaming />);
  const runningGroup = screen.getByRole('button', { name: '5 次工具调用 2 个失败 1 个执行中' });
  expect(runningGroup).toHaveAttribute('aria-expanded', 'true');
  expect(within(runningGroup).getByText('2 个失败')).toBeVisible();
  await user.click(runningGroup);
  expect(runningGroup).toHaveAttribute('aria-expanded', 'false');
  expect(within(runningGroup).getByText('2 个失败')).toBeVisible();
  expect(screen.queryByRole('button', { name: 'web_search · 失败' })).toBeNull();

  view.rerender(<ChatToolCalls calls={calls.map(call => call.status === 'running'
    ? { ...call, status: 'succeeded', result: '已找到' } : call)} streaming={false} />);
  const completedGroup = screen.getByRole('button', { name: '5 次工具调用 2 个失败' });
  expect(completedGroup).toHaveAttribute('aria-expanded', 'false');
  expect(within(completedGroup).getByText('2 个失败')).toBeVisible();
  expect(screen.queryByText('1 个执行中')).toBeNull();
  completedGroup.focus();
  await user.keyboard('{Enter}');
  expect(completedGroup).toHaveAttribute('aria-expanded', 'true');
  await user.click(screen.getByRole('button', { name: 'web_search · 失败' }));
  expect(screen.getByText('搜索服务不可用')).toBeVisible();
  expect(screen.getByRole('button', { name: 'read_file · 已取消' })).toBeVisible();
  expect(screen.getByRole('button', { name: 'shell · 结果不确定' })).toBeVisible();
});

it.each(['succeeded', 'cancelled', 'unknown', 'denied', 'invalid_arguments', 'timed_out'])(
  'does not label a %s tool group as failed', status => {
    render(<ChatToolCalls calls={[{ type: 'tool', id: 'call', name: 'lookup', status, result: '已记录结果' }]} streaming={false} />);
    const group = screen.getByRole('button', { name: '1 次工具调用' });
    expect(group).toHaveAttribute('aria-expanded', 'false');
    expect(within(group).queryByText(/失败/)).toBeNull();
  },
);
