import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { GenerationDrawer } from './GenerationDrawer';
import { PromptTagManager } from './PromptTagManager';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const first = { id: 'a'.repeat(32), name: '雨景', category: 'atmosphere', keywords: ['下雨'], prompt: 'rain', negative_prompt: 'sunny', rating: 'general', enabled: true, reference_attachment_id: null, referenceAttachment: null };
const second = { ...first, id: 'b'.repeat(32), name: '夜景', keywords: ['晚上'], prompt: 'night' };
const routes = {
  'GET /api/services/novelai': () => ({ body: { enabled: false, revision: 1, allowedModels: [], allowedSamplers: [], dailyAttemptLimit: 20 } }),
  'GET /api/roles/r[12]/generations': () => ({ body: { generations: [], nextCursor: null, usage: { accountAttempts: 0, manualAttempts: 0 } } }),
};
const drawer = (id: string) => <GenerationDrawer role={role({ id, name: id })} onClose={() => {}} onGenerated={() => {}} />;

it('opens a dedicated editor, restores the saved value and confirms deletion before sending it', async () => {
  let entries = [first];
  const { calls } = fakeBackend({
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: entries, revision: 1, nextCursor: null } }),
    ['DELETE /api/roles/r1/novelai/prompt-tags/' + first.id]: () => { entries = []; return { body: { revision: 2 } }; },
  });
  const user = userEvent.setup(); render(<PromptTagManager roleId="r1" expanded />);
  await user.click(await screen.findByRole('button', { name: '编辑标签 雨景' }));
  expect(screen.queryByRole('searchbox')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存标签' })).toBeDisabled();
  await user.type(screen.getByLabelText('标签名称'), '草稿');
  await user.click(screen.getByRole('button', { name: '恢复已保存内容' }));
  expect(screen.getByLabelText('标签名称')).toHaveValue('雨景');
  expect(screen.getByRole('button', { name: '保存标签' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: '返回标签库' }));
  await user.click(screen.getByRole('button', { name: '删除标签 雨景' }));
  let confirm = screen.getByRole('dialog', { name: '删除提示标签' });
  expect(calls.some((call) => call.method === 'DELETE')).toBe(false);
  await user.click(within(confirm).getByRole('button', { name: '取消' }));
  expect(screen.getByRole('article', { name: '提示标签 雨景' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '删除标签 雨景' }));
  confirm = screen.getByRole('dialog', { name: '删除提示标签' });
  await user.click(within(confirm).getByRole('button', { name: '确认删除' }));
  await screen.findByText('暂无提示标签。');
  expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(1);
});

it('keeps a failed deletion reviewable and ignores a late deletion after role A-B-A', async () => {
  let failed = true;
  let finish: ((value: { body: unknown }) => void) | undefined;
  const { calls } = fakeBackend({
    'GET /api/roles/r[12]/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [first], revision: 1, nextCursor: null } }),
    ['DELETE /api/roles/r1/novelai/prompt-tags/' + first.id]: () => failed
      ? { status: 503, body: { detail: '暂时无法删除，请重试' } } : new Promise((resolve) => { finish = resolve; }),
  });
  const user = userEvent.setup(); const view = render(<PromptTagManager roleId="r1" expanded />);
  await user.click(await screen.findByRole('button', { name: '删除标签 雨景' }));
  await user.click(screen.getByRole('button', { name: '确认删除' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法删除');
  expect(screen.getByRole('dialog', { name: '删除提示标签' })).toBeInTheDocument();
  failed = false;
  await user.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(finish).toBeDefined());
  view.rerender(<PromptTagManager roleId="r2" expanded />);
  await screen.findByRole('article', { name: '提示标签 雨景' });
  view.rerender(<PromptTagManager roleId="r1" expanded />);
  await screen.findByRole('article', { name: '提示标签 雨景' });
  await act(async () => finish?.({ body: { revision: 2 } }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('article', { name: '提示标签 雨景' })).toBeInTheDocument();
  expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(2);
  expect(calls.some((call) => call.path.includes('generations'))).toBe(false);
});

it('finds a tag beyond the first page by normalized name, keyword and either prompt without generating', async () => {
  const later = { ...second, name: 'Night Garden', keywords: ['夜晚'], prompt: 'moonlight', negative_prompt: 'daytime' };
  const firstPage = Array.from({ length: 50 }, (_, index) => ({ ...first, id: index.toString(16).padStart(32, '0'), name: `Rain ${index}` }));
  const { calls } = fakeBackend({
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: firstPage, revision: 1, nextCursor: firstPage[49].id } }),
    ['GET /api/roles/r1/novelai/prompt-tags\\?limit=50&cursor=' + firstPage[49].id]: () => ({ body: { tags: [later], revision: 1, nextCursor: null } }),
  });
  const user = userEvent.setup(); render(<PromptTagManager roleId="r1" expanded />);
  const search = await screen.findByRole('searchbox', { name: '搜索提示标签' });
  await screen.findByRole('article', { name: '提示标签 Night Garden' });
  for (const query of ['ＮＩＧＨＴ', '夜晚', 'MOONLIGHT', 'daytime']) {
    await user.clear(search); await user.type(search, query);
    expect(screen.getAllByRole('article')).toHaveLength(1);
    expect(screen.getByRole('article', { name: '提示标签 Night Garden' })).toBeInTheDocument();
    expect(screen.getByText('显示 1 / 51 个标签')).toBeInTheDocument();
  }
  expect(calls).toHaveLength(2);
  expect(calls.every((call) => call.method === 'GET')).toBe(true);
});

