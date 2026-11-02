import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RoleCardExport } from './RoleCardExport';

const summary = (format: string) => ({ format, fields: ['名称', '角色设定'], includedAssets: [], omittedAssets: ['头像'], notes: ['JSON 只包含设定，不包含图片。'] });
function response(format: string) {
  const encoded = btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(summary(format)))));
  return new Response('card bytes', { headers: { 'X-Meido-Export-Summary': encoded,
    'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(`爱丽丝.${format}`)}` } });
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => 'blob:test-card');
    static revokeObjectURL = vi.fn();
  });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each(['json', 'png', 'charx'])('downloads %s with matching summary, Unicode filename and released Blob URL', async format => {
  vi.mocked(fetch).mockResolvedValue(response(format));
  const user = userEvent.setup();
  render(<RoleCardExport roleId="alice" revision="1" blockedReason="" />);
  await user.click(screen.getByRole('button', { name: '导出角色卡' }));
  await user.selectOptions(screen.getByLabelText('导出格式'), format);
  const links: string[] = [];
  vi.mocked(HTMLAnchorElement.prototype.click).mockImplementation(function(this: HTMLAnchorElement) { links.push(this.download); });
  await user.click(screen.getByRole('button', { name: '下载角色卡' }));
  expect(await screen.findByRole('status')).toHaveTextContent(`${format.toUpperCase()} 已下载`);
  expect(screen.getByRole('status')).toHaveTextContent('未包含图片：头像');
  expect(fetch).toHaveBeenCalledWith(`/api/roles/alice/export?format=${format}`, expect.objectContaining({ signal: expect.any(AbortSignal) }));
  expect(links).toEqual([`爱丽丝.${format}`]);
  await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:test-card'));
});

it('blocks unsaved, cropping and saving states without requesting an old saved version', async () => {
  const user = userEvent.setup();
  const view = render(<RoleCardExport roleId="alice" revision="1" blockedReason="请先保存角色修改，再导出。" />);
  await user.click(screen.getByRole('button', { name: '导出角色卡' }));
  for (const reason of ['请先保存角色修改，再导出。', '请完成图片裁剪并保存后再导出。', '正在保存，请稍后导出。']) {
    view.rerender(<RoleCardExport roleId="alice" revision="1" blockedReason={reason} />);
    expect(screen.getByText(reason)).toBeVisible();
    expect(screen.getByRole('button', { name: '下载角色卡' })).toBeDisabled();
  }
  expect(fetch).not.toHaveBeenCalled();
});

it('shows actionable backend failure and retries without losing chosen format', async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ detail: '请重新保存图片或改用 JSON' }), { status: 422 }))
    .mockResolvedValueOnce(response('png'));
  const user = userEvent.setup();
  render(<RoleCardExport roleId="alice" revision="1" blockedReason="" />);
  await user.click(screen.getByRole('button', { name: '导出角色卡' }));
  await user.selectOptions(screen.getByLabelText('导出格式'), 'png');
  await user.click(screen.getByRole('button', { name: '下载角色卡' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('请重新保存图片或改用 JSON');
  expect(URL.createObjectURL).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '下载角色卡' }));
  expect(await screen.findByRole('status')).toHaveTextContent('PNG 已下载');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it.each(['role', 'revision', 'dirty', 'unmount'])('ignores a late export after %s changes, even if transport ignores abort', async change => {
  let finish!: (value: Response) => void;
  vi.mocked(fetch).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const user = userEvent.setup();
  const view = render(<RoleCardExport roleId="alice" revision="1" blockedReason="" />);
  await user.click(screen.getByRole('button', { name: '导出角色卡' }));
  await user.click(screen.getByRole('button', { name: '下载角色卡' }));
  const signal = vi.mocked(fetch).mock.calls[0][1]?.signal;
  if (change === 'unmount') view.unmount();
  else view.rerender(<RoleCardExport roleId={change === 'role' ? 'bella' : 'alice'} revision={change === 'revision' ? '2' : '1'} blockedReason={change === 'dirty' ? '请先保存角色修改，再导出。' : ''} />);
  expect(signal?.aborted).toBe(true);
  await act(async () => { finish(response('json')); });
  expect(URL.createObjectURL).not.toHaveBeenCalled();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('does not download if a response lacks matching artifact summary', async () => {
  vi.mocked(fetch).mockResolvedValue(response('charx'));
  const user = userEvent.setup();
  render(<RoleCardExport roleId="alice" revision="1" blockedReason="" />);
  await user.click(screen.getByRole('button', { name: '导出角色卡' }));
  await user.click(screen.getByRole('button', { name: '下载角色卡' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('导出摘要无效');
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});
