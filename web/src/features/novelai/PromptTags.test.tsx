import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { GenerationDrawer } from './GenerationDrawer';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const first = { id: 'a'.repeat(32), name: '雨景', category: 'atmosphere', keywords: ['下雨'], prompt: 'rain', negative_prompt: 'sunny', rating: 'general', enabled: true, reference_attachment_id: null, referenceAttachment: null };
const second = { ...first, id: 'b'.repeat(32), name: '夜景', keywords: ['晚上'], prompt: 'night' };
const routes = {
  'GET /api/services/novelai': () => ({ body: { enabled: false, revision: 1, allowedModels: [], allowedSamplers: [], dailyAttemptLimit: 20 } }),
  'GET /api/roles/r[12]/generations': () => ({ body: { generations: [], nextCursor: null, usage: { accountAttempts: 0, manualAttempts: 0 } } }),
};
const drawer = (id: string) => <GenerationDrawer role={role({ id, name: id })} onClose={() => {}} onGenerated={() => {}} />;

it('manages a role library, paginates and previews the exact server expansion without generating', async () => {
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
  await user.click(within(library).getByRole('button', { name: '加载更多标签' }));
  expect(await within(library).findByText('夜景')).toBeInTheDocument();
  await user.type(screen.getByLabelText('预览当前主人输入'), '正在下雨');
  await user.type(screen.getByLabelText('预览基础正向 tags'), '1girl');
  await user.click(screen.getByRole('button', { name: '预览标签扩写' }));
  expect(await screen.findByText('1girl, rain')).toBeInTheDocument();
  expect(screen.getByText(/命中标签：雨景/)).toBeInTheDocument();
  await user.type(screen.getByLabelText('标签名称'), '新标签');
  await user.type(screen.getByLabelText('匹配关键词（每行一个）'), '海边');
  await user.type(screen.getByLabelText('标签正向 tags'), 'beach');
  await user.selectOptions(screen.getByLabelText('标签 rating'), 'mature');
  await user.click(screen.getByRole('checkbox', { name: '启用此标签' }));
  const reference = new File(['data'], 'reference.png', { type: 'image/png' });
  await user.upload(screen.getByLabelText('上传参考图'), reference);
  await waitFor(() => expect(screen.getByAltText('当前参考图')).toBeInTheDocument());
  await user.click(screen.getByRole('button', { name: '保存标签' }));
  expect(await screen.findByText('新标签')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '编辑标签 雨景' }));
  await user.clear(screen.getByLabelText('标签名称')); await user.type(screen.getByLabelText('标签名称'), '小雨');
  await user.click(screen.getByRole('button', { name: '保存标签' }));
  expect(await screen.findByText('小雨')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '删除标签 小雨' }));
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
  await user.type(screen.getByLabelText('标签名称'), '过多词条');
  fireEvent.change(screen.getByLabelText('匹配关键词（每行一个）'), { target: { value: Array.from({ length: 17 }, (_, index) => `关键词${index}`).join('\n') } });
  await user.click(screen.getByRole('button', { name: '保存标签' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('1–16 个关键词');
  expect(calls.some((call) => call.method === 'POST')).toBe(false);
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
  await user.type(screen.getByLabelText('预览当前主人输入'), '晚上');
  await user.click(screen.getByRole('button', { name: '预览标签扩写' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('扩写结果过长，请缩短 tags');
  await user.click(within(screen.getByRole('alert')).getByRole('button', { name: '关闭提示' }));
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(calls.some((call) => call.path.includes('/generations') && call.method === 'POST')).toBe(false);
});
