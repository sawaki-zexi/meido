import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { MemoryPanel } from './MemoryPanel';

const owner = role({ id: 'r1', name: '诗织' });
const origin = { kind: 'message', sessionKey: 'role:r1', messageIds: ['m1'], messageRange: null, stableSourceKey: 'message:m1' };
const item = { id: 'one', roleId: 'r1', memoryType: 'preference', summary: '主人喜欢在下雨天喝红茶', status: 'active',
  createdAt: '2026-10-19T00:00:00Z', updatedAt: '2026-10-19T00:00:00Z', happenedAt: null, extra: {},
  sourceRef: { ...origin, sourceKeys: [], sources: [origin] } };

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('角色工作区按六个分类查看记忆，领域和时间排序交给服务并可刷新文档', async () => {
  let self = '她喜欢安静的阅读时光';
  const { calls } = fakeBackend({
    'GET /api/roles/r1/memories.*': () => ({ body: { memories: [item], total: 41 } }),
    'GET /api/roles/r1/memory-admin/filters': () => ({ body: { domains: ['daily', 'work'] } }),
    'GET /api/roles/r1/memory-documents': () => ({ body: { documents: [
      { name: 'SELF.md', content: self }, { name: 'MEMORY.md', content: '主人喜欢红茶' },
    ], journals: [] } }),
  });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} embedded onBack={() => {}} onOpenSource={() => {}} />);
  expect(within(screen.getByRole('tablist', { name: '记忆视图' })).getAllByRole('tab').map(tab => tab.textContent))
    .toEqual(['时间线', '自我', '长期', '经历', '近期', '待整理']);
  await user.click(screen.getByRole('tab', { name: '自我' }));
  expect(await screen.findByText(self)).toBeVisible();
  self = '她也开始喜欢与主人分享';
  await user.click(screen.getByRole('button', { name: '刷新记忆' }));
  expect(await screen.findByText(self)).toBeVisible();
  await user.click(screen.getByRole('tab', { name: '长期' }));
  expect(await screen.findByText('主人喜欢红茶')).toBeVisible();
  await user.click(screen.getByRole('tab', { name: '时间线' }));
  await user.click(await screen.findByRole('button', { name: '下一页' }));
  await user.selectOptions(screen.getByRole('combobox', { name: '领域筛选' }), 'work');
  await user.selectOptions(screen.getByRole('combobox', { name: '记忆排序' }), 'asc');
  await waitFor(() => {
    const latest = calls.filter(call => call.path.includes('/memories?')).at(-1)!;
    const params = new URLSearchParams(latest.path.split('?')[1]);
    expect(params.get('memoryDomain')).toBe('work');
    expect(params.get('sortBy')).toBe('created_at');
    expect(params.get('sortOrder')).toBe('asc');
    expect(params.get('page')).toBeNull();
  });
  expect(screen.getByRole('button', { name: '记住一件事' })).toBeVisible();
});

it('工作区中已有记忆时仍可继续新增，保存后回到列表', async () => {
  let rows = [item];
  fakeBackend({
    'GET /api/roles/r1/memories.*': () => ({ body: { memories: rows, total: rows.length } }),
    'POST /api/roles/r1/memories': () => {
      const added = { ...item, id: 'two', summary: '主人喜欢在周末散步' };
      rows = [...rows, added];
      return { body: added };
    },
  });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} embedded onBack={() => {}} onOpenSource={() => {}} />);
  await screen.findByText(item.summary);
  expect(screen.queryByRole('button', { name: '返回对话' })).not.toBeInTheDocument();
  const add = screen.queryByRole('button', { name: '记住一件事' });
  expect(add).not.toBeNull();
  await user.click(add!);
  const editor = within(screen.getByRole('dialog', { name: '记住一件事' }));
  await user.type(editor.getByLabelText('内容'), '主人喜欢在周末散步');
  await user.click(editor.getByRole('button', { name: '保存' }));
  expect(await screen.findByRole('button', { name: '查看记忆：主人喜欢在周末散步' })).toBeVisible();
  expect(screen.getByRole('button', { name: '记住一件事' })).toBeEnabled();
  expect(screen.getByRole('heading', { name: '记忆' })).toHaveFocus();
});

