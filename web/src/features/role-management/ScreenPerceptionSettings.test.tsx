import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { SettingsDialog } from "../settings/SettingsDialog";
import { fakeBackend, role } from "../../test/fakeBackend";
import { payloadFromDraft, readCreateDraft } from "./draft";
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const connections = [
  { id: "chat-model", providerId: "openai", provider: "openai", model: "chat-model", api: "openai-completions", baseUrl: "https://chat.example/v1", apiKeyConfigured: true, active: true },
  { id: "vision-model", providerId: "custom", provider: "vision-provider", model: "vision-model", api: "anthropic-messages", baseUrl: "https://vision.example", apiKeyConfigured: true },
];
const initial = { id: 'screen', name: '屏幕感知', description: '主屏摘要', enabled: false, revision: 0, cleanupPending: false,
  values: { visionModelConfigurationId: null as string | null } };

it('全局默认关闭，可覆盖默认聊天模型，参数保存不启用且无角色编辑', async () => {
  let saved = initial;
  const { calls } = fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [saved] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: connections, activeId: 'chat-model' } }),
    'PUT /api/plugin-management/screen/configuration': init => {
      saved = { ...saved, values: JSON.parse(String(init?.body)).values, revision: saved.revision + 1 };
      return { body: saved };
    },
    'PUT /api/plugin-management/screen/enabled': init => {
      saved = { ...saved, enabled: JSON.parse(String(init?.body)).enabled, revision: saved.revision + 1 };
      return { body: saved };
    },
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  await user.click(await screen.findByRole('button', { name: '屏幕感知 设置' }));
  const selector = await screen.findByRole('combobox', { name: '视觉模型连接' });
  await screen.findByRole('option', { name: /vision-provider/ });
  expect(selector).toHaveValue('');
  expect(screen.queryByRole('checkbox', { name: '允许按需观察主屏' })).not.toBeInTheDocument();
  await user.selectOptions(selector, 'vision-model');
  await user.click(screen.getByRole('button', { name: '打开模型设置' }));
  expect(screen.getAllByRole('dialog', { name: '设置' })).toHaveLength(1);
  expect(screen.getAllByRole('navigation', { name: '设置分类' })).toHaveLength(1);
  expect(screen.queryByRole('combobox', { name: '视觉模型连接' })).not.toBeInTheDocument();
  await user.keyboard('{Escape}');
  expect(screen.getByRole('combobox', { name: '视觉模型连接' })).toHaveValue('vision-model');
  await user.click(screen.getByRole('button', { name: '保存参数' }));
  await screen.findByText('公共参数已保存');
  expect(saved.values.visionModelConfigurationId).toBe('vision-model'); expect(saved.enabled).toBe(false);
  await user.click(screen.getByRole('button', { name: '返回插件列表' }));
  await user.click(await screen.findByRole('switch', { name: '启用 屏幕感知' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 屏幕感知' })).toBeChecked());
  expect(saved.enabled).toBe(true);
  expect(calls.some(call => call.path.startsWith('/api/roles'))).toBe(false);
});

it('已启用时连接删除保持原 ID，无效参数不能保存，仍可从列表全局关闭', async () => {
  let saved = { ...initial, enabled: true, values: { visionModelConfigurationId: 'vision-model' } };
  let configurations = connections;
  const { calls } = fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [saved] } }),
    'GET /api/model/configurations': () => ({ body: { configurations, activeId: 'chat-model' } }),
    'PUT /api/plugin-management/screen/enabled': init => {
      saved = { ...saved, enabled: JSON.parse(String(init?.body)).enabled, revision: saved.revision + 1 };
      return { body: saved };
    },
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  await user.click(await screen.findByRole('button', { name: '屏幕感知 设置' }));
  await screen.findByRole('option', { name: /vision-provider/ });
  configurations = [connections[0]];
  await user.click(screen.getByRole('button', { name: '刷新视觉连接' }));
  await screen.findByText('连接已不存在，请重新选择视觉模型连接。');
  expect(screen.getByRole('combobox', { name: '视觉模型连接' })).toHaveValue('vision-model');
  expect(screen.getByRole('combobox', { name: '视觉模型连接' })).toBeInvalid();
  await user.click(screen.getByRole('button', { name: '保存参数' }));
  expect(calls.some(call => call.method === 'PUT')).toBe(false);
  await user.click(screen.getByRole('button', { name: '返回插件列表' }));
  await user.click(await screen.findByRole('switch', { name: '启用 屏幕感知' }));
  expect(saved.enabled).toBe(false); expect(saved.values.visionModelConfigurationId).toBe('vision-model');
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用 屏幕感知' })).not.toBeChecked());
});