it('combines filters, explains no matches and keeps the editing draft when clearing them', async () => {
  const target = { ...second, name: 'Night target', category: 'composition', rating: 'mature', enabled: false };
  const entries = [first, target,
    { ...target, id: 'c'.repeat(32), name: 'Night enabled', enabled: true },
    { ...target, id: 'd'.repeat(32), name: 'Night outfit', category: 'outfit' },
    { ...target, id: 'e'.repeat(32), name: 'Night general', rating: 'general' },
  ];
  const { calls } = fakeBackend({
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: entries, revision: 1, nextCursor: null } }),
  });
  const user = userEvent.setup(); render(<PromptTagManager roleId="r1" expanded />);
  await user.click(await screen.findByRole('button', { name: '编辑标签 雨景' }));
  await user.clear(screen.getByLabelText('标签名称')); await user.type(screen.getByLabelText('标签名称'), '未保存修改');
  await user.click(screen.getByRole('button', { name: '返回标签库' }));
  await user.click(screen.getByRole('button', { name: '编辑标签 雨景' }));
  expect(screen.getByLabelText('标签名称')).toHaveValue('未保存修改');
  await user.click(screen.getByRole('button', { name: '返回标签库' }));
  const search = screen.getByRole('searchbox', { name: '搜索提示标签' });
  await user.type(search, 'Night');
  expect(screen.getAllByRole('article')).toHaveLength(4);
  await user.selectOptions(screen.getByLabelText('筛选标签分类'), 'composition');
  expect(screen.getAllByRole('article')).toHaveLength(3);
  await user.selectOptions(screen.getByLabelText('筛选内容分级'), 'mature');
  expect(screen.getAllByRole('article')).toHaveLength(2);
  await user.selectOptions(screen.getByLabelText('筛选启用状态'), 'disabled');
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(screen.getByRole('article', { name: '提示标签 Night target' })).toBeInTheDocument();
  await user.type(search, ' missing');
  expect(screen.queryByRole('article')).not.toBeInTheDocument();
  expect(screen.getByText('没有符合条件的标签。')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '清空筛选' }));
  expect(screen.getAllByRole('article')).toHaveLength(5);
  expect(search).toHaveValue('');
  await user.click(screen.getByRole('button', { name: '继续编辑' }));
  expect(screen.getByLabelText('标签名称')).toHaveValue('未保存修改');
  expect(calls.every((call) => call.method === 'GET')).toBe(true);
});

