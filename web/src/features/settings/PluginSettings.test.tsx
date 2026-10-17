import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsDialog } from './SettingsDialog';
import { App } from '../../App';
import { fakeBackend, role } from '../../test/fakeBackend';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('在插件设置暂停角色浏览器，保留最新角色资料和未编辑的配置', async () => {
  let saved = role({ id: 'r1', name: '插件角色', agentConfig: {
    enabledTools: ['custom.query'], enabledPlugins: ['custom'], grantedCapabilities: ['custom.read'],
    browserUse: { enabled: true }, shell: { enabled: false, allowedCommands: ['python'], timeoutSeconds: 30, maxOutputChars: 20000 },
  } });
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [saved] } }),
    'GET /api/roles/r1': () => ({ body: { role: { ...saved, name: '外部更新的名称' } } }),
    'PUT /api/roles/r1': (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  expect(await screen.findByRole('combobox', { name: '配置角色' })).toHaveValue('r1');
  expect(screen.getByLabelText('允许浏览器操作网页')).toBeChecked();
  await user.click(screen.getByLabelText('允许浏览器操作网页'));
  await user.click(screen.getByRole('button', { name: '保存' }));
  await screen.findByText('插件设置已保存');
  expect(saved.name).toBe('外部更新的名称');
  expect(saved.agentConfig?.browserUse?.enabled).toBe(false);
  expect(saved.agentConfig?.enabledTools).toEqual(['custom.query']);
  expect(saved.agentConfig?.enabledPlugins).toEqual(['custom']);
  expect(saved.agentConfig?.shell?.allowedCommands).toEqual(['python']);
  await waitFor(() => expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(1));
});

it('插件保存同步到当前角色，随后编辑角色资料不会恢复旧插件开关', async () => {
  localStorage.clear(); sessionStorage.clear();
  let saved = role({ id: 'r1', name: '同步角色', agentConfig: { browserUse: { enabled: true } } });
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [saved] } }),
    'GET /api/roles/r1': () => ({ body: { role: saved } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'PUT /api/roles/r1': (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup(); render(<App />);
  await screen.findByRole('heading', { name: '同步角色', level: 1 });
  await user.click(screen.getByRole('button', { name: '设置' }));
  await user.click(screen.getByRole('button', { name: '工具与插件' }));
  await screen.findByRole('combobox', { name: '配置角色' });
  await user.click(screen.getByLabelText('允许浏览器操作网页'));
  await user.click(screen.getByRole('button', { name: '保存' }));
  await screen.findByText('插件设置已保存');
  await user.click(screen.getByRole('button', { name: '关闭' }));
  await user.click(screen.getByRole('button', { name: '角色资料' }));
  expect(screen.queryByLabelText('允许浏览器操作网页')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(saved.agentConfig?.browserUse?.enabled).toBe(false));
});


it('设置中调整 Shell 和子任务预算并启用待办，保留刷新读取的扩展授权', async () => {
  let saved = role({ id: 'r1', name: '预算角色', agentConfig: { enabledTools: ['custom.query'], enabledPlugins: ['custom'], grantedCapabilities: ['custom.read'] } });
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [saved] } }),
    'GET /api/roles/r1': () => ({ body: { role: saved } }),
    'PUT /api/roles/r1': (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  await screen.findByRole('combobox', { name: '配置角色' });
  expect(screen.getByText(/基础工具默认提供/)).toBeInTheDocument();
  await user.clear(screen.getByLabelText('Shell 超时（秒）'));
  await user.type(screen.getByLabelText('Shell 超时（秒）'), '60');
  await user.clear(screen.getByLabelText('子任务超时（秒）'));
  await user.type(screen.getByLabelText('子任务超时（秒）'), '240');
  await user.click(screen.getByLabelText('允许查询主人待办'));
  saved = { ...saved, agentConfig: { ...saved.agentConfig, enabledTools: ['custom.query', 'new.query'], enabledPlugins: ['custom', 'new'], grantedCapabilities: ['custom.read', 'new.read'] } };
  await user.click(screen.getByRole('button', { name: '保存' }));
  await screen.findByText('插件设置已保存');
  expect(saved.agentConfig).toMatchObject({ shell: { timeoutSeconds: 60 }, spawn: { timeoutSeconds: 240 },
    enabledTools: ['custom.query', 'new.query', 'todo.search'], enabledPlugins: ['custom', 'new', 'todo'], grantedCapabilities: ['custom.read', 'new.read', 'todo.read'] });
});
