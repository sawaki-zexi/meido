import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../../App';
import { fakeBackend, role } from '../../test/fakeBackend';

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('角色资料只编辑身份，保存不会覆盖在设置里更新的工具配置', async () => {
  let saved = role({ id: 'r1', name: '默认角色', agentConfig: {
    enabledTools: ['custom.query'], enabledPlugins: ['custom'], grantedCapabilities: ['custom.read'],
    browserUse: { enabled: true }, shell: { enabled: false, allowedCommands: ['python'], timeoutSeconds: 42, maxOutputChars: 12345 },
  } });
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [saved] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'PUT /api/roles/r1': (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole('heading', { name: '默认角色', level: 1 });
  await user.click(screen.getByRole('button', { name: '角色资料' }));
  const form = within(await screen.findByRole('region', { name: /的角色工作区$/ }));
  expect(form.queryByText('默认内置工具')).not.toBeInTheDocument();
  expect(form.queryByLabelText('Shell 超时（秒）')).not.toBeInTheDocument();
  expect(form.queryByLabelText('允许浏览器操作网页')).not.toBeInTheDocument();
  expect(form.queryByLabelText('允许查询主人待办')).not.toBeInTheDocument();
  expect(form.queryByRole('button', { name: '查看实际工具' })).not.toBeInTheDocument();
  saved = { ...saved, agentConfig: { ...saved.agentConfig, browserUse: { enabled: false } } };
  await user.clear(form.getByRole('textbox', { name: /^名称/ }));
  await user.type(form.getByRole('textbox', { name: /^名称/ }), '新名称');
  await user.click(form.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(saved.name).toBe('新名称'));
  expect(calls.find((call) => call.method === 'PUT')?.body).not.toHaveProperty('agentConfig');
  expect(saved.agentConfig?.browserUse?.enabled).toBe(false);
  expect(saved.agentConfig?.enabledTools).toEqual(['custom.query']);
  expect(saved.agentConfig?.shell?.allowedCommands).toEqual(['python']);
});

it('创建角色只保存资料，旧草稿里的隐藏插件许可不会被提交', async () => {
  sessionStorage.setItem('meido:create-role-draft', JSON.stringify({ name: '草稿角色', profile: '设定',
    browserUseEnabled: true, shellEnabled: true, preservedAgentConfig: { browserUse: { enabled: true } } }));
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [] } }),
    'POST /api/roles': () => ({ status: 201, body: { role: role({ id: 'new', name: '草稿角色' }) } }),
  });
  const user = userEvent.setup(); render(<App />);
  const form = within(await screen.findByRole('region', { name: '创建角色' }));
  expect(form.getByRole('textbox', { name: /^名称/ })).toHaveValue('草稿角色');
  expect(form.queryByLabelText('子任务超时（秒）')).not.toBeInTheDocument();
  expect(form.queryByRole('group', { name: '插件设置' })).not.toBeInTheDocument();
  await user.click(form.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(calls.find((call) => call.method === 'POST')?.body).toMatchObject({ name: '草稿角色', profile: { profile: '设定' } }));
  expect(calls.find((call) => call.method === 'POST')?.body).not.toHaveProperty('agentConfig');
});