it('keeps the selected category visible when editing its last tag into a different category', async () => {
  let tag = first;
  fakeBackend({
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [tag], revision: 1, nextCursor: null } }),
    ['PUT /api/roles/r1/novelai/prompt-tags/' + first.id]: (request) => {
      tag = { ...tag, ...JSON.parse(String(request?.body)) }; return { body: { tag, revision: 2 } };
    },
  });
  const user = userEvent.setup(); render(<PromptTagManager roleId="r1" expanded />);
  await screen.findByRole('article', { name: '提示标签 雨景' });
  await user.selectOptions(screen.getByLabelText('筛选标签分类'), 'atmosphere');
  await user.click(screen.getByRole('button', { name: '编辑标签 雨景' }));
  await user.clear(screen.getByLabelText('标签分类')); await user.type(screen.getByLabelText('标签分类'), 'composition');
  await user.click(screen.getByRole('button', { name: '保存标签' }));
  await screen.findByText('标签已保存');
  await user.click(screen.getByRole('button', { name: '返回标签库' }));
  await screen.findByText('没有符合条件的标签。');
  expect(screen.getByLabelText('筛选标签分类')).toHaveValue('atmosphere');
  await user.click(screen.getByRole('button', { name: '清空筛选' }));
  expect(screen.getByRole('article', { name: '提示标签 雨景' })).toBeInTheDocument();
});

it.each(['failed page', 'changed library'])('does not present partial search results after a %s and can reload', async (fault) => {
  let fail = true;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [first], revision: 1, nextCursor: first.id } }),
    ['GET /api/roles/r1/novelai/prompt-tags\\?limit=50&cursor=' + first.id]: () => fail && fault === 'failed page'
      ? { status: 503, body: { detail: '暂时无法加载标签，请刷新后重试。' } }
      : { body: { tags: [second], revision: fail ? 2 : 1, nextCursor: null } },
  });
  const user = userEvent.setup(); render(<PromptTagManager roleId="r1" expanded />);
  expect(await screen.findByRole('alert')).toHaveTextContent('刷新');
  expect(screen.queryByRole('article')).not.toBeInTheDocument();
  expect(screen.queryByText(/显示 \d+ \/ \d+ 个标签/)).not.toBeInTheDocument();
  expect(screen.queryByText('没有符合条件的标签。')).not.toBeInTheDocument();
  fail = false;
  await user.click(screen.getByRole('button', { name: '刷新标签库' }));
  await screen.findByRole('article', { name: '提示标签 夜景' });
  expect(screen.getAllByRole('article')).toHaveLength(2);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(calls.every((call) => call.method === 'GET')).toBe(true);
});

it('clears search on role changes and ignores a late subsequent page after A-B-A', async () => {
  let resolveOld: ((value: { body: unknown }) => void) | undefined;
  let visits = 0;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ++visits === 1
      ? { body: { tags: [first], revision: 1, nextCursor: first.id } }
      : { body: { tags: [second], revision: 3, nextCursor: null } },
    ['GET /api/roles/r1/novelai/prompt-tags\\?limit=50&cursor=' + first.id]: () => new Promise((resolve) => { resolveOld = resolve; }),
    'GET /api/roles/r2/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [], revision: 1, nextCursor: null } }),
  });
  const user = userEvent.setup(); const view = render(<PromptTagManager roleId="r1" expanded />);
  await waitFor(() => expect(resolveOld).toBeDefined());
  await user.type(screen.getByRole('searchbox', { name: '搜索提示标签' }), 'old query');
  view.rerender(<PromptTagManager roleId="r2" expanded />);
  await screen.findByText('暂无提示标签。');
  expect(screen.getByRole('searchbox', { name: '搜索提示标签' })).toHaveValue('');
  view.rerender(<PromptTagManager roleId="r1" expanded />);
  await screen.findByRole('article', { name: '提示标签 夜景' });
  await act(async () => { resolveOld?.({ body: { tags: [first], revision: 1, nextCursor: 'c'.repeat(32) } }); });
  expect(screen.queryByRole('article', { name: '提示标签 雨景' })).not.toBeInTheDocument();
  expect(screen.getByText('显示 1 / 1 个标签')).toBeInTheDocument();
  expect(calls.filter((call) => call.path.includes('&cursor='))).toHaveLength(1);
});

