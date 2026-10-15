import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, role } from "../../test/fakeBackend";

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const toolLabels = [
  "读取文件（read_file）", "列出目录（list_dir）", "写入文件（write_file）", "编辑文件（edit_file）",
  "搜索网页（web_search）", "抓取网页（web_fetch）", "搜索消息原文（search_messages）", "读取消息原文（fetch_messages）",
  "记住信息（memorize）", "撤销记忆（forget_memory）", "按需发现工具（tool_search）",
];

it("分别授权已实现工具，保存重开时保留部分选择、未知能力和嵌套配置", async () => {
  const existing = {
    enabledTools: ["read_file", "calendar.search"], enabledPlugins: ["calendar"], grantedCapabilities: ["calendar.read"],
    shell: { enabled: false, allowedCommands: ["python"], timeoutSeconds: 42, maxOutputChars: 12345, extension: "keep-shell" },
    extension: { policy: "keep-role" },
  };
  let saved = role({ id: "r1", name: "基础工具角色", agentConfig: existing });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "基础工具角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const form = within(await screen.findByRole("dialog", { name: "角色资料" }));
  expect(form.getByLabelText("读取文件（read_file）")).toBeChecked();
  expect(form.getByLabelText("列出目录（list_dir）")).not.toBeChecked();
  expect(form.getByLabelText("写入文件（write_file）")).not.toBeChecked();
  await user.click(form.getByLabelText("写入文件（write_file）"));
  await user.click(form.getByLabelText("搜索网页（web_search）"));
  await user.click(form.getByLabelText("搜索消息原文（search_messages）"));
  await user.click(form.getByLabelText("记住信息（memorize）"));
  await user.click(form.getByLabelText("按需发现工具（tool_search）"));
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));
  expect(saved.agentConfig).toMatchObject({
    ...existing, enabledTools: ["read_file", "calendar.search", "write_file", "web_search", "search_messages", "memorize", "tool_search"],
  });
  await user.click(form.getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const reopened = within(await screen.findByRole("dialog", { name: "角色资料" }));
  expect(reopened.getByLabelText("写入文件（write_file）")).toBeChecked();
  expect(reopened.getByLabelText("抓取网页（web_fetch）")).not.toBeChecked();
  expect(reopened.getByLabelText("撤销记忆（forget_memory）")).not.toBeChecked();
  await user.click(reopened.getByLabelText("写入文件（write_file）"));
  await user.type(reopened.getByRole("textbox", { name: /^名称/ }), "改名");
  await user.click(reopened.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PUT")).toHaveLength(2));
  expect(saved.agentConfig?.enabledTools).toEqual(["read_file", "calendar.search", "web_search", "search_messages", "memorize", "tool_search"]);
  expect(saved.agentConfig).toMatchObject({ extension: existing.extension, shell: existing.shell });
});

it("新建角色默认不授权，刷新恢复逐项创建草稿并只保存用户选择", async () => {
  let saved = role({ id: "new-role", name: "新工具角色" });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [] } }),
    "POST /api/roles": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { status: 201, body: { role: saved } }; },
    "GET /api/roles/new-role/session": () => ({ body: { session: { sessionKey: "role:new-role", roleId: "new-role" }, messages: [] } }),
  });
  const user = userEvent.setup();
  const first = render(<App />);
  await user.click(await screen.findByRole("button", { name: "创建角色" }));
  const form = within(await screen.findByRole("dialog", { name: "创建角色" }));
  for (const label of toolLabels) expect(form.getByLabelText(label)).not.toBeChecked();
  await user.type(form.getByRole("textbox", { name: /^名称/ }), "新工具角色");
  await user.type(form.getByRole("textbox", { name: /^角色设定/ }), "设定");
  await user.click(form.getByLabelText("列出目录（list_dir）"));
  await user.click(form.getByLabelText("读取消息原文（fetch_messages）"));
  first.unmount();
  render(<App />);
  const restored = within(await screen.findByRole("dialog", { name: "创建角色" }));
  expect(restored.getByLabelText("列出目录（list_dir）")).toBeChecked();
  expect(restored.getByLabelText("读取消息原文（fetch_messages）")).toBeChecked();
  expect(restored.getByLabelText("写入文件（write_file）")).not.toBeChecked();
  await user.click(restored.getByLabelText("编辑文件（edit_file）"));
  await user.click(restored.getByLabelText("抓取网页（web_fetch）"));
  await user.click(restored.getByLabelText("撤销记忆（forget_memory）"));
  await user.click(restored.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
  expect(saved.agentConfig?.enabledTools).toEqual(["list_dir", "fetch_messages", "edit_file", "web_fetch", "forget_memory"]);
});

it("未保存授权不改变目录，保存后刷新目录显示宿主拒绝并保留选择", async () => {
  let saved = role({ id: "r1", name: "目录角色", agentConfig: { enabledTools: [] } });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
    "GET /api/roles/r1/tools": () => ({ body: { tools: [{ name: "web_fetch", description: "读取网页", enabled: saved.agentConfig?.enabledTools?.includes("web_fetch") === true,
      authorized: false, modelVisible: false, reason: "宿主联网已关闭", dependency: { status: "unavailable", reason: "宿主未许可联网" } }], plugins: [] } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "目录角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const form = within(await screen.findByRole("dialog", { name: "角色资料" }));
  await user.click(form.getByLabelText("抓取网页（web_fetch）"));
  expect(form.getByText(/工具设置尚未保存/)).toBeInTheDocument();
  await user.click(form.getByRole("button", { name: "查看实际工具" }));
  expect(within(await form.findByRole("list", { name: "实际工具" })).getByText("未启用")).toBeInTheDocument();
  expect(form.getByText(/授权冻结/)).toBeInTheDocument();
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(form.queryByText(/工具设置尚未保存/)).not.toBeInTheDocument());
  await user.click(form.getByRole("button", { name: "刷新工具目录" }));
  const tools = within(await form.findByRole("list", { name: "实际工具" }));
  expect(await tools.findByText("已启用")).toBeInTheDocument();
  expect(tools.getByText("策略未允许")).toBeInTheDocument();
  expect(tools.getByText("宿主联网已关闭")).toBeInTheDocument();
  expect(form.getByLabelText("抓取网页（web_fetch）")).toBeChecked();
});
