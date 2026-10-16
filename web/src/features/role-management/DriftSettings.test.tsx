import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { fakeBackend, role } from '../../test/fakeBackend';
import { App } from '../../App';
import { AutonomySettings } from './AutonomySettings';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); });
const config = { revision: 4, activeDecisionEnabled: false, driftEnabled: false,
  decisionModelConfigurationId: null, driftModelConfigurationId: null,
  defaultTargetRef: 'desktop', allowedTargetRefs: ['desktop'], sourceGrants: [{ sourceId: 'keep', enabled: false }],
  driftToolGrants: [], shellRuleIds: [], consentRevision: 0,
  driftAuthoring: { enabled: false, scopeId: 'owner-scope', revision: 2, allowedTools: [], allowMcp: false,
    allowShell: false, allowAutonomousAuthoring: false, allowedResourceIds: [] },
  driftMcpGrants: [], driftReadResourceIds: [], driftAttachmentIds: [] };
const state = { config, runs: [], acknowledgements: [], cleanupPending: false, humanBusy: false };
const models = [{ id: 'drift-model', provider: 'local', model: 'drift-helper', api: 'openai-completions' }];

it('enables Drift independently with an explicit model and finite consent while preserving the complete source configuration', async () => {
  let saved: unknown;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: state }),
    'GET /api/model/configurations': () => ({ body: { configurations: models, activeId: 'drift-model' } }),
    'PUT /api/roles/r1/autonomy': (request) => { saved = JSON.parse(String(request?.body)); return { body: { ...state, config: { ...config, ...(saved as { config: object }).config, revision: 5 } } }; },
  });
  const user = userEvent.setup(); render(<AutonomySettings roleId="r1" disabled={false} />);
  const drift = await screen.findByRole('checkbox', { name: '开启 Drift' });
  expect(drift).not.toBeChecked(); expect(drift).toBeEnabled();
  await user.click(drift);
  const selector = screen.getByRole('combobox', { name: 'Drift 模型连接' });
  expect(selector).toHaveValue('');
  expect(screen.getByRole('button', { name: '保存自主设置' })).toBeDisabled();
  await user.selectOptions(selector, 'drift-model');
  await user.click(screen.getByRole('checkbox', { name: /同意有限自主读取/ }));
  await user.click(screen.getByRole('button', { name: '保存自主设置' }));
  await waitFor(() => expect(saved).toMatchObject({ expectedRevision: 4, config: { ...config,
    driftEnabled: true, driftModelConfigurationId: 'drift-model', consentRevision: 1 } }));
  expect(screen.getByRole('checkbox', { name: '开启主动决策' })).not.toBeChecked();
  expect(calls.filter((call) => call.method !== 'GET').map((call) => call.path)).toEqual(['/api/roles/r1/autonomy']);
  expect(screen.getByText(/最多4次/)).toBeInTheDocument();
});

it('separates author declaration bounds from execution grants and saves only explicit observed MCP hashes without connecting', async () => {
  const hash = 'a'.repeat(64);
  let saved: unknown;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: state }),
    'GET /api/model/configurations': () => ({ body: { configurations: models } }),
    'GET /api/roles/r1/autonomy/drift-grant-options': () => ({ body: {
      tools: [{ name: 'read_file', available: true }, { name: 'web_fetch', available: false, reason: '角色尚未授权此工具' }],
      resources: [{ id: 'role-profile', name: '角色资料', available: true }],
      shellRules: [{ id: 'python-version', description: '读取 Python 版本', available: false, reason: '请先配置角色 Shell 白名单' }],
      mcpServers: [{ serverId: 'notes', name: '笔记服务', configRevision: 'config-v2', grantRevision: 'grant-v3', risk: 'read_only',
        status: 'observed', operations: [{ name: 'search', definitionHash: hash, description: '检索笔记' }] },
        { serverId: 'unknown', name: '未观察服务', configRevision: 'c1', grantRevision: 'g1', risk: 'read_only', status: 'unobserved', operations: [] }] } }),
    'PUT /api/roles/r1/autonomy': (request) => { saved = JSON.parse(String(request?.body)); return { body: { ...state, config: (saved as { config: object }).config } }; },
  });
  const user = userEvent.setup(); render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole('checkbox', { name: '开启 Drift' });
  await user.click(screen.getByRole('button', { name: '配置 Drift 工具许可' }));
  await screen.findByRole('checkbox', { name: '作者可声明 web_fetch' });
  await user.click(screen.getByRole('checkbox', { name: '允许创建或更新 Drift 技能' }));
  await user.click(screen.getByRole('checkbox', { name: '作者可声明 web_fetch' }));
  await user.click(screen.getByRole('checkbox', { name: '作者可声明 MCP 依赖' }));
  expect(screen.getByRole('checkbox', { name: '执行许可 web_fetch' })).toBeDisabled();
  await user.click(screen.getByRole('checkbox', { name: '执行许可 read_file' }));
  await user.click(screen.getByRole('checkbox', { name: '只读资源 角色资料' }));
  await user.click(screen.getByRole('checkbox', { name: 'MCP 许可 notes search' }));
  expect(screen.getByRole('checkbox', { name: 'Shell 许可 python-version' })).toBeDisabled();
  expect(screen.getByText(/普通主人聊天通过 mcp_add/)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '保存自主设置' }));
  await waitFor(() => expect(saved).toMatchObject({ config: { driftEnabled: false, activeDecisionEnabled: false,
    driftAuthoring: { ...config.driftAuthoring, enabled: true, allowedTools: ['web_fetch'], allowMcp: true },
    driftToolGrants: ['read_file'], driftReadResourceIds: ['role-profile'], driftMcpGrants: [{
      serverId: 'notes', configRevision: 'config-v2', grantRevision: 'grant-v3', risk: 'read_only',
      operations: [{ name: 'search', definitionHash: hash }] }] } }));
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
  expect(screen.getByText(/声明上界不等于执行许可/)).toBeInTheDocument();
});