it('manages the complete role library and previews the exact server expansion without generating', async () => {
  let tags = [first];
  const { calls } = fakeBackend({ ...routes,
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ({ body: { tags, revision: 1, nextCursor: second.id } }),
    ['GET /api/roles/r1/novelai/prompt-tags\\?limit=50&cursor=' + second.id]: () => ({ body: { tags: [second], revision: 1, nextCursor: null } }),
    'POST /api/roles/r1/attachments': () => ({ body: { id: 'c'.repeat(32), name: 'reference.png', mediaType: 'image/png', size: 4, kind: 'image' } }),
    'POST /api/roles/r1/novelai/prompt-tags': (request) => { const input = JSON.parse(String(request?.body)); tags = [...tags, { ...input, id: 'c'.repeat(32) }]; return { body: { tag: tags.at(-1), revision: 2 } }; },
    ['PUT /api/roles/r1/novelai/prompt-tags/' + first.id]: (request) => { tags = [{ ...JSON.parse(String(request?.body)), id: first.id }]; return { body: { tag: tags[0], revision: 3 } }; },
    ['DELETE /api/roles/r1/novelai/prompt-tags/' + first.id]: () => { tags = []; return { body: { revision: 4 } }; },
    'POST /api/roles/r1/novelai/prompt-tags/preview': () => ({ body: { prompt: '1girl, rain', negative_prompt: 'sunny', matchedIds: [first.id], revision: 1 } }),
  });
  const user = userEvent.setup(); render(drawer('r1'));
  await screen.findByText('暂无生图历史。');
  await user.click(screen.getByRole('button', { name: '管理提示标签' }));
  const library = await screen.findByRole('region', { name: '本角色提示标签库' });
  await within(library).findByText('雨景');
  expect(await within(library).findByText('夜景')).toBeInTheDocument();
  await user.click(screen.getByText('本地匹配预览'));
  await user.type(screen.getByLabelText('预览当前主人输入'), '正在下雨');
  await user.type(screen.getByLabelText('预览基础正向 tags'), '1girl');
  await user.click(screen.getByRole('button', { name: '预览标签扩写' }));
  expect(await screen.findByText('1girl, rain')).toBeInTheDocument();
  expect(screen.getByText(/命中标签：雨景/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '新建标签' }));
  await user.type(screen.getByLabelText('标签名称'), '新标签');
  await user.type(screen.getByLabelText('匹配关键词（每行一个）'), '海边');
  await user.type(screen.getByLabelText('标签正向 tags'), 'beach');
  await user.selectOptions(screen.getByLabelText('标签 rating'), 'mature');
  await user.click(screen.getByRole('checkbox', { name: '启用此标签' }));
  const reference = new File(['data'], 'reference.png', { type: 'image/png' });
  await user.upload(screen.getByLabelText('上传参考图'), reference);
  await waitFor(() => expect(screen.getByAltText('当前参考图')).toBeInTheDocument());
  await user.click(screen.getByRole('button', { name: '保存标签' }));
  await screen.findByText('标签已保存');
  await user.click(screen.getByRole('button', { name: '返回标签库' }));
  expect(await screen.findByText('新标签')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '编辑标签 雨景' }));
  await user.clear(screen.getByLabelText('标签名称')); await user.type(screen.getByLabelText('标签名称'), '小雨');
  await user.click(screen.getByRole('button', { name: '保存标签' }));
  await screen.findByText('标签已保存');
  await user.click(screen.getByRole('button', { name: '返回标签库' }));
  expect(await screen.findByText('小雨')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '删除标签 小雨' }));
  await user.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(screen.queryByText('小雨')).not.toBeInTheDocument());
  expect(calls.filter((call) => call.method === 'POST').map((call) => call.path)).toEqual([
    '/api/roles/r1/novelai/prompt-tags/preview', '/api/roles/r1/attachments', '/api/roles/r1/novelai/prompt-tags']);
  expect(calls.find((call) => call.path === '/api/roles/r1/novelai/prompt-tags' && call.method === 'POST')?.body)
    .toEqual({ name: '新标签', category: 'composition', keywords: ['海边'], prompt: 'beach', negative_prompt: '', rating: 'mature', enabled: false, reference_attachment_id: 'c'.repeat(32) });
});

