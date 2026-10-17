import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, role } from "../../test/fakeBackend";
import { payloadFromDraft, readCreateDraft } from "./draft";

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const connections = [
  { id: "chat-model", providerId: "openai", provider: "openai", model: "chat-model", api: "openai-completions", baseUrl: "https://chat.example/v1", apiKeyConfigured: true, active: true },
  { id: "vision-model", providerId: "custom", provider: "vision-provider", model: "vision-model", api: "anthropic-messages", baseUrl: "https://vision.example", apiKeyConfigured: true },
];

it("默认关闭且不跟随聊天或全局模型，显式选择视觉连接后随角色保存并重开", async () => {
  let saved = role({ id: "r1", name: "观察角色", agentConfig: { enabledTools: ["read_file"] } });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/model/configurations": () => ({ body: { configurations: connections, activeId: "chat-model" } }),
    "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: "chat-model", effectiveConfigurationId: "chat-model" } }),
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "观察角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const form = within(await screen.findByRole("dialog", { name: "角色资料" }));
  expect(form.getByRole("checkbox", { name: "允许按需观察主屏" })).not.toBeChecked();
  const selector = await form.findByRole("combobox", { name: "视觉模型连接" });
  await waitFor(() => expect(form.getByRole("option", { name: /vision-provider.*vision-model.*anthropic-messages/ })).toBeInTheDocument());
  expect(selector).toHaveValue("");
  expect(form.getByText("画面发送到所选视觉连接，本地不保存原图；分析摘要会用于当前对话")).toBeInTheDocument();
  await user.click(form.getByRole("checkbox", { name: "允许按需观察主屏" }));
  await user.selectOptions(selector, "vision-model");
  expect(form.getByText("所选连接：vision-provider · vision-model · anthropic-messages（ID：vision-model）")).toBeInTheDocument();
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(saved.agentConfig?.screenPerception).toEqual({ enabled: true, visionModelConfigurationId: "vision-model" }));
  expect(saved.agentConfig?.enabledTools).toEqual(["read_file"]);
  await user.click(form.getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const reopened = within(await screen.findByRole("dialog", { name: "角色资料" }));
  expect(reopened.getByRole("checkbox", { name: "允许按需观察主屏" })).toBeChecked();
  expect(reopened.getByRole("combobox", { name: "视觉模型连接" })).toHaveValue("vision-model");
  expect(calls.filter((call) => call.method !== "GET").map((call) => call.path)).toEqual(["/api/roles/r1"]);
});

it("创建角色刷新恢复屏幕开关和视觉连接，关闭保存仍保留所选连接", async () => {
  let saved = role({ id: "new-role", name: "新观察角色" });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [] } }),
    "GET /api/model/configurations": () => ({ body: { configurations: connections, activeId: "chat-model" } }),
    "POST /api/roles": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { status: 201, body: { role: saved } }; },
  });
  const user = userEvent.setup();
  const first = render(<App />);
  await user.click(await screen.findByRole("button", { name: "创建角色" }));
  const form = within(await screen.findByRole("dialog", { name: "创建角色" }));
  expect(form.getByRole("checkbox", { name: "允许按需观察主屏" })).not.toBeChecked();
  await user.type(form.getByRole("textbox", { name: /^名称/ }), "新观察角色");
  await user.type(form.getByRole("textbox", { name: /^角色设定/ }), "角色的设定");
  await form.findByRole("option", { name: /vision-provider/ });
  await user.selectOptions(form.getByRole("combobox", { name: "视觉模型连接" }), "vision-model");
  await user.click(form.getByRole("checkbox", { name: "允许按需观察主屏" }));
  expect(form.queryByRole("checkbox", { name: "读取文件（read_file）" })).not.toBeInTheDocument();
  first.unmount();
  render(<App />);
  const restored = within(await screen.findByRole("dialog", { name: "创建角色" }));
  expect(restored.getByRole("checkbox", { name: "允许按需观察主屏" })).toBeChecked();
  expect(restored.getByRole("combobox", { name: "视觉模型连接" })).toHaveValue("vision-model");
  expect(restored.getByRole("textbox", { name: /^角色设定/ })).toHaveValue("角色的设定");
  await user.click(restored.getByRole("checkbox", { name: "允许按需观察主屏" }));
  await user.click(restored.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(calls.some((call) => call.method === "POST" && call.path === "/api/roles")).toBe(true));
  expect(saved.agentConfig?.screenPerception).toEqual({ enabled: false, visionModelConfigurationId: "vision-model" });
  expect(saved.agentConfig?.enabledTools).toEqual([]);
});