it('按本地记录日期分组时间线，保留服务返回顺序，详细时间按需查看', async () => {
  const first = { ...item, createdAt: new Date(2026, 9, 19, 8).toISOString() };
  const sameDay = { ...item, id: 'two', summary: '主人也喜欢听钢琴曲', createdAt: new Date(2026, 9, 19, 9).toISOString() };
  const earlier = { ...item, id: 'three', summary: '一起度过的周末', createdAt: new Date(2026, 9, 18, 8).toISOString() };
  fakeBackend({ 'GET /api/roles/r1/memories.*': () => ({ body: { memories: [first, sameDay, earlier], total: 3 } }) });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={() => {}} />);
  const timeline = await screen.findByRole('list', { name: '记忆列表' });
  expect(within(timeline).getAllByRole('heading').map(heading => heading.textContent)).toEqual(['2026年10月19日', '2026年10月18日']);
  expect(within(timeline).getAllByRole('button').map(button => button.getAttribute('aria-label'))).toEqual([
    `查看记忆：${item.summary}`, `查看记忆：${sameDay.summary}`, `查看记忆：${earlier.summary}`,
  ]);
  await user.click(screen.getByRole('button', { name: `查看记忆：${item.summary}` }));
  const detail = within(screen.getByRole('region', { name: '记忆详情' }));
  expect(detail.getByText('记录时间')).not.toBeVisible();
  await user.click(detail.getByText('更多操作与信息'));
  expect(detail.getByText('记录时间')).toBeVisible();
});

it('在列表原位置展开来源和操作，编辑时才打开表单', async () => {
  const onSource = vi.fn();
  const { calls } = fakeBackend({ 'GET /api/roles/r1/memories.*': () => ({ body: { memories: [item], total: 1 } }) });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={onSource} />);
  expect(await screen.findByText(item.summary)).toBeVisible();
  expect(screen.queryByRole('button', { name: '删除' })).not.toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: '向量筛选' })).not.toBeInTheDocument();
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: `查看记忆：${item.summary}` }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  const detail = within(await screen.findByRole('region', { name: '记忆详情' }));
  expect(screen.getByRole('button', { name: `查看记忆：${item.summary}` })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('button', { name: '记住一件事' })).toBeVisible();
  await user.click(detail.getByRole('button', { name: '定位到消息' }));
  expect(onSource).toHaveBeenCalledWith(origin, 'm1');
  await user.click(detail.getByRole('button', { name: '编辑记录' }));
  const editor = within(await screen.findByRole('dialog', { name: '编辑记忆' }));
  expect(editor.getByLabelText('内容')).toBeDisabled();
  expect(editor.getByLabelText('扩展字段（JSON）')).not.toBeVisible();
  await user.click(editor.getByText('高级字段'));
  expect(editor.getByLabelText('扩展字段（JSON）')).toBeVisible();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('记录失败保留输入，重试成功后回到列表，保存只提交主人填写的记录', async () => {
  let attempts = 0;
  let rows: typeof item[] = [];
  const { calls } = fakeBackend({
    'GET /api/roles/r1/memories.*': () => ({ body: { memories: rows, total: rows.length } }),
    'POST /api/roles/r1/memories': () => {
      if (++attempts === 1) return { status: 500, body: { detail: '暂时无法保存' } };
      rows = [{ ...item, summary: '每天晚上提醒我喝水' }]; return { body: rows[0] };
    },
  });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={() => {}} />);
  await user.click(screen.getByRole('button', { name: '记住一件事' }));
  const editor = within(screen.getByRole('dialog', { name: '记住一件事' }));
  await user.type(editor.getByLabelText('内容'), '每天晚上提醒我喝水');
  await user.selectOptions(editor.getByLabelText('类型'), 'procedure');
  await user.click(editor.getByRole('button', { name: '保存' }));
  expect(await editor.findByRole('alert')).toHaveTextContent('暂时无法保存');
  expect(editor.getByLabelText('内容')).toHaveValue('每天晚上提醒我喝水');
  await user.click(editor.getByRole('button', { name: '保存' }));
  expect(await screen.findByRole('button', { name: '查看记忆：每天晚上提醒我喝水' })).toBeVisible();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: '诗织的记忆' })).toHaveFocus();
  expect(calls.filter(call => call.method === 'POST').map(call => call.body)).toEqual([
    { summary: '每天晚上提醒我喝水', memoryType: 'procedure', happenedAt: null },
    { summary: '每天晚上提醒我喝水', memoryType: 'procedure', happenedAt: null },
  ]);
});