it('rejects oversized keyword lists locally and ignores a late preview after A-B-A switching', async () => {
  let resolvePreview: ((value: { body: unknown }) => void) | undefined;
  const { calls } = fakeBackend({ ...routes,
    'GET /api/roles/r[12]/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [first], revision: 1, nextCursor: null } }),
    'POST /api/roles/r1/novelai/prompt-tags/preview': () => new Promise((resolve) => { resolvePreview = resolve; }),
  });
  const user = userEvent.setup(); const view = render(drawer('r1'));
  await screen.findByText('暂无生图历史。');
  await user.click(screen.getByRole('button', { name: '管理提示标签' }));
  await screen.findByText('雨景');
  await user.click(screen.getByRole('button', { name: '新建标签' }));
  await user.type(screen.getByLabelText('标签名称'), '过多词条');
  fireEvent.change(screen.getByLabelText('匹配关键词（每行一个）'), { target: { value: Array.from({ length: 17 }, (_, index) => `关键词${index}`).join('\n') } });
  await user.click(screen.getByRole('button', { name: '保存标签' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('1–16 个关键词');
  expect(calls.some((call) => call.method === 'POST')).toBe(false);
  await user.click(screen.getByRole('button', { name: '返回标签库' }));
  await user.click(screen.getByText('本地匹配预览'));
  await user.type(screen.getByLabelText('预览当前主人输入'), '下雨');
  await user.click(screen.getByRole('button', { name: '预览标签扩写' }));
  await waitFor(() => expect(resolvePreview).toBeDefined());
  view.rerender(drawer('r2')); await screen.findByText('r2的生图与历史');
  view.rerender(drawer('r1')); await screen.findByText('r1的生图与历史');
  await user.click(screen.getByRole('button', { name: '管理提示标签' }));
  await screen.findByText('雨景');
  await act(async () => { resolvePreview?.({ body: { prompt: 'stale-expansion', negative_prompt: '', matchedIds: [first.id], revision: 1 } }); });
  await waitFor(() => expect(screen.queryByText('stale-expansion')).not.toBeInTheDocument());
  expect(screen.getByLabelText('预览当前主人输入')).toHaveValue('');
});

it('isolates old library and preview responses through role A-B-A and shows local errors without consumption', async () => {
  let resolveOld: ((value: { body: unknown }) => void) | undefined;
  let visits = 0;
  const { calls } = fakeBackend({ ...routes,
    'GET /api/roles/r1/novelai/prompt-tags\\?limit=50': () => ++visits === 1
      ? new Promise((resolve) => { resolveOld = resolve; }) : { body: { tags: [second], revision: 2, nextCursor: null } },
    'GET /api/roles/r2/novelai/prompt-tags\\?limit=50': () => ({ body: { tags: [], revision: 1, nextCursor: null } }),
    'POST /api/roles/r1/novelai/prompt-tags/preview': () => ({ status: 422, body: { detail: '扩写结果过长，请缩短 tags' } }),
  });
  const user = userEvent.setup(); const view = render(drawer('r1'));
  await screen.findByText('暂无生图历史。');
  await user.click(screen.getByRole('button', { name: '管理提示标签' }));
  await waitFor(() => expect(resolveOld).toBeDefined());
  view.rerender(drawer('r2')); await screen.findByText('r2的生图与历史');
  view.rerender(drawer('r1')); await screen.findByText('r1的生图与历史');
  await user.click(screen.getByRole('button', { name: '管理提示标签' }));
  await screen.findByText('夜景');
  await act(async () => { resolveOld?.({ body: { tags: [first], revision: 1, nextCursor: null } }); });
  await waitFor(() => expect(screen.queryByText('雨景')).not.toBeInTheDocument());
  await user.click(screen.getByText('本地匹配预览'));
  await user.type(screen.getByLabelText('预览当前主人输入'), '晚上');
  await user.click(screen.getByRole('button', { name: '预览标签扩写' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('扩写结果过长，请缩短 tags');
  await user.click(within(screen.getByRole('alert')).getByRole('button', { name: '关闭提示' }));
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(calls.some((call) => call.path.includes('/generations') && call.method === 'POST')).toBe(false);
});