it('shows real Drift finish, message receipt, Runtime ACK and cleanup as separate facts', async () => {
  fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: { ...state, cleanupPending: true, runs: [
      { id: 'silent-run', kind: 'autonomy_tick', mode: 'drift', status: 'finished_silent', runtimeAcknowledged: false, delivery: null },
      { id: 'sent-run', kind: 'autonomy_tick', mode: 'drift', status: 'finished_sent', runtimeAcknowledged: true,
        delivery: { status: 'sent', messageId: 'actual-message' } },
      { id: 'unknown-run', kind: 'autonomy_tick', mode: 'drift', status: 'finish_uncertain', runtimeAcknowledged: true,
        delivery: { status: 'uncertain', messageId: null } },
      { id: 'unfinished-run', kind: 'autonomy_tick', mode: 'drift', status: 'aborted', runtimeAcknowledged: true, delivery: null },
    ] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
  });
  render(<AutonomySettings roleId="r1" disabled={false} />);
  expect(await screen.findByText(/合法静默结束/)).toBeInTheDocument();
  expect(screen.getByText(/合法发送结束/)).toBeInTheDocument();
  expect(screen.getByText(/合法结束，投递结果不确定/)).toBeInTheDocument();
  expect(screen.getByText(/Runtime 结果尚未确认交接/)).toBeInTheDocument();
  expect(screen.getByText(/消息投递：已发送，回执已确认/)).toBeInTheDocument();
  expect(screen.getByText(/消息投递：结果不确定，不会再次发送/)).toBeInTheDocument();
  expect(screen.getByText(/已中止，未合法 finish/)).toBeInTheDocument();
  expect(screen.getByText(/正在回收自有资源/)).toBeInTheDocument();
});

it('requires a fresh complete configuration after a version conflict instead of resubmitting the stale revision', async () => {
  let revision = 4; let writes = 0;
  const { calls } = fakeBackend({
    'GET /api/roles/r1/autonomy': () => ({ body: { ...state, config: { ...config, revision } } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [] } }),
    'PUT /api/roles/r1/autonomy': (request) => ++writes === 1 ? (revision = 5, { status: 409, body: { detail: '配置版本已变化' } })
      : { body: { ...state, config: JSON.parse(String(request?.body)).config } },
  });
  const user = userEvent.setup(); render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole('checkbox', { name: '开启 Drift' });
  await user.click(screen.getByRole('button', { name: '保存自主设置' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('配置版本已变化');
  await user.click(screen.getByRole('button', { name: '刷新自主状态' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存自主设置' })).toBeDisabled());
  await user.click(screen.getByRole('button', { name: '重新读取自主设置' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存自主设置' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: '保存自主设置' }));
  await waitFor(() => expect(writes).toBe(2));
  expect(calls.filter((call) => call.method === 'PUT').map((call) => (call.body as { expectedRevision: number }).expectedRevision)).toEqual([4, 5]);
});

it('exposes the working Drift configuration in the actual role editor and reopens its saved independent model', async () => {
  let saved = state;
  const { calls } = fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [role({ id: 'r1', name: 'Drift角色' })] } }),
    'GET /api/roles/r1/session': () => ({ body: { session: { sessionKey: 'role:r1', roleId: 'r1' }, messages: [] } }),
    'GET /api/roles/r1/model-configuration': () => ({ body: { configurationId: null, effectiveConfigurationId: null } }),
    'GET /api/roles/r1/tools': () => ({ body: { tools: [], plugins: [] } }),
    'GET /api/roles/r1/autonomy': () => ({ body: saved }),
    'GET /api/model/configurations': () => ({ body: { configurations: models, activeId: 'drift-model' } }),
    'PUT /api/roles/r1/autonomy': (request) => { saved = { ...state, config: { ...JSON.parse(String(request?.body)).config, revision: 5 } }; return { body: saved }; },
  });
  const user = userEvent.setup(); render(<App />);
  await user.click(await screen.findByRole('button', { name: '角色资料' }));
  const editor = within(await screen.findByRole('dialog', { name: '角色资料' }));
  await editor.findByRole('checkbox', { name: '开启 Drift' });
  await user.click(editor.getByRole('checkbox', { name: '开启 Drift' }));
  await user.selectOptions(editor.getByRole('combobox', { name: 'Drift 模型连接' }), 'drift-model');
  await user.click(editor.getByRole('checkbox', { name: /同意有限自主读取/ }));
  await user.click(editor.getByRole('button', { name: '保存自主设置' }));
  await waitFor(() => expect(saved.config.driftEnabled).toBe(true));
  await user.keyboard('{Escape}');
  await user.click(screen.getByRole('button', { name: '角色资料' }));
  await waitFor(() => expect(screen.getByRole('checkbox', { name: '开启 Drift' })).toBeChecked());
  expect(screen.getByRole('combobox', { name: 'Drift 模型连接' })).toHaveValue('drift-model');
  expect(screen.getByRole('checkbox', { name: '开启主动决策' })).not.toBeChecked();
  expect(screen.getByRole('group', { name: '来源产物预览' })).toBeInTheDocument();
  expect(calls.filter((call) => call.method !== 'GET').map((call) => call.path)).toEqual(['/api/roles/r1/autonomy']);
});
