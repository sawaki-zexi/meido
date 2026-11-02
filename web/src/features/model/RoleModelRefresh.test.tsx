import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, type Reply } from '../../test/fakeBackend';
import { RoleModelSelector } from './RoleModelSelector';

const first = { id: 'm1', providerId: 'openai', provider: 'OpenAI', baseUrl: 'https://example.invalid/v1', model: 'first-model', apiKeyConfigured: true };
const second = { ...first, id: 'm2', model: 'second-model' };
const trigger = () => screen.getByRole('button', { name: '选择聊天模型' });
function deferred() {
  let resolve!: (value: Reply) => void;
  const promise = new Promise<Reply>(done => { resolve = done; });
  return { promise, resolve };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('refreshes the saved catalog and default binding, including a deleted explicit binding', async () => {
  let configurations = [first, second];
  let binding = { configurationId: null as string | null, effectiveConfigurationId: first.id };
  const { calls } = fakeBackend({
    'GET /api/model/configurations': () => ({ body: { configurations } }),
    'GET /api/roles/r1/model-configuration': () => ({ body: binding }),
  });
  const changed = vi.fn();
  const view = render(<RoleModelSelector roleId="r1" revision="0" onChange={changed} />);
  await waitFor(() => expect(trigger()).toHaveTextContent(first.model));

  configurations = [first, { ...second, model: 'updated-model' }];
  binding = { configurationId: null, effectiveConfigurationId: second.id };
  view.rerender(<RoleModelSelector roleId="r1" revision="1" onChange={changed} />);
  await waitFor(() => expect(trigger()).toHaveTextContent('updated-model'));
  expect(changed).toHaveBeenLastCalledWith(configurations[1]);

  configurations = [first];
  binding = { configurationId: second.id, effectiveConfigurationId: second.id };
  view.rerender(<RoleModelSelector roleId="r1" revision="2" onChange={changed} />);
  await waitFor(() => expect(trigger()).toHaveTextContent('模型已不存在'));
  expect(screen.getByRole('status')).toHaveTextContent('当前角色绑定的模型已不存在');
  expect(changed).toHaveBeenLastCalledWith(null);
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it('keeps a same-role selection on refresh failure and recovers on a later revision', async () => {
  let fail = false;
  let active = first;
  fakeBackend({
    'GET /api/model/configurations': () => fail ? { status: 503, body: { detail: '目录暂不可用' } } : { body: { configurations: [active] } },
    'GET /api/roles/r1/model-configuration': () => ({ body: { configurationId: null, effectiveConfigurationId: active.id } }),
  });
  const changed = vi.fn();
  const view = render(<RoleModelSelector roleId="r1" revision="0" onChange={changed} />);
  await waitFor(() => expect(trigger()).toHaveTextContent(first.model));
  changed.mockClear();
  fail = true;
  view.rerender(<RoleModelSelector roleId="r1" revision="1" onChange={changed} />);
  expect(await screen.findByRole('status')).toHaveTextContent('目录暂不可用');
  expect(trigger()).toHaveTextContent(first.model);
  expect(trigger()).toBeEnabled();
  expect(changed).not.toHaveBeenCalledWith(null);

  fail = false;
  active = second;
  view.rerender(<RoleModelSelector roleId="r1" revision="2" onChange={changed} />);
  await waitFor(() => expect(trigger()).toHaveTextContent(second.model));
  expect(screen.queryByRole('status')).toBeNull();
  expect(changed).toHaveBeenLastCalledWith(second);
});

it('ignores a late catalog from an older settings revision', async () => {
  const oldCatalog = deferred();
  let reads = 0;
  fakeBackend({
    'GET /api/model/configurations': () => ++reads === 2 ? oldCatalog.promise : { body: { configurations: [reads === 1 ? first : second] } },
    'GET /api/roles/r1/model-configuration': () => ({ body: { configurationId: null, effectiveConfigurationId: reads < 3 ? first.id : second.id } }),
  });
  const view = render(<RoleModelSelector roleId="r1" revision="0" />);
  await waitFor(() => expect(trigger()).toHaveTextContent(first.model));
  view.rerender(<RoleModelSelector roleId="r1" revision="1" />);
  await waitFor(() => expect(reads).toBe(2));
  view.rerender(<RoleModelSelector roleId="r1" revision="2" />);
  await waitFor(() => expect(trigger()).toHaveTextContent(second.model));
  await act(async () => { oldCatalog.resolve({ body: { configurations: [first] } }); });
  expect(trigger()).toHaveTextContent(second.model);
  expect(trigger()).toBeEnabled();
});

it('does not publish a late refresh into another role', async () => {
  const oldBinding = deferred();
  let reads = 0;
  fakeBackend({
    'GET /api/model/configurations': () => ({ body: { configurations: [first, second] } }),
    'GET /api/roles/r1/model-configuration': () => ++reads === 2 ? oldBinding.promise : { body: { configurationId: first.id, effectiveConfigurationId: first.id } },
    'GET /api/roles/r2/model-configuration': () => ({ body: { configurationId: second.id, effectiveConfigurationId: second.id } }),
  });
  const view = render(<RoleModelSelector roleId="r1" revision="0" />);
  await waitFor(() => expect(trigger()).toHaveTextContent(first.model));
  view.rerender(<RoleModelSelector roleId="r1" revision="1" />);
  await waitFor(() => expect(reads).toBe(2));
  view.rerender(<RoleModelSelector roleId="r2" revision="1" />);
  await waitFor(() => expect(trigger()).toHaveTextContent(second.model));
  await act(async () => { oldBinding.resolve({ body: { configurationId: first.id, effectiveConfigurationId: first.id } }); });
  expect(trigger()).toHaveTextContent(second.model);
});

it.each([true, false])('waits for an in-flight selection save (success=%s) before coalescing settings refreshes', async succeeds => {
  const pendingSave = deferred();
  let selected = first.id;
  let configurations = [first, second];
  const { calls } = fakeBackend({
    'GET /api/model/configurations': () => ({ body: { configurations } }),
    'GET /api/roles/r1/model-configuration': () => ({ body: { configurationId: selected, effectiveConfigurationId: selected } }),
    'PUT /api/roles/r1/model-configuration': () => pendingSave.promise,
  });
  const user = userEvent.setup();
  const view = render(<RoleModelSelector roleId="r1" revision="0" />);
  await waitFor(() => expect(trigger()).toHaveTextContent(first.model));
  await user.click(trigger());
  await user.click(await within(screen.getByRole('menu')).findByRole('menuitemradio', { name: /second-model/ }));
  const readsBeforeSave = calls.filter(call => call.method === 'GET').length;
  view.rerender(<RoleModelSelector roleId="r1" revision="1" />);
  view.rerender(<RoleModelSelector roleId="r1" revision="2" />);
  expect(trigger()).toBeDisabled();
  expect(calls.filter(call => call.method === 'GET')).toHaveLength(readsBeforeSave);

  configurations = [{ ...first, model: 'updated-first' }, { ...second, model: 'updated-after-save' }];
  selected = succeeds ? second.id : first.id;
  await act(async () => { pendingSave.resolve(succeeds
    ? { body: { configurationId: selected, effectiveConfigurationId: selected } }
    : { status: 409, body: { detail: '模型选择未保存' } }); });
  await waitFor(() => expect(trigger()).toHaveTextContent(succeeds ? 'updated-after-save' : 'updated-first'));
  if (succeeds) expect(screen.queryByRole('status')).toBeNull();
  else expect(screen.getByRole('status')).toHaveTextContent('模型选择未保存');
  expect(trigger()).toBeEnabled();
  expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1);
  expect(calls.filter(call => call.method === 'GET')).toHaveLength(readsBeforeSave + 2);
});
