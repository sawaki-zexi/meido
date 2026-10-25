import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { MaterialLibrary, RoleMaterial } from '../../api/materials';
import { fakeBackend } from '../../test/fakeBackend';
import { RoleMaterialLibrary } from './RoleMaterialLibrary';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const material = (overrides: Partial<RoleMaterial> = {}): RoleMaterial => ({ id: 'm1', name: '开心.gif', category: '开心', description: '挥手打招呼', aliases: ['你好'], format: 'GIF', mediaType: 'image/gif', size: 1200, width: 120, height: 60, animated: true, source: 'owner_import', enabled: true, imageUrl: '/api/roles/r1/materials/m1/image', ...overrides });
const library = (items = [material()], revision = 'rev1'): MaterialLibrary => ({ materials: items, categories: [...new Set(items.map(item => item.category))].map(name => ({ name, description: '', count: items.filter(item => item.category === name).length, enabled: true })), revision, storageLabel: '当前角色工作区 / workspace/memes' });
const routes = (body = library()) => ({ 'GET /api/roles/r1/materials': () => ({ body }) });

it('shows owner-scoped storage, category/search, original GIF preview and returns focus after closing', async () => {
  const { calls } = fakeBackend(routes(library([material(), material({ id: 'm2', name: '晚安.png', category: '晚安', description: '休息一下', aliases: [], format: 'PNG', animated: false })])));
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  expect(await screen.findByText('开心.gif')).toBeVisible();
  expect(screen.getByText(/workspace\/memes/)).toBeVisible();
  expect(screen.getByText(/不需要开启 Drift/)).toBeVisible();
  await user.selectOptions(screen.getByRole('combobox', { name: '素材分类' }), '晚安');
  expect(screen.queryByText('开心.gif')).not.toBeInTheDocument();
  await user.selectOptions(screen.getByRole('combobox', { name: '素材分类' }), '');
  await user.type(screen.getByRole('searchbox', { name: '搜索素材' }), '你好');
  expect(screen.queryByText('晚安.png')).not.toBeInTheDocument();
  const opener = screen.getByRole('button', { name: '预览开心.gif' });
  await user.click(opener);
  const dialog = screen.getByRole('dialog', { name: '开心.gif' });
  expect(within(dialog).getByRole('img', { name: '开心.gif 原图' })).toHaveAttribute('src', '/api/roles/r1/materials/m1/image');
  expect(within(dialog).getByText('GIF · 动图')).toBeVisible();
  expect(within(dialog).getByText('主人导入')).toBeVisible();
  expect(within(dialog).getByText('挥手打招呼')).toBeVisible();
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
  expect(calls.every(call => call.method === 'GET' && !call.path.includes('generations'))).toBe(true);
});

it('handles loading, failed list retry, empty library and empty filters distinctly', async () => {
  let tries = 0;
  let finish: (reply: { body: MaterialLibrary }) => void = () => {};
  fakeBackend({ 'GET /api/roles/r1/materials': () => ++tries === 1 ? { status: 503, body: { detail: '暂时无法读取素材库' } } : new Promise(resolve => { finish = resolve; }) });
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  expect(screen.getByRole('status')).toHaveTextContent('正在读取素材库');
  expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法读取素材库');
  await user.click(screen.getByRole('button', { name: '重新读取素材库' }));
  await act(async () => finish({ body: library([]) }));
  expect(await screen.findByText('还没有素材')).toBeVisible();
});

it('shows broken image placeholder and retries without losing preview access', async () => {
  fakeBackend(routes());
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  const thumb = await screen.findByRole('img', { name: '开心.gif' });
  fireEvent.error(thumb);
  expect(screen.getByText('图片暂时无法显示')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '重试加载开心.gif' }));
  expect(screen.getByRole('img', { name: '开心.gif' })).toHaveAttribute('src', '/api/roles/r1/materials/m1/image');
  await user.click(screen.getByRole('button', { name: '预览开心.gif' }));
  expect(screen.getByRole('dialog')).toBeVisible();
});

