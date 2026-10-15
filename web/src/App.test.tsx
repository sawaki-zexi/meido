import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { fakeBackend, message, role, sse } from "./test/fakeBackend";

const alice = role({ id: "r1", name: "爱丽丝", description: "安静的女仆" });
const bella = role({ id: "r2", name: "贝拉" });
const session = { sessionKey: "role:r1", roleId: "r1", createdAt: "2026-09-30T00:00:00Z", updatedAt: "2026-09-30T00:00:00Z" };
const sessionOf = (id: string) => ({ ...session, sessionKey: `role:${id}`, roleId: id });
const emptySession = () => ({ body: { session, messages: [] } });

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });

const roleList = () => screen.findByRole("list", { name: "角色列表" });
const chatHeading = (name: string) => screen.findByRole("heading", { level: 1, name });
const dialog = (name: string) => screen.findByRole("dialog", { name });

describe("首页", () => {
  it("从手机会话进入角色账号，关闭账号详情后仍留在角色页，全局身份独立可用", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": emptySession,
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [] } }),
      "GET /api/channels/settings": () => ({ body: { channels: [{ channel: "telegram", name: "Telegram", enabled: true, configuredAccounts: 0, connectedAccounts: 0 }] } }),
      "GET /api/channels/identities": () => ({ body: { identities: [] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(screen.getByRole("button", { name: "手机会话" }));
    await user.click(screen.getByRole("button", { name: "角色账号" }));
    const profile = await screen.findByRole("dialog", { name: "角色资料" });
    expect(within(profile).getByRole("tab", { name: "账号" })).toHaveAttribute("aria-selected", "true");
    await user.click(await within(profile).findByRole("button", { name: /Telegram/ }));
    expect(await screen.findByLabelText("Telegram Bot Token")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByLabelText("Telegram Bot Token")).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "角色资料" })).toBeVisible();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "设置" }));
    await user.click(screen.getByRole("button", { name: "我的身份" }));
    expect(screen.getByRole("button", { name: "生成配对码" })).toBeEnabled();
    expect(screen.queryByLabelText("Telegram Bot Token")).not.toBeInTheDocument();
  });
  it("导航轨道可打开手机会话，保留角色侧栏并仅突出当前页面", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": emptySession,
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [] } }),
    });
    const user = userEvent.setup();
    const { container } = render(<App />);
    await chatHeading("爱丽丝");
    const navigation = screen.getByRole("navigation", { name: "页面" });
    const chatPage = within(navigation).getByRole("button", { name: "对话" });
    const phonePage = within(navigation).getByRole("button", { name: "手机会话" });
    expect(navigation).toHaveClass("app-rail");
    expect(chatPage).toHaveAttribute("aria-current", "page");
    await user.click(within(navigation).getByRole("button", { name: "搜索角色" }));
    expect(container.querySelector(".shell")).toHaveClass("show-list");
    await user.click(phonePage);
    expect(await chatHeading("爱丽丝 · 手机会话")).toBeInTheDocument();
    expect(container.querySelector(".shell")).not.toHaveClass("show-list");
    expect(phonePage).toHaveAttribute("aria-current", "page");
    expect(chatPage).not.toHaveAttribute("aria-current");
    expect(within(navigation).getAllByRole("button", { current: "page" })).toHaveLength(1);
    expect(within(await roleList()).getByRole("button", { name: /爱丽丝/ })).toBeInTheDocument();
    await user.click(chatPage);
    expect(await chatHeading("爱丽丝")).toBeInTheDocument();
    expect(chatPage).toHaveAttribute("aria-current", "page");
    expect(phonePage).not.toHaveAttribute("aria-current");
    expect(within(navigation).getByRole("button", { name: "设置" })).toBeInTheDocument();
  });

  it("从左侧搜索筛选角色，保留列表切换对话并可清除搜索", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice, bella] } }),
      "GET /api/roles/r1/session": emptySession,
      "GET /api/roles/r2/session": () => ({ body: { session: sessionOf("r2"), messages: [] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    const navigation = screen.getByRole("navigation", { name: "页面" });
    expect(within(navigation).getByRole("button", { name: "设置" })).toBeTruthy();
    await user.click(within(navigation).getByRole("button", { name: "搜索角色" }));
    await user.type(screen.getByRole("searchbox", { name: "搜索角色" }), "贝拉");
    expect(within(await roleList()).queryByText("爱丽丝")).toBeNull();
    expect(within(await roleList()).getByText("贝拉")).toBeTruthy();
    await user.click(within(navigation).getByRole("button", { name: "角色" }));
    const cards = screen.getByRole("list", { name: "角色卡牌" });
    expect(within(cards).queryByRole("button", { name: "与爱丽丝对话" })).toBeNull();
    expect(within(cards).getByText("No.002")).toBeTruthy();
    await user.click(within(await roleList()).getByRole("button", { name: /贝拉/ }));
    expect(await chatHeading("贝拉")).toBeTruthy();
    expect(calls.some((call) => call.path === "/api/roles/r2/session")).toBe(true);
    await user.click(within(navigation).getByRole("button", { name: "角色" }));
    await user.clear(screen.getByRole("searchbox", { name: "搜索角色" }));
    await user.type(screen.getByRole("searchbox", { name: "搜索角色" }), "不存在");
    expect(screen.getByText("没有找到匹配的角色")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "清除搜索" }));
    expect(within(await roleList()).getAllByRole("button")).toHaveLength(2);
    expect(within(screen.getByRole("list", { name: "角色卡牌" })).getByRole("button", { name: "与爱丽丝对话" })).toBeTruthy();
  });

  it("从酒馆侧栏导入角色卡，进入现有预览确认流程", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/role-card-import/preview": () => ({ body: {
        draft: { name: "导入角色", description: "简介", profile: "设定", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" },
        source: "card.json", assets: [], compatibility: { format: "json", warnings: [], discardedFields: [], unsupportedMacros: [], unsupportedResources: [] },
      } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/还没有角色。/);
    await user.click(screen.getByRole("button", { name: "角色" }));
    const sidebar = screen.getByRole("complementary", { name: "角色管理" });
    expect(within(sidebar).getByRole("button", { name: "导入角色卡" })).toBeEnabled();
    fireEvent.change(within(sidebar).getByLabelText("导入角色卡文件"), { target: { files: [new File(["{}"], "card.json", { type: "application/json" })] } });
    const form = await dialog("创建角色");
    expect(await within(form).findByRole("region", { name: "角色卡导入预览" })).toBeTruthy();
    await user.click(within(form).getByRole("button", { name: "填充到表单" }));
    expect(within(form).getByLabelText(/名称/)).toHaveValue("导入角色");
    expect(calls.some((call) => call.method === "POST" && call.path === "/api/roles")).toBe(false);
  });

  it("没有角色时仍是对话界面，并提示创建角色", async () => {
    fakeBackend({ "GET /api/roles": () => ({ body: { roles: [] } }) });
    render(<App />);
    expect(screen.getAllByRole("status")[0]).toHaveTextContent("正在加载角色");
    expect(await screen.findByText(/还没有角色。先创建一位/)).toBeTruthy();
    expect(screen.getByLabelText("消息内容")).toBeDisabled();
    expect(screen.getByRole("button", { name: "创建角色" })).toBeTruthy();
  });

  it("进入后直接打开上次对话的角色", async () => {
    localStorage.setItem("meido:last-role", "r2");
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice, bella] } }),
      "GET /api/roles/r2/session": () => ({ body: { session: sessionOf("r2"), messages: [] } }),
    });
    render(<App />);
    expect(await chatHeading("贝拉")).toBeTruthy();
    expect(screen.getByLabelText("消息内容")).not.toBeDisabled();
  });

  it("没有记录时打开第一个角色，并记住当前角色", async () => {
    fakeBackend({ "GET /api/roles": () => ({ body: { roles: [alice] } }), "GET /api/roles/r1/session": emptySession });
    render(<App />);
    expect(await chatHeading("爱丽丝")).toBeTruthy();
    expect(localStorage.getItem("meido:last-role")).toBe("r1");
  });

  it("在聊天待办抽屉中创建并查看主人级待办", async () => {
    const items: Record<string, unknown>[] = [];
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": emptySession,
      "GET /api/todos/settings": () => ({ body: { enabled: true, remindersEnabled: true, timezone: "Asia/Shanghai" } }),
      "GET /api/todos/reminders": () => ({ body: { reminders: [], unreadCount: 0 } }),
      "GET /api/todos/diagnostics": () => ({ body: { items: [] } }),
      "GET /api/todos": () => ({ body: { todos: items } }),
      "POST /api/todos": (_, match) => {
        void match;
        const call = calls.at(-1);
        const body = call?.body as Record<string, unknown>;
        items.push({ ...body, id: "todo-1", status: body.dueDate ? "scheduled" : "inbox", sourceRoleId: null, sourceSessionKey: null, sourceMessageId: null, sourceAvailable: true, createdAt: "2026-10-08T00:00:00Z", updatedAt: "2026-10-08T00:00:00Z", completedAt: null, cancelledAt: null });
        return { status: 201, body: items[0] };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(await screen.findByRole("button", { name: "待办" }));
    const drawer = await dialog("待办");
    await user.click(within(drawer).getByRole("button", { name: "新建待办" }));
    await user.type(within(drawer).getByLabelText("标题"), "提交实验报告");
    await user.click(within(drawer).getByRole("button", { name: "保存" }));
    expect(await within(drawer).findByText("提交实验报告")).toBeTruthy();
    expect(calls.find((call) => call.method === "POST" && call.path === "/api/todos")?.body).toMatchObject({ title: "提交实验报告", timezone: "Asia/Shanghai" });
  });

  it("当前聊天会刷新并显示后台送达的角色主动提醒", async () => {
    let sessionReads = 0;
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => {
        sessionReads += 1;
        return { body: { session, messages: sessionReads === 1 ? [] : [message({ id: "reminder-1", role: "assistant", content: "主人，记得交报告", sessionKey: "role:r1", sequence: 1, messageType: "proactive_reminder" })] } };
      },
    });
    render(<App />);
    await chatHeading("爱丽丝");
    window.dispatchEvent(new Event("focus"));
    expect(await screen.findByText("主人，记得交报告")).toBeTruthy();
    expect(sessionReads).toBe(2);
  });

  it("后端不可用时显示明确错误", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("无法连接本地后端");
  });

  it("在列表中点击角色切换对话", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice, bella] } }),
      "GET /api/roles/r1/session": emptySession,
      "GET /api/roles/r2/session": () => ({ body: { session: sessionOf("r2"), messages: [message({ id: "m", role: "assistant", content: "贝拉的话", sessionKey: "role:r2" })] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(within(await roleList()).getByRole("button", { name: /贝拉/ }));
    expect(await chatHeading("贝拉")).toBeTruthy();
    expect(screen.getByText("贝拉的话")).toBeTruthy();
    expect(localStorage.getItem("meido:last-role")).toBe("r2");
  });
});