it('模型目录读取失败后刷新保留原公共视觉绑定，绝不补为当前连接', async () => {
  let unavailable = true;
  const saved = { ...initial, values: { visionModelConfigurationId: 'vision-model' } };
  const { calls } = fakeBackend({
    'GET /api/plugin-management': () => ({ body: { plugins: [saved] } }),
    'GET /api/model/configurations': () => unavailable ? { status: 503, body: { detail: '视觉连接目录暂时不可用' } }
      : { body: { configurations: connections, activeId: 'chat-model' } },
  });
  const user = userEvent.setup(); render(<SettingsDialog onClose={() => {}} initialSection="plugins" />);
  await user.click(await screen.findByRole('button', { name: '屏幕感知 设置' }));
  await screen.findByText('视觉连接目录暂时不可用');
  expect(screen.getByRole('combobox', { name: '视觉模型连接' })).toHaveValue('vision-model');
  unavailable = false; await user.click(screen.getByRole('button', { name: '刷新视觉连接' }));
  await screen.findByRole('option', { name: /vision-provider/ });
  expect(screen.getByRole('combobox', { name: '视觉模型连接' })).toHaveValue('vision-model');
  expect(calls.every(call => call.method === 'GET')).toBe(true);
});

it("角色卡只填充资料，保留屏幕连接和其他未知能力配置", async () => {
  const capabilities = {
    screenPerception: { enabled: true, visionModelConfigurationId: "vision-model", futurePolicy: "keep-vision" },
    browserUse: { enabled: true }, enabledTools: ["read_file", "calendar.search"], enabledPlugins: ["calendar"], grantedCapabilities: ["calendar.read"],
    futureFeature: { policy: "keep-role" },
  };
  let saved = role({ id: "r1", name: "导入前角色", agentConfig: capabilities });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1": () => ({ body: { role: saved } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/model/configurations": () => ({ body: { configurations: connections } }),
    "POST /api/role-card-import/preview": () => ({ body: {
      draft: { name: "导入后角色", description: "新简介", profile: "导入角色设定", personality: "温柔", behaviorRules: "", responseConstraints: "", nickname: "主人" },
      importProvenance: { format: "json", source: "card.json", importedAt: "2026-10-23T00:00:00Z" },
      source: "card.json", assets: [], compatibility: { format: "json", warnings: [], discardedFields: [], unsupportedMacros: [], unsupportedResources: [] },
    } }),
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "导入前角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const form = within(await screen.findByRole("dialog", { name: "角色资料" }));
  await user.upload(form.getByLabelText("导入角色卡"), new File(["{}"], "card.json", { type: "application/json" }));
  await user.click(await form.findByRole("button", { name: "填充到表单" }));
  expect(form.queryByRole("checkbox", { name: "允许按需观察主屏" })).not.toBeInTheDocument();
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(saved.name).toBe("导入后角色"));
  expect(saved.profile.character.profile).toBe("导入角色设定");
  expect(saved.profile.importProvenance).toEqual({ format: "json", source: "card.json", importedAt: "2026-10-23T00:00:00Z" });
  expect(saved.agentConfig).toMatchObject(capabilities);
});

it.each([
  { label: "旧草稿没有屏幕配置", stored: { name: "旧角色" }, expected: { enabled: false, visionModelConfigurationId: "" } },
  { label: "旧草稿只保存在完整能力配置", stored: { preservedAgentConfig: { screenPerception: { enabled: true, visionModelConfigurationId: "retained-vision" } } }, expected: { enabled: true, visionModelConfigurationId: "retained-vision" } },
  { label: "新草稿明确关闭并清空绑定", stored: { screenPerceptionEnabled: false, screenVisionModelConfigurationId: "", preservedAgentConfig: { screenPerception: { enabled: true, visionModelConfigurationId: "retained-vision" } } }, expected: { enabled: false, visionModelConfigurationId: "" } },
])("恢复$label时不从其他连接补值", ({ stored, expected }) => {
  sessionStorage.setItem("meido:create-role-draft", JSON.stringify(stored));
  const restored = readCreateDraft();
  expect(restored?.screenPerceptionEnabled).toBe(expected.enabled);
  expect(restored?.screenVisionModelConfigurationId).toBe(expected.visionModelConfigurationId);
  if (restored?.preservedAgentConfig.screenPerception) {
    expect(payloadFromDraft(restored).agentConfig.screenPerception).toEqual({ enabled: expected.enabled, visionModelConfigurationId: expected.visionModelConfigurationId || null });
  }
});