it("缺连接时进入现有模型设置，保存连接后返回保留角色草稿且不会误保存角色", async () => {
  let configurations = [] as typeof connections;
  let saved = role({ id: "r1", name: "待配置角色", agentConfig: { screenPerception: { enabled: false, visionModelConfigurationId: "previous-vision" } } });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/model/configurations": () => ({ body: { configurations, activeId: null } }),
    "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: null, effectiveConfigurationId: null } }),
    "GET /api/model/providers": () => ({ body: { providers: [{ id: "openai", label: "OpenAI", provider: "openai", baseUrl: "https://api.example/v1", modelHint: "模型名" }] } }),
    "POST /api/model/configurations": () => { configurations = [connections[1]]; return { status: 201, body: { configuration: connections[1] } }; },
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "待配置角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const form = within(await screen.findByRole("dialog", { name: "角色资料" }));
  await form.findByText("暂无已保存的模型连接，请先在模型设置中添加。");
  await user.type(form.getByRole("textbox", { name: /^名称/ }), "改名草稿");
  expect(form.queryByRole("checkbox", { name: "读取文件（read_file）" })).not.toBeInTheDocument();
  await user.click(form.getByRole("button", { name: "打开模型设置" }));
  const settings = within(await screen.findByRole("dialog", { name: "设置" }));
  await user.type(await settings.findByRole("textbox", { name: "模型 ID" }), "vision-model");
  await user.click(settings.getByRole("button", { name: "保存" }));
  await settings.findByRole("list", { name: "已保存的模型配置" });
  expect(calls.some((call) => call.method === "PUT" && call.path === "/api/roles/r1")).toBe(false);
  await user.click(settings.getByRole("button", { name: "关闭" }));
  await form.findByRole("option", { name: /vision-provider/ });
  expect(form.getByRole("textbox", { name: /^名称/ })).toHaveValue("待配置角色改名草稿");
  expect(form.getByText(/基础工具默认提供/)).toBeInTheDocument();
  expect(form.getByRole("combobox", { name: "视觉模型连接" })).toHaveValue("previous-vision");
  await user.selectOptions(form.getByRole("combobox", { name: "视觉模型连接" }), "vision-model");
  await user.click(form.getByRole("checkbox", { name: "允许按需观察主屏" }));
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(saved.agentConfig?.screenPerception).toEqual({ enabled: true, visionModelConfigurationId: "vision-model" }));
  expect(calls.filter((call) => call.method !== "GET").map((call) => call.path)).toEqual(["/api/model/configurations", "/api/roles/r1"]);
});

it("刷新后连接已删除时保留原 ID，要求重新选择或关闭，关闭保存也不会回退到全局连接", async () => {
  let configurations = connections;
  let saved = role({ id: "r1", name: "连接变更角色", agentConfig: { screenPerception: { enabled: true, visionModelConfigurationId: "vision-model" } } });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/model/configurations": () => ({ body: { configurations, activeId: "chat-model" } }),
    "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: "chat-model", effectiveConfigurationId: "chat-model" } }),
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "连接变更角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const form = within(await screen.findByRole("dialog", { name: "角色资料" }));
  await form.findByRole("option", { name: /vision-provider/ });
  configurations = [connections[0]];
  await user.click(form.getByRole("button", { name: "刷新视觉连接" }));
  await form.findByText("连接已不存在，请重新选择视觉模型连接。");
  const selector = form.getByRole("combobox", { name: "视觉模型连接" });
  expect(selector).toHaveValue("vision-model");
  expect(selector).toBeInvalid();
  await user.click(form.getByRole("button", { name: "保存" }));
  expect(calls.some((call) => call.method === "PUT")).toBe(false);
  await user.click(form.getByRole("checkbox", { name: "允许按需观察主屏" }));
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(saved.agentConfig?.screenPerception).toEqual({ enabled: false, visionModelConfigurationId: "vision-model" }));
  expect(calls.filter((call) => call.method !== "GET").map((call) => call.path)).toEqual(["/api/roles/r1"]);
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
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/model/configurations": () => ({ body: { configurations: connections } }),
    "POST /api/role-card-import/preview": () => ({ body: {
      draft: { name: "导入后角色", description: "新简介", profile: "导入角色设定", personality: "温柔", behaviorRules: "", responseConstraints: "", nickname: "主人" },
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
  expect(form.getByRole("checkbox", { name: "允许按需观察主屏" })).toBeChecked();
  expect(form.getByRole("combobox", { name: "视觉模型连接" })).toHaveValue("vision-model");
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(saved.name).toBe("导入后角色"));
  expect(saved.profile.profile).toBe("导入角色设定");
  expect(saved.agentConfig).toMatchObject(capabilities);
});

it("加载连接失败可重试，已有视觉绑定不会被清空或替换", async () => {
  let unavailable = true;
  const saved = role({ id: "r1", name: "重试角色", agentConfig: { screenPerception: { enabled: false, visionModelConfigurationId: "vision-model" } } });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/model/configurations": () => unavailable ? { status: 503, body: { detail: "视觉连接目录暂时不可用" } } : { body: { configurations: connections, activeId: "chat-model" } },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "重试角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const form = within(await screen.findByRole("dialog", { name: "角色资料" }));
  await form.findByText("视觉连接目录暂时不可用");
  expect(form.getByRole("combobox", { name: "视觉模型连接" })).toBeDisabled();
  expect(form.getByRole("combobox", { name: "视觉模型连接" })).toHaveValue("vision-model");
  unavailable = false;
  await user.click(form.getByRole("button", { name: "刷新视觉连接" }));
  await form.findByRole("option", { name: /vision-provider/ });
  expect(form.getByRole("combobox", { name: "视觉模型连接" })).toBeEnabled();
  expect(form.getByRole("combobox", { name: "视觉模型连接" })).toHaveValue("vision-model");
  expect(form.queryByText("视觉连接目录暂时不可用")).not.toBeInTheDocument();
  expect(calls.every((call) => call.method === "GET")).toBe(true);
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