describe("设置", () => {
  it("点击设置图标打开设置，其中包含模型配置", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "GET /api/model/providers": () => ({ body: { providers: [{ id: "openai", label: "OpenAI", provider: "openai", baseUrl: "https://api.openai.com/v1" }] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: [], activeId: null } }),
      "GET /api/memory/settings": () => ({ body: { enabled: true, embeddingEnabled: true, embedding: { configuration: { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: false }, status: { configured: false, ready: false, enabled: true } } } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "设置" }));
    const panel = await dialog("设置");
    expect(within(panel).getByRole("button", { name: /模型/ })).toBeTruthy();
    expect(await within(panel).findByLabelText("API 地址")).toHaveValue("https://api.openai.com/v1");
    await user.click(within(panel).getByRole("button", { name: "记忆" }));
    expect(await within(panel).findByRole("heading", { name: "记忆" })).toBeInTheDocument();
    expect(await within(panel).findByLabelText("启用向量模型")).toBeChecked();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("从飞书授权返回时直接打开主人资料设置", async () => {
    window.history.replaceState({}, "", "/app?ownerKnowledge=connected");
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "GET /api/owner-knowledge": () => ({ body: { enabled: false, connected: true, oauthConfigured: true, appId: "cli_local", embeddingReady: true, lastError: null, lastSync: null } }),
      "GET /api/owner-knowledge/documents": () => ({ body: { documents: [] } }),
      "GET /api/model/providers": () => ({ body: { providers: [] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: [], activeId: null } }),
    });

    render(<App />);

    const panel = await dialog("设置");
    expect(within(panel).getByRole("button", { name: /主人资料/ })).toHaveAttribute("aria-current", "true");
    expect(await within(panel).findByText("飞书授权成功，可以启用主人资料插件")).toBeInTheDocument();
  });

  it("可以独立暂停待办提醒而保留待办插件", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "GET /api/model/providers": () => ({ body: { providers: [] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: [], activeId: null } }),
      "GET /api/todos/settings": () => ({ body: { enabled: true, remindersEnabled: true, timezone: "Asia/Shanghai" } }),
      "PUT /api/todos/settings": (_, match) => {
        void match;
        const body = calls.at(-1)?.body as Record<string, unknown>;
        return { body: { enabled: true, remindersEnabled: body.remindersEnabled ?? true, timezone: "Asia/Shanghai" } };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "设置" }));
    const panel = await dialog("设置");
    await user.click(within(panel).getByRole("button", { name: "待办" }));
    const reminders = within(panel).getByRole("checkbox", { name: "启用提醒" });
    expect(reminders).toBeChecked();
    await user.click(reminders);
    await waitFor(() => expect(calls.find((call) => call.method === "PUT" && call.path === "/api/todos/settings")?.body).toMatchObject({ remindersEnabled: false }));
    expect(within(panel).getByRole("checkbox", { name: "启用待办插件" })).toBeChecked();
  });

  it("可以选择提醒角色并暂停今天的提醒", async () => {
    let roleEnabled = false;
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "GET /api/model/providers": () => ({ body: { providers: [] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: [], activeId: null } }),
      "GET /api/todos/settings": () => ({ body: { enabled: true, remindersEnabled: true, timezone: "Asia/Shanghai" } }),
      "GET /api/todos/reminder-roles": () => ({ body: { roles: [{ id: "r1", name: "爱丽丝", enabled: roleEnabled, activeToday: false, effectiveDate: roleEnabled ? "2026-10-10" : "2026-10-09" }] } }),
      "PUT /api/todos/reminder-roles/r1": () => { roleEnabled = true; return { body: { roleId: "r1", enabled: true, effectiveDate: "2026-10-10" } }; },
      "GET /api/todos/reminders/today": () => ({ body: { date: "2026-10-09", paused: false } }),
      "PUT /api/todos/reminders/today\\?paused=true": () => ({ body: { date: "2026-10-09", paused: true } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "设置" }));
    const panel = await dialog("设置");
    await user.click(within(panel).getByRole("button", { name: "待办" }));

    expect(await within(panel).findByText("未启用提醒角色，系统不会发送提醒。")).toBeTruthy();
    const role = await within(panel).findByRole("checkbox", { name: "爱丽丝" });
    await user.click(role);
    await waitFor(() => expect(calls.find((call) => call.method === "PUT" && call.path === "/api/todos/reminder-roles/r1")?.body).toEqual({ enabled: true }));
    expect(await within(panel).findByText("2026-10-10 起生效")).toBeTruthy();
    expect(await within(panel).findByText("提醒角色从生效日期起参与；今天不会发送提醒。")).toBeTruthy();
    await user.click(within(panel).getByRole("checkbox", { name: "暂停今天的提醒" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path === "/api/todos/reminders/today?paused=true")).toBe(true));
    expect(await within(panel).findByText(/暂停期间跳过的提醒不会补发/)).toBeTruthy();
  });
});