it('validates files, preserves original bytes and metadata, then refreshes after an import', async () => {
  let imported = false;
  let posted: RequestInit | undefined;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/materials': () => ({ body: library(imported ? [material({ name: '新图.gif' })] : [], imported ? 'rev2' : 'rev1') }),
    'POST /api/roles/r1/materials\\?.*': init => { posted = init; imported = true; return { body: { material: material({ name: '新图.gif' }), deduplicated: false, revision: 'rev2' } }; },
  });
  const user = userEvent.setup({ applyAccept: false });
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await screen.findByText('还没有素材');
  await user.click(screen.getByRole('button', { name: '导入素材' }));
  const dialog = screen.getByRole('dialog', { name: '导入素材' });
  const input = within(dialog).getByLabelText('图片文件');
  await user.upload(input, new File(['bad'], 'bad.svg', { type: 'image/svg+xml' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('PNG、JPEG、WebP 或 GIF');
  const file = new File(['original-gif'], '新图.gif', { type: 'image/gif' });
  await user.upload(input, file);
  await user.type(within(dialog).getByRole('textbox', { name: '分类' }), '开心');
  await user.type(within(dialog).getByRole('textbox', { name: '描述（可选）' }), '挥手');
  await user.click(within(dialog).getByRole('button', { name: '确认导入' }));
  expect(await screen.findByText('新图.gif')).toBeVisible();
  expect(posted?.body).toBe(file);
  expect(posted?.headers).toMatchObject({ 'content-type': 'image/gif', 'x-material-filename': encodeURIComponent('新图.gif') });
  expect(calls.find(call => call.method === 'POST')?.path).toContain('expectedRevision=rev1');
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(1);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole('button', { name: '导入素材' })).toHaveFocus());
});

it('requires confirmation to remove, prevents repeated deletes and preserves recovery semantics', async () => {
  let complete: (reply: { body: unknown }) => void = () => {};
  let removed = false;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/materials': () => ({ body: library(removed ? [] : [material()], removed ? 'rev2' : 'rev1') }),
    'DELETE /api/roles/r1/materials/m1\\?expectedRevision=rev1': () => new Promise(resolve => { complete = resolve; }),
  });
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await user.click(await screen.findByRole('button', { name: '预览开心.gif' }));
  await user.click(screen.getByRole('button', { name: '移出素材库' }));
  expect(screen.getByRole('dialog', { name: '移出素材库？' })).toHaveTextContent('本地恢复区');
  await user.click(screen.getByRole('button', { name: '取消' }));
  expect(calls.filter(call => call.method === 'DELETE')).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: '移出素材库' }));
  const confirm = screen.getByRole('button', { name: '确认移出' });
  await user.dblClick(confirm);
  expect(confirm).toBeDisabled();
  removed = true;
  await act(async () => complete({ body: { removed: true, recoverable: true, revision: 'rev2' } }));
  expect(await screen.findByText('还没有素材')).toBeVisible();
  expect(screen.getByRole('status')).toHaveTextContent('原图保留在本地恢复区');
  expect(calls.filter(call => call.method === 'DELETE')).toHaveLength(1);
  await waitFor(() => expect(screen.getByRole('button', { name: '导入素材' })).toHaveFocus());
});

it('keeps an uncertain import draft and requires a fresh read before an explicit retry', async () => {
  const { calls } = fakeBackend({ ...routes(library([])), 'POST /api/roles/r1/materials\\?.*': () => ({ status: 503, body: { detail: '导入结果未确认，请重新读取' } }) });
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await screen.findByText('还没有素材');
  await user.click(screen.getByRole('button', { name: '导入素材' }));
  await user.upload(screen.getByLabelText('图片文件'), new File(['png'], 'one.png', { type: 'image/png' }));
  await user.type(screen.getByRole('textbox', { name: '分类' }), '开心');
  await user.click(screen.getByRole('button', { name: '确认导入' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('导入结果未确认');
  expect(screen.getByRole('button', { name: '确认导入' })).toBeDisabled();
  expect(screen.getByRole('textbox', { name: '分类' })).toHaveValue('开心');
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '重新读取素材库' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '确认导入' })).toBeEnabled());
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(1);
});

it('ignores stale list results and removes the old role preview on role change', async () => {
  let finish: (reply: { body: MaterialLibrary }) => void = () => {};
  fakeBackend({ 'GET /api/roles/r1/materials': () => new Promise(resolve => { finish = resolve; }), 'GET /api/roles/r2/materials': () => ({ body: library([material({ name: '另一角色.png', imageUrl: '/api/roles/r2/materials/m1/image' })]) }) });
  const { rerender } = render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  rerender(<RoleMaterialLibrary roleId="r2" roleName="贝拉" />);
  expect(await screen.findByText('另一角色.png')).toBeVisible();
  await act(async () => finish({ body: library() }));
  expect(screen.queryByText('开心.gif')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '预览另一角色.png' }));
  rerender(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.queryByText('另一角色.png')).not.toBeInTheDocument();
});

it('shows no matching results with a clear-filter recovery path', async () => {
  fakeBackend(routes());
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await screen.findByText('开心.gif');
  await user.type(screen.getByRole('searchbox', { name: '搜索素材' }), '不存在的素材');
  expect(screen.getByText('没有匹配的素材')).toBeVisible();
  expect(screen.queryByText('还没有素材')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '清除筛选' }));
  expect(screen.getByText('开心.gif')).toBeVisible();
});