it('筛选和搜索交给原接口，分页使用一致条数，领域变化返回第一页', async () => {
  const { calls } = fakeBackend({ 'GET /api/roles/r1/memories.*': () => ({ body: { memories: [item], total: 41 } }) });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={() => {}} />);
  await screen.findByText(item.summary);
  expect(new URL(calls[0].path, 'http://fixture').searchParams.get('pageSize')).toBe('20');
  await user.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(calls.at(-1)?.path).toContain('page=2'));
  await user.click(screen.getByRole('button', { name: '筛选' }));
  await user.type(screen.getByLabelText('领域筛选'), 'study');
  await waitFor(() => expect(calls.at(-1)?.path).toContain('memoryDomain=study'));
  expect(new URL(calls.at(-1)!.path, 'http://fixture').searchParams.has('page')).toBe(false);
  await user.selectOptions(screen.getByRole('combobox', { name: '类型筛选' }), 'preference');
  await user.type(screen.getByRole('textbox', { name: '搜索记忆' }), '红茶');
  await user.click(screen.getByRole('button', { name: '搜索' }));
  await waitFor(() => expect(new URL(calls.at(-1)!.path, 'http://fixture').searchParams.get('q')).toBe('红茶'));
  expect(new URL(calls.at(-1)!.path, 'http://fixture').searchParams.get('memoryType')).toBe('preference');
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('批量删除需明确确认，取消不写入，确认只删除选中项', async () => {
  let rows = [item, { ...item, id: 'two', summary: '主人不喜欢太甜的点心' }];
  const { calls } = fakeBackend({
    'GET /api/roles/r1/memories.*': () => ({ body: { memories: rows, total: rows.length } }),
    'POST /api/roles/r1/memory-admin/items/batch-delete': () => { rows = rows.filter(row => row.id !== 'one'); return { body: { deleted: 1 } }; },
  });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={() => {}} />);
  await screen.findByText(item.summary);
  await user.click(screen.getByRole('button', { name: '批量管理' }));
  await user.click(screen.getByRole('checkbox', { name: `选择记忆 ${item.summary}` }));
  await user.click(screen.getByRole('button', { name: '删除选中' }));
  const first = within(screen.getByRole('dialog', { name: '删除选中的记忆' }));
  expect(first.getByText(/选中的 1 条/)).toBeVisible();
  await user.click(first.getByRole('button', { name: '取消' }));
  expect(calls.every(call => call.method === 'GET')).toBe(true);
  await user.click(screen.getByRole('button', { name: '删除选中' }));
  await user.click(within(screen.getByRole('dialog', { name: '删除选中的记忆' })).getByRole('button', { name: '确认' }));
  await waitFor(() => expect(screen.queryByText(item.summary)).not.toBeInTheDocument());
  expect(screen.getByText('主人不喜欢太甜的点心')).toBeVisible();
  expect(calls.find(call => call.method === 'POST')?.body).toEqual({ ids: ['one'] });
});

it('记忆文档单独读取，标题可读，切回条目后保留搜索草稿', async () => {
  const { calls } = fakeBackend({
    'GET /api/roles/r1/memories.*': () => ({ body: { memories: [item], total: 1 } }),
    'GET /api/roles/r1/memory-documents': () => ({ body: { documents: [{ name: 'MEMORY.md', content: '喜欢红茶，周末一起看电影。' }], journals: [{ date: '2026-10-19', content: '今天整理了偏好。' }] } }),
  });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={() => {}} />);
  await screen.findByText(item.summary);
  await user.type(screen.getByRole('textbox', { name: '搜索记忆' }), '未提交的草稿');
  expect(calls.some(call => call.path.endsWith('/memory-documents'))).toBe(false);
  await user.click(screen.getByRole('tab', { name: '记忆文档' }));
  expect(await screen.findByText('喜欢红茶，周末一起看电影。')).toBeVisible();
  expect(screen.getByRole('heading', { name: '长期记忆' })).toBeVisible();
  await user.selectOptions(screen.getByRole('combobox', { name: '选择记忆文档' }), 'journal:2026-10-19');
  expect(screen.getByText('今天整理了偏好。')).toBeVisible();
  await user.click(screen.getByRole('tab', { name: '记忆条目' }));
  expect(screen.getByRole('textbox', { name: '搜索记忆' })).toHaveValue('未提交的草稿');
});