describe("角色管理", () => {
  it("新建角色时显示字段提示，并默认收起非核心设定", async () => {
    fakeBackend({ "GET /api/roles": () => ({ body: { roles: [] } }) });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "创建角色" }));
    const form = await dialog("创建角色");

    expect(within(form).getByLabelText(/名称/)).toHaveAttribute("placeholder", "给她起个名字");
    expect(within(form).getByLabelText(/角色设定/)).toHaveAttribute("placeholder", "描述她的身份、背景，以及你们之间的关系");
    expect(within(form).queryByLabelText(/性格/)).toBeNull();
    const more = within(form).getByRole("button", { name: "更多设定" });
    expect(more).toHaveAttribute("aria-expanded", "false");

    await user.click(more);
    expect(within(form).getByLabelText(/性格/)).toHaveAttribute("placeholder", "例如：温柔、细心，偶尔会开些小玩笑");
    expect(within(form).getByRole("button", { name: "收起更多设定" })).toHaveAttribute("aria-expanded", "true");
  });

  it("预览角色卡后可取消保留原草稿，确认后只填充表单", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/role-card-import/preview": () => ({ body: {
        draft: { name: "导入角色", description: "导入简介", profile: "导入设定", personality: "温柔", behaviorRules: "", responseConstraints: "", nickname: "主人" },
        source: "card.json", assets: [], compatibility: { format: "json", warnings: [], discardedFields: [], unsupportedMacros: [], unsupportedResources: [] },
      } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "创建角色" }));
    const form = await dialog("创建角色");
    await user.type(within(form).getByLabelText(/名称/), "当前草稿");
    const file = new File([JSON.stringify({ name: "导入角色" })], "card.json", { type: "application/json" });
    const input = within(form).getByLabelText("导入角色卡");
    fireEvent.change(input, { target: { files: [file] } });
    const preview = await within(form).findByRole("region", { name: "角色卡导入预览" });
    await user.click(within(preview).getByRole("button", { name: "取消" }));
    expect(within(form).getByLabelText(/名称/)).toHaveValue("当前草稿");

    fireEvent.change(input, { target: { files: [file] } });
    await within(form).findByRole("region", { name: "角色卡导入预览" });
    await user.click(within(form).getByRole("button", { name: "填充到表单" }));
    expect(within(form).getByLabelText(/名称/)).toHaveValue("导入角色");
    expect(within(form).getByLabelText(/简介/)).toHaveValue("导入简介");
    expect(within(form).getByLabelText(/角色设定/)).toHaveValue("导入设定");
    expect(calls.filter((call) => call.method === "POST" && call.path === "/api/role-card-import/preview")).toHaveLength(2);
    expect(calls.some((call) => call.method === "POST" && call.path === "/api/roles")).toBe(false);
  });

  it("角色卡素材按类型进入头像或卡片图裁剪", async () => {
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:import-preview"), revokeObjectURL: vi.fn() });
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/role-card-import/preview": () => ({ body: {
        draft: { name: "导入角色", description: "", profile: "设定", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" },
        source: "card.charx", assets: [
          { kind: "avatar", name: "main.png", mediaType: "image/png", size: 3, dataUrl: "data:image/png;base64,YWJj" },
          { kind: "background", name: "background.png", mediaType: "image/png", size: 3, dataUrl: "data:image/png;base64,YWJj" },
        ], compatibility: { format: "charx", warnings: [], discardedFields: [], unsupportedMacros: [], unsupportedResources: [] },
      } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "创建角色" }));
    const form = await dialog("创建角色");
    const input = within(form).getByLabelText("导入角色卡");
    fireEvent.change(input, { target: { files: [new File(["card"], "card.charx")] } });
    const preview = await within(form).findByRole("region", { name: "角色卡导入预览" });
    await user.click(within(preview).getByRole("button", { name: /main\.png.*头像/ }));
    expect(within(form).getByRole("region", { name: "调整头像裁切" })).toBeTruthy();
  });

  it("在角色酒馆中创建角色，生成她的专属卡牌，再从卡牌进入对话", async () => {
    const created = role({ id: "r2", name: "新角色", description: "新来的", profile: { ...alice.profile, profile: "她的设定" } });
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/roles": () => ({ status: 201, body: { role: created } }),
      "GET /api/roles/r2/session": () => ({ body: { session: sessionOf("r2"), messages: [] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/还没有角色。/);
    await user.click(screen.getByRole("button", { name: "角色" }));
    await user.click(await screen.findByRole("button", { name: "新建角色" }));
    const form = await dialog("创建角色");
    const preview = within(form).getByRole("complementary", { name: "卡牌预览" });
    expect(within(preview).getByText("No.001")).toBeTruthy();
    await user.type(within(form).getByLabelText(/名称/), "新角色");
    expect(within(form).getByLabelText(/名称/)).toHaveValue("新角色");
    await user.type(within(form).getByLabelText(/角色设定/), "她的设定");
    await user.click(within(form).getByRole("button", { name: "保存" }));

    const reveal = await dialog("新卡牌");
    expect(within(reveal).getByText("新角色", { selector: "strong" })).toBeTruthy();
    expect(within(reveal).getByText("No.001")).toBeTruthy();
    expect(within(await screen.findByRole("list", { name: "角色卡牌" })).getByRole("button", { name: "编辑新角色" })).toBeTruthy();
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      name: "新角色", description: "",
      profile: { profile: "她的设定", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" },
      agentConfig: { memoryRecall: { enabled: false }, enabledPlugins: [], grantedCapabilities: [], enabledTools: [] },
    });
    expect(sessionStorage.getItem("meido:create-role-draft")).toBeNull();

    await user.click(within(reveal).getByRole("button", { name: "开始对话" }));
    expect(await chatHeading("新角色")).toBeTruthy();
    expect(within(await roleList()).getByText("新角色")).toBeTruthy();
  });

  it("创建角色时可以启用主动记忆检索", async () => {
    const created = role({ id: "r4", name: "记忆角色" });
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/roles": () => ({ status: 201, body: { role: created } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "创建角色" }));
    const form = await dialog("创建角色");
    await user.type(within(form).getByLabelText(/名称/), "记忆角色");
    await user.type(within(form).getByLabelText(/角色设定/), "设定");
    await user.click(within(form).getByRole("checkbox", { name: "允许主动检索记忆" }));
    await user.click(within(form).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(calls.find((call) => call.method === "POST")?.body).toMatchObject({
      agentConfig: { memoryRecall: { enabled: true } },
    }));
  });

  it("可以为角色显式启用只读待办查询工具", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/roles": () => ({ body: { role: role({ id: "r1", name: "待办角色" }) } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "创建角色" }));
    const form = await dialog("创建角色");
    await user.type(within(form).getByLabelText(/名称/), "待办角色");
    await user.type(within(form).getByLabelText(/角色设定/), "设定");
    await user.click(within(form).getByRole("checkbox", { name: "允许查询主人待办" }));
    await user.click(within(form).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(calls.find((call) => call.method === "POST" && call.path === "/api/roles")?.body).toMatchObject({
      agentConfig: { enabledPlugins: ["todo"], grantedCapabilities: ["todo.read"], enabledTools: ["todo.search"] },
    }));
  });

  it("创建角色时预览并上传头像，保存后显示在角色卡", async () => {
    const previewUrl = "blob:portrait-preview";
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => previewUrl), revokeObjectURL: vi.fn() });
    const created = role({ id: "r3", name: "新角色" });
    const withAvatar = { ...created, avatarUrl: "/api/roles/r3/avatar" };
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/roles": () => ({ status: 201, body: { role: created } }),
      "POST /api/roles/r3/avatar": () => ({ body: { role: withAvatar } }),
      "POST /api/roles/r3/avatar-original": () => ({ body: { role: { ...withAvatar, avatarOriginalUrl: "/api/roles/r3/avatar-original" } } }),
      "POST /api/roles/r3/card-image": () => ({ body: { role: { ...withAvatar, avatarOriginalUrl: "/api/roles/r3/avatar-original", cardImageUrl: "/api/roles/r3/card-image" } } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "创建角色" }));
    const form = await dialog("创建角色");
    await user.type(within(form).getByLabelText(/名称/), "新角色");
    await user.type(within(form).getByLabelText(/角色设定/), "设定");
    const image = new File(["portrait"], "portrait.png", { type: "image/png" });
    await user.upload(within(form).getByLabelText("角色头像"), image);

    const crop = within(form).getByRole("region", { name: "调整头像裁切" });
    expect(crop.querySelector(".avatar-crop-window")).toHaveClass("crop-circle");
    const cropImage = within(crop).getByRole("img", { name: "头像裁切预览" });
    Object.defineProperties(cropImage, { naturalWidth: { configurable: true, value: 800 }, naturalHeight: { configurable: true, value: 600 } });
    fireEvent.load(cropImage);
    const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => callback(new Blob(["cropped"], { type: "image/png" })));
    await user.click(within(crop).getByRole("button", { name: "保存" }));

    expect(within(form).getByRole("img", { name: "新角色卡片图片" })).toHaveAttribute("src", previewUrl);
    const cardImage = new File(["card"], "card.png", { type: "image/png" });
    await user.upload(within(form).getByLabelText("卡片图片"), cardImage);
    const cardCrop = within(form).getByRole("region", { name: "调整头像裁切" });
    const cardCropImage = within(cardCrop).getByRole("img", { name: "头像裁切预览" });
    Object.defineProperties(cardCropImage, { naturalWidth: { configurable: true, value: 600 }, naturalHeight: { configurable: true, value: 800 } });
    fireEvent.load(cardCropImage);
    await user.click(within(cardCrop).getByRole("button", { name: "保存" }));
    await user.click(within(form).getByRole("button", { name: "保存" }));

    const reveal = await dialog("新卡牌");
    expect(within(reveal).getByRole("img", { name: "新角色卡片图片" })).toHaveAttribute("src", "/api/roles/r3/card-image");
    await user.click(within(reveal).getByRole("button", { name: "稍后再聊" }));
    await user.click(screen.getByRole("button", { name: "角色" }));
    const cards = await screen.findByRole("list", { name: "角色卡牌" });
    expect(cards.querySelector('img[src="/api/roles/r3/card-image"]')).toBeTruthy();
    const uploaded = calls.find((call) => call.method === "POST" && call.path === "/api/roles/r3/avatar")?.body as File;
    expect(uploaded).toBeInstanceOf(File);
    expect(uploaded).not.toBe(image);
    expect(drawImage).toHaveBeenCalled();
    const uploadedCard = calls.find((call) => call.method === "POST" && call.path === "/api/roles/r3/card-image")?.body as File;
    expect(uploadedCard).toBeInstanceOf(File);
    expect(uploadedCard).not.toBe(cardImage);
  });

  it("点击酒馆角色卡头像可预览大图", async () => {
    const withAvatar = { ...alice, avatarUrl: "/api/roles/r1/avatar" };
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [withAvatar] } }),
      "GET /api/roles/r1/session": emptySession,
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(screen.getByRole("button", { name: "角色" }));
    const cards = await screen.findByRole("list", { name: "角色卡牌" });
    await user.click(within(cards).getByRole("button", { name: "预览爱丽丝头像" }));
    const preview = await dialog("爱丽丝头像");
    expect(within(preview).getByRole("img", { name: "爱丽丝头像" })).toHaveAttribute("src", withAvatar.avatarUrl);
  });

  it("编辑角色资料时可以移除已有头像", async () => {
    const withAvatar = { ...alice, avatarUrl: "/api/roles/r1/avatar?v=old" };
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [withAvatar] } }),
      "GET /api/roles/r1/session": emptySession,
      "PUT /api/roles/r1": () => ({ body: { role: withAvatar } }),
      "DELETE /api/roles/r1/avatar": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(screen.getByRole("button", { name: "角色" }));
    const cards = await screen.findByRole("list", { name: "角色卡牌" });
    expect(cards.querySelector(`img[src="${withAvatar.avatarUrl}"]`)).toBeTruthy();
    await user.click(within(cards).getByRole("button", { name: "编辑爱丽丝" }));
    const profile = await dialog("角色资料");
    expect(within(profile).getByLabelText(/名称/)).not.toBeDisabled();
    await user.click(within(profile).getByRole("button", { name: "移除" }));
    await user.click(within(profile).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(calls.some((call) => call.method === "DELETE" && call.path === "/api/roles/r1/avatar")).toBe(true));
    expect(cards.querySelector("img")).toBeNull();
    expect(within(profile).getByLabelText(/名称/)).toHaveValue("爱丽丝");
  });

  it("从对话页打开创建页，关闭后回到对话页", async () => {
    fakeBackend({ "GET /api/roles": () => ({ body: { roles: [] } }) });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "创建角色" }));
    await user.click(within(await dialog("创建角色")).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText(/还没有角色。先创建一位/)).toBeTruthy();
    expect(screen.queryByRole("heading", { level: 1, name: "角色酒馆" })).toBeNull();
  });

  it("在酒馆和对话之间切换，取消创建后仍可回到对话", async () => {
    fakeBackend({ "GET /api/roles": () => ({ body: { roles: [alice] } }), "GET /api/roles/r1/session": emptySession });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    const tabs = screen.getByRole("navigation", { name: "页面" });
    expect(within(tabs).getByRole("button", { name: "对话" })).toHaveAttribute("aria-current", "page");
    await user.click(within(tabs).getByRole("button", { name: "角色" }));
    await user.click(await screen.findByRole("button", { name: "新建角色" }));
    await user.click(within(await dialog("创建角色")).getByRole("button", { name: "取消" }));
    expect(screen.getByRole("heading", { level: 1, name: "角色酒馆" })).toBeTruthy();
    await user.click(within(tabs).getByRole("button", { name: "对话" }));
    expect(await chatHeading("爱丽丝")).toBeTruthy();
    expect(within(tabs).getByRole("button", { name: "对话" })).toHaveAttribute("aria-current", "page");
  });

  it("角色酒馆以卡牌展示全部角色，可查看资料或直接对话", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice, bella] } }),
      "GET /api/roles/r1/session": emptySession,
      "GET /api/roles/r2/session": () => ({ body: { session: sessionOf("r2"), messages: [] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(screen.getByRole("button", { name: "角色" }));
    const cards = await screen.findByRole("list", { name: "角色卡牌" });
    expect(within(cards).getByText("安静的女仆")).toBeTruthy();
    expect(within(cards).getByText("No.002")).toBeTruthy();

    await user.click(within(cards).getByRole("button", { name: "编辑贝拉" }));
    const profile = await dialog("角色资料");
    expect(within(profile).getByLabelText(/名称/)).toHaveValue("贝拉");
    expect(within(profile).getByLabelText(/名称/)).not.toBeDisabled();
    expect(within(within(profile).getByRole("complementary", { name: "卡牌预览" })).getByText("No.002")).toBeTruthy();
    expect(profile.closest(".dialog-layer")).toHaveClass("dialog-page");
    await user.keyboard("{Escape}");

    await user.click(within(cards).getByRole("button", { name: "与贝拉对话" }));
    expect(await chatHeading("贝拉")).toBeTruthy();
  });

  it("保存失败时保留草稿并显示原因", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/roles": () => ({ status: 422, body: { detail: [{ msg: "名称不能为空" }] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "创建角色" }));
    const form = await dialog("创建角色");
    await user.type(within(form).getByLabelText(/名称/), "  ");
    await user.type(within(form).getByLabelText(/角色设定/), "设定");
    await user.click(within(form).getByRole("button", { name: "保存" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent("名称不能为空");
    expect(within(form).getByLabelText(/角色设定/)).toHaveValue("设定");
    expect(JSON.parse(sessionStorage.getItem("meido:create-role-draft")!).profile).toBe("设定");
  });

  it("刷新后恢复创建草稿；关闭面板不丢草稿", async () => {
    sessionStorage.setItem("meido:create-role-draft", JSON.stringify({ name: "草稿", profile: "未完成" }));
    fakeBackend({ "GET /api/roles": () => ({ body: { roles: [] } }) });
    const user = userEvent.setup();
    render(<App />);
    const form = await dialog("创建角色");
    expect(within(form).getByLabelText(/名称/)).toHaveValue("草稿");
    await user.click(within(form).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(await screen.findByRole("button", { name: "创建角色" }));
    expect(within(await dialog("创建角色")).getByLabelText(/名称/)).toHaveValue("草稿");
  });

  it("打开角色资料后直接编辑，取消恢复原值，保存后更新", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": emptySession,
      "PUT /api/roles/r1": (init) => ({ body: { role: { ...alice, name: JSON.parse(String(init!.body)).name } } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(screen.getByRole("button", { name: "角色资料" }));
    const panel = await dialog("角色资料");
    const name = within(panel).getByLabelText(/名称/);
    expect(name).not.toBeDisabled();
    await user.clear(name);
    await user.type(name, "临时");
    await user.click(within(panel).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog", { name: "角色资料" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "角色资料" }));
    const reopened = await dialog("角色资料");
    expect(within(reopened).getByLabelText(/名称/)).toHaveValue("爱丽丝");
    expect(within(reopened).getByLabelText(/名称/)).not.toBeDisabled();
    await user.clear(within(reopened).getByLabelText(/名称/));
    await user.type(within(reopened).getByLabelText(/名称/), "爱丽丝二号");
    await user.click(within(reopened).getByRole("button", { name: "保存" }));
    expect(await within(await roleList()).findByText("爱丽丝二号")).toBeTruthy();
    expect(await chatHeading("爱丽丝二号")).toBeTruthy();
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
  });

  it("确认后删除角色；取消确认则不删除", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": emptySession,
      "DELETE /api/roles/r1": () => ({ status: 204 }),
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(screen.getByRole("button", { name: "角色资料" }));
    const panel = await dialog("角色资料");

    await user.click(within(panel).getByRole("button", { name: "删除角色" }));
    expect(calls.some((call) => call.method === "DELETE")).toBe(false);

    await user.click(within(panel).getByRole("button", { name: "删除角色" }));
    expect(await screen.findByText(/还没有角色。先创建一位/)).toBeTruthy();
    expect(confirm.mock.calls[1][0]).toContain("爱丽丝");
  });
});

describe("唯一会话", () => {
  it("空会话提示发送第一条消息", async () => {
    fakeBackend({ "GET /api/roles": () => ({ body: { roles: [alice] } }), "GET /api/roles/r1/session": emptySession });
    render(<App />);
    expect(await screen.findByText("发送第一条消息开始对话。")).toBeTruthy();
  });

  it("发送消息后流式显示回复，完成后可继续对话", async () => {
    const assistant = message({ id: "a1", role: "assistant", content: "您好，主人", sequence: 2 });
    let turn = 0;
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [message({ id: "old", role: "user", content: "以前的话" })] } }),
      "POST /api/roles/r1/messages": () => {
        turn += 1;
        const id = `t${turn}`;
        return { stream: [
          sse("user_message_accepted", { message: message({ id: `u${turn}`, role: "user", content: `第${turn}句` }) }),
          sse("assistant_generation_started", { messageId: id }),
          // Split a delta across chunks to exercise buffering.
          sse("assistant_delta", { messageId: id, delta: "您好" }).slice(0, 20),
          sse("assistant_delta", { messageId: id, delta: "您好" }).slice(20),
          sse("assistant_delta", { messageId: id, delta: "，主人" }),
          sse("assistant_completed", { message: { ...assistant, id } }),
        ] };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByText("以前的话")).toBeTruthy();

    const input = screen.getByLabelText("消息内容");
    await user.type(input, "第1句{Enter}");
    expect(await screen.findByText("您好，主人")).toBeTruthy();
    expect(input).toHaveValue("");
    await waitFor(() => expect(input).not.toBeDisabled());
    expect(screen.queryByText("生成中")).toBeNull();

    await user.type(input, "第2句");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(screen.getAllByText("您好，主人")).toHaveLength(2));
    expect(calls.filter((call) => call.method === "POST").map((call) => call.body)).toEqual([{ content: "第1句" }, { content: "第2句" }]);
  });

  it("Shift+Enter 换行而不发送，空白消息不能发送", async () => {
    const { calls } = fakeBackend({ "GET /api/roles": () => ({ body: { roles: [alice] } }), "GET /api/roles/r1/session": emptySession });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    const input = screen.getByLabelText("消息内容");
    await user.type(input, "   ");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    await user.type(input, "a{Shift>}{Enter}{/Shift}b");
    expect(input).toHaveValue("   a\nb");
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("生成失败时标记未完成并显示原因，之后仍可发送", async () => {
    let turn = 0;
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": emptySession,
      "POST /api/roles/r1/messages": () => {
        turn += 1;
        if (turn === 2) return { status: 409, body: { detail: "该角色正在生成回复，请稍后再试" } };
        return { stream: [
          sse("user_message_accepted", { message: message({ id: "u1", role: "user", content: "你好" }) }),
          sse("assistant_generation_started", { messageId: "a1" }),
          sse("assistant_delta", { messageId: "a1", delta: "半句" }),
          sse("assistant_failed", { message: message({ id: "a1", role: "assistant", content: "半句", status: "failed" }), error: "模型服务超时" }),
        ] };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    const input = screen.getByLabelText("消息内容");
    await user.type(input, "你好{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("模型服务超时");
    expect(screen.getByText("未完成")).toBeTruthy();
    expect(screen.getByText("半句")).toBeTruthy();

    await waitFor(() => expect(input).not.toBeDisabled());
    await user.type(input, "再试{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("该角色正在生成回复");
  });

  it("输入区可以上传、预览、移除并单独发送附件", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": emptySession,
      "GET /api/model/configurations": () => ({ body: { configurations: [] } }),
      "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: null, effectiveConfigurationId: null } }),
      "POST /api/roles/r1/attachments": () => ({ body: { id: "att-1", name: "photo.png", mediaType: "image/png", size: 8, kind: "image" } }),
      "POST /api/roles/r1/messages": () => ({ stream: [
        sse("user_message_accepted", { message: { ...message({ id: "u1", role: "user", content: "" }), metadata: { attachments: [{ id: "att-1", name: "photo.png", mediaType: "image/png", size: 8, kind: "image" }] } } }),
        sse("assistant_generation_started", { messageId: "a1" }),
        sse("assistant_completed", { message: message({ id: "a1", role: "assistant", content: "看到了" }) }),
      ] }),
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.upload(screen.getByLabelText("附件文件"), new File(["png"], "photo.png", { type: "image/png" }));
    expect(await screen.findByAltText("photo.png")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "移除 photo.png" }));
    expect(screen.queryByAltText("photo.png")).toBeNull();
    await user.upload(screen.getByLabelText("附件文件"), new File(["png"], "photo.png", { type: "image/png" }));
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(calls.find((call) => call.method === "POST" && call.path === "/api/roles/r1/messages")?.body).toEqual({ content: "", attachments: ["att-1"] }));
    expect(await screen.findByAltText("photo.png")).toHaveAttribute("src", "/api/roles/r1/attachments/att-1");
  });

  it("拖放附件后保留文件条目，并在历史消息中显示附件", async () => {
    const history = message({ id: "m1", role: "user", content: "", metadata: { attachments: [{ id: "att-2", name: "notes.txt", mediaType: "text/plain", size: 5, kind: "file" }] } });
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [history] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: [] } }),
      "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: null, effectiveConfigurationId: null } }),
      "POST /api/roles/r1/attachments": () => ({ body: { id: "att-2", name: "notes.txt", mediaType: "text/plain", size: 5, kind: "file" } }),
    });
    render(<App />);
    await chatHeading("爱丽丝");
    expect(screen.getByRole("link", { name: "notes.txt" })).toHaveAttribute("href", "/api/roles/r1/attachments/att-2");
    fireEvent.drop(screen.getByRole("form", { name: "聊天输入" }), { dataTransfer: { files: [new File(["hello"], "notes.txt", { type: "text/plain" })] } });
    expect(await screen.findByText("notes.txt")).toBeTruthy();
  });

  it("打开会话失败时停留在首页并显示错误", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ status: 404, body: { detail: "角色不存在" } }),
    });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("角色不存在");
    expect(within(await roleList()).getByText("爱丽丝")).toBeTruthy();
    expect(screen.getByText(/从左侧选一位角色/)).toBeTruthy();
  });
});

