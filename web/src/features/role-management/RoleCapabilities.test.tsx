import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, role } from "../../test/fakeBackend";

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("调整默认 Shell 预算并保留旧配置，重开设置仍显示保存值", async () => {
  let saved = role({ id: "r1", name: "工具角色", agentConfig: {
    shell: { enabled: true, allowedCommands: ["python"], timeoutSeconds: 45, maxOutputChars: 30000 },
    memoryRecall: { enabled: true }, browserUse: { enabled: true },
    enabledTools: ["calendar.search"], enabledPlugins: ["calendar"], grantedCapabilities: ["calendar.read"],
  } });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1": () => ({ body: { role: saved } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)), agentConfig: { ...saved.agentConfig, ...JSON.parse(String(init?.body)).agentConfig } }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "工具角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "插件" }));
  await user.click(screen.getByRole("button", { name: "服务与连接" }));
  await user.click(await screen.findByRole("button", { name: "高级运行设置" }));
  await user.click(screen.getByRole("button", { name: "角色参数" }));
  await screen.findByRole("combobox", { name: "角色参数" });
  const form = within(await screen.findByRole("dialog", { name: "设置" }));
  expect(form.queryByLabelText("允许执行 Shell 命令")).not.toBeInTheDocument();
  expect(form.getByLabelText("Shell 超时（秒）")).toHaveValue(45);
  await user.clear(form.getByLabelText("Shell 超时（秒）"));
  await user.type(form.getByLabelText("Shell 超时（秒）"), "60");
  await user.click(form.getByRole("button", { name: "保存角色参数" }));
  await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));
  expect(saved.agentConfig).toEqual({
    shell: { enabled: true, allowedCommands: ["python"], timeoutSeconds: 60, maxOutputChars: 30000 },
    memoryRecall: { enabled: true }, browserUse: { enabled: true },
    enabledTools: ["calendar.search"], enabledPlugins: ["calendar"], grantedCapabilities: ["calendar.read"],
  });
  await user.click(form.getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "插件" }));
  await user.click(screen.getByRole("button", { name: "服务与连接" }));
  await user.click(await screen.findByRole("button", { name: "高级运行设置" }));
  await user.click(screen.getByRole("button", { name: "角色参数" }));
  await screen.findByRole("combobox", { name: "角色参数" });
  expect(within(await screen.findByRole("dialog", { name: "设置" })).getByLabelText("Shell 超时（秒）")).toHaveValue(60);
});

it("在设置查询已保存配置的真实工具状态，失败后可重试", async () => {
  const saved = role({ id: "r1", name: "工具目录角色" });
  let attempts = 0;
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1": () => ({ body: { role: saved } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/roles/r1/tools": () => ++attempts === 1 ? { status: 503, body: { detail: "目录暂时不可用" } } : { body: {
      tools: [
        { name: "shell", description: "执行白名单程序", enabled: false, authorized: false, modelVisible: false, reason: "角色的 Shell 开关已关闭", dependency: { status: "unchecked", reason: "尚未配置命令白名单" } },
        { name: "agent_browser_open", description: "打开网页", enabled: true, authorized: true, modelVisible: true, reason: "已授权", dependency: { status: "unavailable", reason: "浏览器运行组件缺失" } },
      ], plugins: [{ id: "browser_use", enabled: true, authorized: true, reason: "仅检查声明" }],
    } },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "工具目录角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "插件" }));
  await user.click(screen.getByRole("button", { name: "服务与连接" }));
  await user.click(await screen.findByRole("button", { name: "高级运行设置" }));
  await user.click(screen.getByRole("button", { name: "角色参数" }));
  await screen.findByRole("combobox", { name: "角色参数" });
  const form = within(await screen.findByRole("dialog", { name: "设置" }));
  await user.click(form.getByRole("button", { name: "查看实际工具" }));
  expect(await form.findByText("目录暂时不可用")).toBeInTheDocument();
  await user.click(form.getByRole("button", { name: "刷新工具目录" }));
  const tools = within(await form.findByRole("list", { name: "实际工具" }));
  expect(tools.getByText("shell")).toBeInTheDocument();
  expect(tools.getByText("角色的 Shell 开关已关闭")).toBeInTheDocument();
  expect(tools.getByText("浏览器运行组件缺失")).toBeInTheDocument();
  expect(tools.getByText("传给模型")).toBeInTheDocument();
  expect(form.getByText(/已保存配置/)).toBeInTheDocument();
});

it("默认 Shell 不提供开关，修改预算仍保留旧白名单和扩展", async () => {
  let saved = role({ id: "r1", name: "命令角色", agentConfig: {
    shell: { enabled: true, allowedCommands: ["python"], timeoutSeconds: 40, maxOutputChars: 25000 },
    enabledPlugins: ["todo"], grantedCapabilities: ["todo.write"], enabledTools: ["todo.write"],
  } });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1": () => ({ body: { role: saved } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)), agentConfig: { ...saved.agentConfig, ...JSON.parse(String(init?.body)).agentConfig } }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "命令角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "插件" }));
  await user.click(screen.getByRole("button", { name: "服务与连接" }));
  await user.click(await screen.findByRole("button", { name: "高级运行设置" }));
  await user.click(screen.getByRole("button", { name: "角色参数" }));
  await screen.findByRole("combobox", { name: "角色参数" });
  const form = within(await screen.findByRole("dialog", { name: "设置" }));
  expect(form.queryByLabelText("允许执行 Shell 命令")).not.toBeInTheDocument();
  await user.clear(form.getByLabelText("Shell 输出字符上限"));
  await user.type(form.getByLabelText("Shell 输出字符上限"), "27000");
  await user.click(form.getByRole("button", { name: "保存角色参数" }));
  await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));
  expect(saved.agentConfig?.shell).toEqual({ enabled: true, allowedCommands: ["python"], timeoutSeconds: 40, maxOutputChars: 27000 });
  expect(saved.agentConfig?.enabledPlugins).toEqual(["todo"]);
  expect(saved.agentConfig?.grantedCapabilities).toEqual(["todo.write"]);
  expect(saved.agentConfig?.enabledTools).toEqual(["todo.write"]);
});
