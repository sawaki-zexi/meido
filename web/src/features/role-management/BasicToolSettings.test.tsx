import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../../App';
import { fakeBackend, role } from '../../test/fakeBackend';

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('基础工具默认提供，不要求角色逐项勾选，并保留未知扩展配置', async () => {
  let saved = role({ id: 'r1', name: '默认角色', agentConfig: {
    enabledTools: ['custom.query'], enabledPlugins: ['custom'], grantedCapabilities: ['custom.read'],
    shell: { enabled: false, allowedCommands: ['python'], timeoutSeconds: 42, maxOutputChars: 12345 },
  } });
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [saved] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'PUT /api/roles/r1': (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole('heading', { name: '默认角色', level: 1 });
  await user.click(screen.getByRole('button', { name: '角色资料' }));
  const form = within(await screen.findByRole('dialog', { name: '角色资料' }));
  expect(form.queryByRole('checkbox', { name: '读取文件（read_file）' })).not.toBeInTheDocument();
  expect(form.getByText(/基础工具默认提供/)).toBeInTheDocument();
  expect(form.queryByLabelText('允许执行 Shell 命令')).not.toBeInTheDocument();
  expect(form.queryByLabelText('允许主动检索记忆')).not.toBeInTheDocument();
  expect(form.getByLabelText('允许浏览器操作网页')).not.toBeChecked();
  await user.click(form.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(saved.agentConfig?.enabledTools).toEqual(['custom.query']));
  expect(saved.agentConfig?.enabledPlugins).toEqual(['custom']);
  expect(saved.agentConfig?.grantedCapabilities).toEqual(['custom.read']);
  expect(saved.agentConfig?.shell?.allowedCommands).toEqual(['python']);
});

it('新建角色无需打开工具开关，也能保存 Shell 与子任务预算', async () => {
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [] } }),
    'POST /api/roles': () => ({ status: 201, body: { role: role({ id: 'new', name: '预算角色' }) } }),
  });
  const user = userEvent.setup(); render(<App />);
  await user.click(await screen.findByRole('button', { name: '创建角色' }));
  const form = within(await screen.findByRole('dialog', { name: '创建角色' }));
  await user.type(form.getByRole('textbox', { name: /^名称/ }), '预算角色');
  await user.type(form.getByRole('textbox', { name: /^角色设定/ }), '设定');
  await user.clear(form.getByLabelText('Shell 超时（秒）'));
  await user.type(form.getByLabelText('Shell 超时（秒）'), '60');
  await user.clear(form.getByLabelText('子任务超时（秒）'));
  await user.type(form.getByLabelText('子任务超时（秒）'), '240');
  await user.click(form.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(calls.find((call) => call.method === 'POST')?.body).toMatchObject({
    agentConfig: { shell: { timeoutSeconds: 60 }, spawn: { timeoutSeconds: 240 } },
  }));
});