describe("角色记忆", () => {
  const origin = { kind: "message", sessionKey: "role:r1", messageIds: ["m2"], messageRange: null, stableSourceKey: "message:m2" };
  const memory = { id: "mem1", roleId: "r1", memoryType: "preference", summary: "主人喜欢红茶", status: "active", happenedAt: null, createdAt: "2026-09-30T00:00:00Z", updatedAt: "2026-09-30T00:00:00Z", sourceRef: { ...origin, sourceKeys: [], sources: [origin] } };

  it("从对话打开角色记忆，并定位到记忆来源的消息", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [message({ id: "m1", role: "user", sequence: 1, content: "早安" }), message({ id: "m2", role: "user", sequence: 2, content: "我喜欢红茶" })] } }),
      "GET /api/roles/r1/memories": () => ({ body: { memories: [memory] } }),
      "GET /api/roles/r1/memory-documents": () => ({ body: { documents: [], journals: [] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(screen.getByRole("button", { name: "角色记忆" }));
    const list = await screen.findByRole("list", { name: "记忆列表" });
    expect(within(list).getByText("主人喜欢红茶")).toBeTruthy();
    expect(within(list).getByText("偏好")).toBeTruthy();

    await user.click(within(list).getByRole("button", { name: "定位到消息" }));
    expect(await chatHeading("爱丽丝")).toBeTruthy();
    const source = screen.getByText("我喜欢红茶").closest("li")!;
    expect(source).toHaveClass("source-highlight");
    expect(within(source).getByText("记忆来源")).toBeTruthy();
  });

  it("渠道记忆来源打开当前角色的手机会话并定位消息", async () => {
    const channelOrigin = { kind: "turn", sessionKey: "thread:phone", messageIds: ["phone-source"], messageRange: [7, 8], stableSourceKey: "turn:phone" };
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": emptySession,
      "GET /api/roles/r1/memories": () => ({ body: { memories: [{ ...memory, sourceRef: { ...channelOrigin, sourceKeys: [], sources: [channelOrigin] } }] } }),
      "GET /api/roles/r1/memory-documents": () => ({ body: { documents: [], journals: [] } }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "phone", sessionKey: "thread:phone", channel: "telegram", chatId: "42", chatType: "private", title: "主人手机", lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET .*threads/phone/messages.*": () => ({ body: { messages: [{ id: "phone-source", sequence: 7, role: "user", content: "手机里我说喜欢红茶", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "phone" } }], hasMore: false, nextBefore: null } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await chatHeading("爱丽丝");
    await user.click(screen.getByRole("button", { name: "角色记忆" }));
    await user.click(within(await screen.findByRole("list", { name: "记忆列表" })).getByRole("button", { name: "定位到消息" }));
    expect(await screen.findByRole("heading", { name: "爱丽丝 · 手机会话" })).toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "页面" });
    expect(within(navigation).getByRole("button", { name: "手机会话" })).toHaveAttribute("aria-current", "page");
    expect(within(navigation).getByRole("button", { name: "对话" })).not.toHaveAttribute("aria-current");
    expect((await screen.findByText("手机里我说喜欢红茶")).closest("li")).toHaveClass("source-highlight");
    expect(screen.queryByText("来源会话与当前角色不匹配")).not.toBeInTheDocument();
  });
});
