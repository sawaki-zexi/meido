import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { fakeBackend } from '../../test/fakeBackend';
import { SaveMaterialAction } from './SaveMaterialAction';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('saves the actual message image with empty optional meaning once and preserves GIF selection', async () => {
  let finish: (reply: { body: unknown }) => void = () => {};
  const { calls } = fakeBackend({ 'POST /api/roles/r1/materials/from-message': init => {
    expect(JSON.parse(String(init?.body))).toEqual({ messageId: 'message1', attachmentId: 'gif1', description: '' });
    return new Promise(resolve => { finish = resolve; });
  } });
  const user = userEvent.setup();
  render(<SaveMaterialAction roleId="r1" messageId="message1" attachmentId="gif1" />);
  await user.click(screen.getByRole('button', { name: '保存为表情' }));
  expect(screen.getByRole('textbox', { name: '说明（可选）' })).toHaveValue('');
  expect(screen.getByText(/留空时不会自动推荐/)).toBeVisible();
  await user.dblClick(screen.getByRole('button', { name: '确认保存' }));
  expect(screen.getByRole('button', { name: '保存中…' })).toBeDisabled();
  await act(async () => finish({ body: { deduplicated: false } }));
  expect(screen.getByRole('status')).toHaveTextContent('已保存到素材库');
  expect(calls).toHaveLength(1);
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('keeps a rejected description for explicit correction and never retries automatically', async () => {
  const { calls } = fakeBackend({ 'POST /api/roles/r1/materials/from-message': () => ({ status: 404, body: { detail: '图片不属于当前角色' } }) });
  const user = userEvent.setup();
  render(<SaveMaterialAction roleId="r1" messageId="message1" attachmentId="gif1" />);
  await user.click(screen.getByRole('button', { name: '保存为表情' }));
  await user.type(screen.getByRole('textbox'), '开心');
  await user.click(screen.getByRole('button', { name: '确认保存' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('图片不属于当前角色');
  expect(screen.getByRole('textbox')).toHaveValue('开心');
  expect(calls).toHaveLength(1);
});

it('reports that a conflicting duplicate description was retained only as a proposal', async () => {
  fakeBackend({ 'POST /api/roles/r1/materials/from-message': () => ({ body: { deduplicated: true, descriptionOutcome: 'proposal_retained' } }) });
  const user = userEvent.setup();
  render(<SaveMaterialAction roleId="r1" messageId="message1" attachmentId="gif1" />);
  await user.click(screen.getByRole('button', { name: '保存为表情' }));
  await user.type(screen.getByRole('textbox'), '不同的说明');
  await user.click(screen.getByRole('button', { name: '确认保存' }));
  expect(await screen.findByRole('status')).toHaveTextContent('另存为建议，暂不用于聊天');
});
