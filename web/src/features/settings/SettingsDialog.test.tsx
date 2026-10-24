import { cleanup, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { SettingsDialog } from './SettingsDialog';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('模型详情与记忆子页的未提交内容在分类往返后保留', async () => {
  const { calls } = fakeBackend({
    'GET /api/model/providers': () => ({ body: { providers: [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [], activeId: null } }),
    'GET /api/memory/settings': () => ({ body: { enabled: true, embeddingEnabled: false, embedding: {
      configuration: { provider: 'local', baseUrl: 'http://localhost:9/v1', model: 'embedding', dimensions: 32, apiKeyConfigured: false },
      status: { configured: false, ready: false, enabled: false },
    } } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} />);
  await user.type(await screen.findByLabelText('模型 ID'), 'unsaved-chat');
  const nav = within(screen.getByRole('navigation', { name: '设置分类' }));
  await user.click(nav.getByRole('button', { name: '记忆' }));
  await user.click(await screen.findByRole('button', { name: '向量模型' }));
  await user.clear(screen.getByLabelText('向量模型 ID'));
  await user.type(screen.getByLabelText('向量模型 ID'), 'unsaved-embedding');
  await user.click(nav.getByRole('button', { name: '模型' }));
  expect(screen.getByLabelText('模型 ID')).toHaveValue('unsaved-chat');
  await user.click(nav.getByRole('button', { name: '记忆' }));
  expect(screen.getByLabelText('向量模型 ID')).toHaveValue('unsaved-embedding');
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('嵌套模型页新增连接后，回到已访问的模型列表会刷新目录', async () => {
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 0));
  const initial = { id: 'chat', providerId: 'custom', provider: 'local', api: 'openai-completions', baseUrl: 'http://localhost:9/v1', model: 'chat', apiKeyConfigured: false };
  let configurations = [initial];
  fakeBackend({
    'GET /api/model/providers': () => ({ body: { providers: [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations, activeId: configurations.at(-1)!.id } }),
    'POST /api/model/configurations': () => {
      const configuration = { ...initial, id: 'new', model: 'new-vision' };
      configurations = [...configurations, configuration]; return { body: { configuration } };
    },
    'GET /api/plugin-management': () => ({ body: { plugins: [{ id: 'screen', name: '屏幕感知', description: '', enabled: false, revision: 0, cleanupPending: false, values: { visionModelConfigurationId: null } }] } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} />);
  await screen.findByRole('list', { name: '已保存的模型配置' });
  await user.click(screen.getByRole('button', { name: '插件' }));
  await user.click(await screen.findByRole('button', { name: '屏幕感知 设置' }));
  await user.click(screen.getByRole('button', { name: '打开模型设置' }));
  await user.click(await screen.findByRole('button', { name: '新建配置' }));
  await user.type(screen.getByLabelText('API 地址'), 'http://localhost:9/v1');
  await user.type(screen.getByLabelText('模型 ID'), 'new-vision');
  await user.click(screen.getByRole('button', { name: '保存' }));
  await screen.findByText('配置已保存并生效');
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('button', { name: '模型' }));
  expect(await screen.findByText('local · new-vision')).toBeInTheDocument();
});

it('外观偏好立即应用，重开仍保留且不调用写入 API', async () => {
  localStorage.clear();
  const { calls } = fakeBackend({});
  const user = userEvent.setup();
  const view = render(<SettingsDialog initialSection="appearance" onClose={() => {}} />);
  await user.selectOptions(screen.getByRole('combobox', { name: '界面字号' }), 'large');
  await user.click(screen.getByRole('switch', { name: '减弱动态效果' }));
  expect(document.documentElement.dataset.fontSize).toBe('large');
  expect(document.documentElement.dataset.reducedMotion).toBe('true');
  view.unmount();
  render(<SettingsDialog initialSection="appearance" onClose={() => {}} />);
  expect(screen.getByRole('combobox', { name: '界面字号' })).toHaveValue('large');
  expect(screen.getByRole('switch', { name: '减弱动态效果' })).toBeChecked();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
  localStorage.clear();
  delete document.documentElement.dataset.fontSize;
  delete document.documentElement.dataset.reducedMotion;
});

it('系统减弱动态效果生效时，背景呼吸显示关闭并暂停操作', () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  render(<SettingsDialog initialSection="appearance" onClose={() => {}} />);
  expect(screen.getByRole('switch', { name: '背景呼吸' })).not.toBeChecked();
  expect(screen.getByRole('switch', { name: '背景呼吸' })).toBeDisabled();
  expect(screen.getByText('系统已减弱动态效果。')).toBeInTheDocument();
});

it('设置使用 Shiori 的七类侧栏，服务作为插件下级页面可返回', async () => {
  const { calls } = fakeBackend({ 'GET /api/plugin-management': () => ({ body: { plugins: [] } }) });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  const nav = within(screen.getByRole('navigation', { name: '设置分类' }));
  expect(nav.getAllByRole('button').map(button => button.textContent)).toEqual(['模型', '记忆', '外观', '高级', '我的身份', '插件', '关于']);
  await user.click(screen.getByRole('button', { name: 'MCP 服务' }));
  expect(await screen.findByRole('heading', { name: 'MCP 服务', level: 2 })).toBeInTheDocument();
  expect(nav.getByRole('button', { name: '插件' })).toHaveAttribute('aria-current', 'page');
  await user.click(screen.getByRole('button', { name: '返回插件' }));
  expect(screen.getByRole('button', { name: 'MCP 服务' })).toBeInTheDocument();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('编程插件仅在展开用途参数后选择角色，保存只更新该用途并保留最新资料', async () => {
  const saved = role({ id: 'r2', name: '角色二', agentConfig: { coding: { enabled: false, maxAttemptsPerDay: 3,
    repoGrantIds: [], profileIds: [], defaultHerdrProfileId: null, defaultCodexProfileId: null } } });
  const { calls } = fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [] } }),
    'GET /api/roles': () => ({ body: { roles: [saved] } }),
    'GET /api/roles/r2': () => ({ body: { role: { ...saved, name: '外部更新' } } }),
    'PUT /api/roles/r2': init => ({ body: { role: { ...saved, name: '外部更新', agentConfig: {
      ...saved.agentConfig, coding: { ...saved.agentConfig!.coding, ...JSON.parse(String(init?.body)).agentConfig.coding } } } } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  expect(calls.some(call => call.path === '/api/roles')).toBe(false);
  await user.click(await screen.findByRole('button', { name: '编程委托 设置' }));
  expect(screen.queryByRole('combobox', { name: '角色参数' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '角色参数' }));
  expect(await screen.findByRole('combobox', { name: '角色参数' })).toHaveValue('r2');
  expect(screen.queryByRole('checkbox', { name: '允许编程委托' })).not.toBeInTheDocument();
  const budget = await screen.findByLabelText('每天最多编程尝试次数');
  await user.clear(budget); await user.type(budget, '7');
  await user.click(screen.getByRole('button', { name: '保存角色参数' }));
  await screen.findByText('角色参数已保存');
  const changes = calls.find(call => call.method === 'PUT')?.body as { name: string; agentConfig: unknown };
  expect(changes.name).toBe('外部更新');
  expect(changes.agentConfig).toEqual({ coding: { maxAttemptsPerDay: 7 } });
  await waitFor(() => expect(calls.filter(call => call.method === 'PUT')).toHaveLength(1));
});

it('公共参数草稿在模型页往返后保留', async () => {
  fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [{ id: 'browser', name: '浏览器', description: '', enabled: false,
      revision: 0, cleanupPending: false, values: { headed: false, timeout_seconds: 45 } }] } }),
    'GET /api/model/providers': () => ({ body: { providers: [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [], activeId: null } }),
  });
  const user = userEvent.setup(); render(<SettingsDialog initialSection="plugins" onClose={() => {}} />);
  await user.click(await screen.findByRole('button', { name: 'Browser Use 设置' }));
  await user.click(screen.getByRole('checkbox', { name: '显示浏览器窗口' }));
  const nav = within(screen.getByRole('navigation', { name: '设置分类' }));
  await user.click(nav.getByRole('button', { name: '模型' }));
  await user.click(nav.getByRole('button', { name: '插件' }));
  expect(screen.getByRole('checkbox', { name: '显示浏览器窗口' })).toBeChecked();
});