it('closing an import in flight neither repeats it nor reopens its dialog when the result arrives', async () => {
  let complete: (reply: { body: unknown }) => void = () => {};
  let imported = false;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/materials': () => ({ body: library(imported ? [material()] : []) }),
    'POST /api/roles/r1/materials\\?.*': () => new Promise(resolve => { complete = resolve; }),
  });
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await screen.findByText('还没有素材');
  await user.click(screen.getByRole('button', { name: '导入素材' }));
  await user.upload(screen.getByLabelText('图片文件'), new File(['gif'], '开心.gif', { type: 'image/gif' }));
  await user.dblClick(screen.getByRole('button', { name: '确认导入' }));
  expect(screen.getByRole('button', { name: '导入中…' })).toBeDisabled();
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '导入素材' })).toBeDisabled();
  await user.click(screen.getByRole('searchbox', { name: '搜索素材' }));
  imported = true;
  await act(async () => complete({ body: { material: material(), deduplicated: false, revision: 'rev2' } }));
  expect(await screen.findByText('开心.gif')).toBeVisible();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(1);
  expect(screen.getByRole('button', { name: '导入素材' })).toBeEnabled();
  expect(screen.getByRole('searchbox', { name: '搜索素材' })).toHaveFocus();
});

it('never shows the previous role import outcome after switching roles', async () => {
  let complete: (reply: { body: unknown }) => void = () => {};
  const { calls } = fakeBackend({ ...routes(library([])), 'POST /api/roles/r1/materials\\?.*': () => new Promise(resolve => { complete = resolve; }), 'GET /api/roles/r2/materials': () => ({ body: library([]) }) });
  const user = userEvent.setup();
  const { rerender } = render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await screen.findByText('还没有素材');
  await user.click(screen.getByRole('button', { name: '导入素材' }));
  await user.upload(screen.getByLabelText('图片文件'), new File(['png'], 'old.png', { type: 'image/png' }));
  await user.click(screen.getByRole('button', { name: '确认导入' }));
  rerender(<RoleMaterialLibrary roleId="r2" roleName="贝拉" />);
  expect(await screen.findByRole('region', { name: '贝拉的素材库' })).toBeVisible();
  await act(async () => complete({ body: { material: material(), deduplicated: false, revision: 'rev2' } }));
  expect(screen.queryByText('素材已导入当前角色的素材库。')).not.toBeInTheDocument();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(calls.filter(call => call.path === '/api/roles/r1/materials' && call.method === 'GET')).toHaveLength(1);
  expect(calls.filter(call => call.method === 'POST')[0].path).toContain('/roles/r1/');
});

it('a stale removal requires refresh and another confirmation, and read failure remains visible inside the modal', async () => {
  let reads = 0;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/materials': () => ++reads === 2 ? { status: 503, body: { detail: '读取暂不可用' } } : { body: library([material()], reads > 1 ? 'rev2' : 'rev1') },
    'DELETE /api/roles/r1/materials/m1\\?expectedRevision=rev1': () => ({ status: 409, body: { detail: '素材库已变化，请刷新' } }),
  });
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await user.click(await screen.findByRole('button', { name: '预览开心.gif' }));
  await user.click(screen.getByRole('button', { name: '移出素材库' }));
  await user.click(screen.getByRole('button', { name: '确认移出' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('素材库已变化');
  expect(screen.getByRole('button', { name: '确认移出' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: '重新读取素材库' }));
  expect(await within(screen.getByRole('dialog', { name: '移出素材库？' })).findByText('读取暂不可用')).toBeVisible();
  await user.click(screen.getByRole('button', { name: '取消' }));
  expect(screen.getByRole('dialog', { name: '开心.gif' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: '重新读取素材库' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '移出素材库' })).toBeEnabled());
  expect(calls.filter(call => call.method === 'DELETE')).toHaveLength(1);
});