it('编辑状态保留原时间精度和来源，忘记操作取消不提交', async () => {
  const original = { ...item, happenedAt: '2026-10-19T00:15:39.123Z', extra: { domain: 'life', retained: 7 } };
  const { calls } = fakeBackend({
    'GET /api/roles/r1/memories.*': () => ({ body: { memories: [original], total: 1 } }),
    'PATCH /api/roles/r1/memory-admin/items/one': () => ({ body: original }),
  });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={() => {}} />);
  await user.click(await screen.findByRole('button', { name: `查看记忆：${item.summary}` }));
  await user.click(screen.getByText('更多操作与信息'));
  await user.click(screen.getByRole('button', { name: '忘记' }));
  await user.click(within(screen.getByRole('dialog', { name: '忘记这条记忆' })).getByRole('button', { name: '取消' }));
  expect(calls.every(call => call.method === 'GET')).toBe(true);
  await user.click(screen.getByRole('button', { name: '编辑记录' }));
  await user.selectOptions(screen.getByLabelText('状态'), 'forgotten');
  await user.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(calls.some(call => call.method === 'PATCH')).toBe(true));
  expect(calls.find(call => call.method === 'PATCH')?.body).toMatchObject({
    status: 'forgotten', happenedAt: original.happenedAt, extraJson: original.extra, sourceRef: original.sourceRef,
  });
});

it('加载失败不伪装成空库，可以重新读取', async () => {
  let reads = 0;
  fakeBackend({ 'GET /api/roles/r1/memories.*': () => ++reads === 1 ? { status: 500, body: { detail: '暂时离线' } } : { body: { memories: [item], total: 1 } } });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={() => {}} />);
  expect(await screen.findByText('暂时无法读取记忆')).toBeVisible();
  expect(screen.queryByText('暂无记忆')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '重新加载' }));
  expect(await screen.findByText(item.summary)).toBeVisible();
});

it('选择后切换筛选失败，会清掉不可见的旧选择并禁用批量删除', async () => {
  const { calls } = fakeBackend({ 'GET /api/roles/r1/memories.*': () => calls.length > 1
    ? { status: 500, body: { detail: '筛选读取失败' } } : { body: { memories: [item], total: 1 } } });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={() => {}} />);
  await screen.findByText(item.summary);
  await user.click(screen.getByRole('button', { name: '批量管理' }));
  await user.click(screen.getByRole('checkbox', { name: `选择记忆 ${item.summary}` }));
  await user.selectOptions(screen.getByRole('combobox', { name: '类型筛选' }), 'fact');
  expect(await screen.findByText('暂时无法读取记忆')).toBeVisible();
  expect(screen.getByText('已选 0 条')).toBeVisible();
  expect(screen.getByRole('button', { name: '删除选中' })).toBeDisabled();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('展开内容可以直接收起，编辑表单独占焦点并回到原行', async () => {
  fakeBackend({ 'GET /api/roles/r1/memories.*': () => ({ body: { memories: [item], total: 1 } }) });
  const user = userEvent.setup();
  render(<MemoryPanel role={owner} onBack={() => {}} onOpenSource={() => {}} />);
  const opener = await screen.findByRole('button', { name: `查看记忆：${item.summary}` });
  await user.click(opener);
  expect(screen.getByRole('region', { name: '记忆详情' })).toBeVisible();
  await user.click(opener);
  expect(screen.queryByRole('region', { name: '记忆详情' })).not.toBeInTheDocument();
  await user.click(opener);
  await user.click(screen.getByRole('button', { name: '编辑记录' }));
  const panel = screen.getByRole('dialog', { name: '编辑记忆' });
  for (let index = 0; index < 8; index++) { await user.tab({ shift: true }); expect(panel).toContainElement(document.activeElement as HTMLElement); }
  await user.keyboard('{Escape}');
  expect(opener).toHaveFocus();
  expect(screen.getByRole('region', { name: '记忆详情' })).toBeVisible();
  const add = screen.getByRole('button', { name: '记住一件事' });
  await user.click(add);
  await user.keyboard('{Escape}');
  expect(add).toHaveFocus();
  await user.click(opener);
  expect(screen.queryByRole('region', { name: '记忆详情' })).not.toBeInTheDocument();
  await user.click(opener);
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('region', { name: '记忆详情' })).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
});
