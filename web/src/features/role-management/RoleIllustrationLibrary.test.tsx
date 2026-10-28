import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { RoleIllustrationLibrary } from './RoleIllustrationLibrary';
import { GeneratedAssetActions } from '../novelai/GeneratedAssetActions';
const illustration = { id: 'i1', url: '/owned-image', name: 'garden.png', mediaType: 'image/png', size: 100, category: '未分类' };
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('selects and clears a background, then confirms recoverable illustration removal', async () => {
  let current = role({ id: 'r1', name: '诗织', illustrations: [illustration], illustrationCategories: ['未分类'] });
  const { calls } = fakeBackend({
    'GET /api/roles/r1/illustrations': () => ({ body: { role: current } }),
    'PUT /api/roles/r1/chat-background': init => { const value = JSON.parse(String(init?.body)).illustrationId;
      current = { ...current, chatBackgroundId: value, chatBackgroundUrl: value ? illustration.url : null }; return { body: { role: current } }; },
    'DELETE /api/roles/r1/illustrations/i1': () => { current = { ...current, illustrations: [] }; return { body: { role: current } }; },
  });
  const user = userEvent.setup();
  render(<RoleIllustrationLibrary role={current} onRoleSaved={() => {}} />);
  await user.click(await screen.findByRole('button', { name: '查看插画 garden.png' }));
  await user.click(screen.getByRole('button', { name: '设为聊天背景' }));
  await screen.findByText('已设为聊天背景。');
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('button', { name: '恢复默认聊天背景' }));
  await screen.findByText('已恢复默认聊天背景，插画仍保留。');
  await user.click(screen.getByRole('button', { name: '查看插画 garden.png' }));
  await user.click(screen.getByRole('button', { name: '移出插画库' }));
  expect(calls.filter(call => call.method === 'DELETE')).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: '确认移出插画' }));
  await screen.findByText('暂无角色插画。');
  expect(calls.filter(call => call.method === 'DELETE')).toHaveLength(1);
});
it('saves an owned history image as background without creating a generation', async () => {
  const owner = role({ id: 'r1', name: '诗织', illustrations: [illustration], chatBackgroundId: illustration.id, chatBackgroundUrl: illustration.url });
  const { calls } = fakeBackend({ 'POST /api/roles/r1/illustrations/from-attachment': () => ({ body: { role: owner } }) });
  const applied = vi.fn();
  render(<GeneratedAssetActions roleId="r1" attachmentId="image-original" onApplied={applied} />);
  await userEvent.click(screen.getByRole('button', { name: '设为聊天背景' }));
  await waitFor(() => expect(applied).toHaveBeenCalledWith(owner));
  expect(calls.filter(call => call.method === 'POST')).toMatchObject([{ path: '/api/roles/r1/illustrations/from-attachment', body: { attachmentId: 'image-original', setBackground: true } }]);
});
it('binds mood and separately grants category sending without granting other categories', async () => {
  let current = role({ id: 'r1', name: '诗织', illustrations: [illustration], illustrationCategories: ['未分类'], illustrationCategorySend: {} });
  const { calls } = fakeBackend({
    'GET /api/roles/r1/illustrations': () => ({ body: { role: current } }),
    'PATCH /api/roles/r1/illustrations': init => { const value = JSON.parse(String(init?.body));
      current = { ...current, ...(value.moodBindings ? { moodIllustrationBindings: value.moodBindings, defaultMood: '开心' } : {}),
        ...(value.categorySend ? { illustrationCategorySend: value.categorySend } : {}) }; return { body: { role: current } }; },
  });
  const user = userEvent.setup();
  render(<RoleIllustrationLibrary role={current} onRoleSaved={() => {}} />);
  await user.click(await screen.findByRole('button', { name: '查看插画 garden.png' }));
  await user.type(screen.getByRole('textbox', { name: '绑定心情' }), '开心');
  await user.click(screen.getByRole('button', { name: '保存心情绑定' }));
  await screen.findByText('心情插画绑定已保存。');
  expect(current.illustrationCategorySend).toEqual({});
  await user.keyboard('{Escape}');
  expect(screen.getByRole('combobox', { name: '默认心情' })).toHaveValue('开心');
  await user.selectOptions(screen.getByRole('combobox', { name: '插画分类' }), '未分类');
  await user.click(screen.getByRole('checkbox', { name: /允许角色发送此分类插画/ }));
  await screen.findByText('角色可以在获准会话中发送此分类插画。');
  expect(calls.filter(call => call.method === 'PATCH')).toMatchObject([
    { body: { moodBindings: { 开心: 'i1' } } }, { body: { categorySend: { 未分类: true } } },
  ]);
});
it('uploads every selected file and confirms category removal through recoverable image actions', async () => {
  let current = role({ id: 'r1', name: '诗织', illustrations: [illustration], illustrationCategories: ['未分类', '春日'] });
  let uploaded = 0;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/illustrations': () => ({ body: { role: current } }),
    'POST /api/roles/r1/illustrations\\?.*': (init, match) => {
      const params = new URL(match[0].slice('POST '.length), 'http://local').searchParams;
      const file = init?.body as File;
      expect(params.get('name')).toBe(file.name);
      expect(params.get('category')).toBe('春日');
      uploaded++;
      current = { ...current, illustrations: [...current.illustrations!, { ...illustration, id: `upload-${uploaded}`, name: file.name, category: '春日' }] };
      return { body: { role: current } };
    },
    'DELETE /api/roles/r1/illustrations/(upload-[12])': (_init, match) => {
      current = { ...current, illustrations: current.illustrations!.filter(item => item.id !== match[1]) };
      return { body: { role: current } };
    },
    'PATCH /api/roles/r1/illustrations': init => {
      current = { ...current, illustrationCategories: JSON.parse(String(init?.body)).categories };
      return { body: { role: current } };
    },
  });
  const user = userEvent.setup();
  render(<RoleIllustrationLibrary role={current} onRoleSaved={() => {}} />);
  await user.selectOptions(screen.getByRole('combobox', { name: '插画分类' }), '春日');
  await user.upload(screen.getByLabelText('上传角色插画'), [
    new File(['first'], 'first.png', { type: 'image/png' }), new File(['second'], 'second.png', { type: 'image/png' }),
  ]);
  await screen.findByText('2 张插画已保存。');
  expect(calls.filter(call => call.method === 'POST')).toHaveLength(2);
  expect(screen.getByRole('button', { name: '查看插画 first.png' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '查看插画 second.png' })).toBeInTheDocument();
  await user.click(screen.getByText('管理插画分类'));
  expect(screen.getByRole('button', { name: '移除所选分类并保留插画' })).toBeEnabled();
  await user.click(screen.getByRole('button', { name: '移除所选分类及其中插画' }));
  expect(calls.filter(call => call.method === 'DELETE')).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: '取消' }));
  expect(calls.filter(call => call.method === 'DELETE')).toHaveLength(0);
  await user.click(screen.getByRole('button', { name: '移除所选分类及其中插画' }));
  await user.click(screen.getByRole('button', { name: '确认移除分类及插画' }));
  await screen.findByText('分类及其中插画已移出；原文件保留在本地恢复区，仅支持手动恢复。');
  expect(current.illustrationCategories).toEqual(['未分类']);
  expect(current.illustrations).toEqual([illustration]);
  expect(calls.filter(call => call.method === 'DELETE').map(call => call.path)).toEqual([
    '/api/roles/r1/illustrations/upload-1', '/api/roles/r1/illustrations/upload-2',
  ]);
  expect(screen.getByRole('button', { name: '查看插画 garden.png' })).toBeInTheDocument();
});