it('refreshing a conflicted removal that is already absent clears confirmation before previewing another item', async () => {
  let refreshed = false;
  const second = material({ id: 'm2', name: '另一张.png', imageUrl: '/api/roles/r1/materials/m2/image' });
  const { calls } = fakeBackend({
    'GET /api/roles/r1/materials': () => ({ body: library(refreshed ? [second] : [material(), second], refreshed ? 'rev2' : 'rev1') }),
    'DELETE /api/roles/r1/materials/m1\\?expectedRevision=rev1': () => { refreshed = true; return { status: 409, body: { detail: '素材已被其他操作移出' } }; },
  });
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await user.click(await screen.findByRole('button', { name: '预览开心.gif' }));
  await user.click(screen.getByRole('button', { name: '移出素材库' }));
  await user.click(screen.getByRole('button', { name: '确认移出' }));
  await screen.findByText('素材已被其他操作移出');
  await user.click(screen.getByRole('button', { name: '重新读取素材库' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  await waitFor(() => expect(screen.getByRole('button', { name: '导入素材' })).toHaveFocus());
  await user.click(screen.getByRole('button', { name: '预览另一张.png' }));
  expect(screen.getByRole('dialog', { name: '另一张.png' })).toBeVisible();
  expect(screen.queryByRole('dialog', { name: '移出素材库？' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '移出素材库' })).toBeEnabled();
  expect(calls.filter(call => call.method === 'DELETE')).toHaveLength(1);
});

it('does not resurrect a confirmed removed item if the follow-up list read fails', async () => {
  let removed = false;
  fakeBackend({
    'GET /api/roles/r1/materials': () => removed ? { status: 503, body: { detail: '刷新暂不可用' } } : { body: library() },
    'DELETE /api/roles/r1/materials/m1\\?expectedRevision=rev1': () => { removed = true; return { body: { removed: true, recoverable: true, revision: 'rev2' } }; },
  });
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await user.click(await screen.findByRole('button', { name: '预览开心.gif' }));
  await user.click(screen.getByRole('button', { name: '移出素材库' }));
  await user.click(screen.getByRole('button', { name: '确认移出' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('刷新暂不可用');
  expect(screen.queryByRole('button', { name: '预览开心.gif' })).not.toBeInTheDocument();
  expect(screen.getByText('还没有素材')).toBeVisible();
  expect(screen.getByRole('status')).toHaveTextContent('原图保留在本地恢复区');
});

it.each(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])('accepts %s files but cancel sends nothing', async type => {
  const { calls } = fakeBackend(routes(library([])));
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await screen.findByText('还没有素材');
  await user.click(screen.getByRole('button', { name: '导入素材' }));
  await user.upload(screen.getByLabelText('图片文件'), new File(['image'], 'asset', { type }));
  expect(screen.getByRole('button', { name: '确认导入' })).toBeEnabled();
  await user.click(screen.getByRole('button', { name: '取消' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('a completed removal does not close a newer preview opened while it was in flight', async () => {
  let complete: (reply: { body: unknown }) => void = () => {};
  let removed = false;
  const second = material({ id: 'm2', name: '下一张.png', imageUrl: '/api/roles/r1/materials/m2/image' });
  fakeBackend({
    'GET /api/roles/r1/materials': () => ({ body: library(removed ? [second] : [material(), second]) }),
    'DELETE /api/roles/r1/materials/m1\\?expectedRevision=rev1': () => new Promise(resolve => { complete = resolve; }),
  });
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await user.click(await screen.findByRole('button', { name: '预览开心.gif' }));
  await user.click(screen.getByRole('button', { name: '移出素材库' }));
  await user.click(screen.getByRole('button', { name: '确认移出' }));
  await user.keyboard('{Escape}{Escape}');
  await user.click(screen.getByRole('button', { name: '预览下一张.png' }));
  removed = true;
  await act(async () => complete({ body: { removed: true, recoverable: true, revision: 'rev2' } }));
  expect(screen.getByRole('dialog', { name: '下一张.png' })).toBeVisible();
  expect(screen.queryByRole('dialog', { name: '开心.gif' })).not.toBeInTheDocument();
});

it('a delayed post-success refresh does not steal focus from newer user interaction', async () => {
  let reads = 0;
  let finishRead: (reply: { body: MaterialLibrary }) => void = () => {};
  fakeBackend({
    'GET /api/roles/r1/materials': () => ++reads === 1 ? { body: library([]) } : new Promise(resolve => { finishRead = resolve; }),
    'POST /api/roles/r1/materials\\?.*': () => ({ body: { material: material(), deduplicated: false, revision: 'rev2' } }),
  });
  const user = userEvent.setup();
  render(<RoleMaterialLibrary roleId="r1" roleName="爱丽丝" />);
  await screen.findByText('还没有素材');
  await user.click(screen.getByRole('button', { name: '导入素材' }));
  await user.upload(screen.getByLabelText('图片文件'), new File(['gif'], '开心.gif', { type: 'image/gif' }));
  await user.click(screen.getByRole('button', { name: '确认导入' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  const search = screen.getByRole('searchbox', { name: '搜索素材' });
  await user.type(search, '开心');
  await act(async () => finishRead({ body: library([material()], 'rev2') }));
  expect(search).toHaveFocus();
  expect(search).toHaveValue('开心');
});
