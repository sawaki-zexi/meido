import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { fakeBackend, message, role, sse } from "./test/fakeBackend";

const alice = role({ id: "r1", name: "爱丽丝", description: "安静的女仆" });
const bella = role({ id: "r2", name: "贝拉" });
const session = { sessionKey: "role:r1", roleId: "r1", createdAt: "2026-09-30T00:00:00Z", updatedAt: "2026-09-30T00:00:00Z" };
const sessionOf = (id: string) => ({ ...session, sessionKey: `role:${id}`, roleId: id });
const emptySession = () => ({ body: { session, messages: [] } });

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const roleList = () => screen.findByRole("list", { name: "角色列表" });
const chatHeading = (name: string) => screen.findByRole("heading", { level: 1, name });
const dialog = (name: string) => screen.findByRole("dialog", { name });

describe("首页", () => {
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
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "设置" }));
    const panel = await dialog("设置");
    expect(within(panel).getByRole("button", { name: /模型/ })).toBeTruthy();
    expect(await within(panel).findByLabelText("API 地址")).toHaveValue("https://api.openai.com/v1");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("角色管理", () => {
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
    expect(within(preview).getByText("新角色", { selector: "strong" })).toBeTruthy();
    await user.type(within(form).getByLabelText(/角色设定/), "她的设定");
    await user.click(within(form).getByRole("button", { name: "保存" }));

    const reveal = await dialog("新卡牌");
    expect(within(reveal).getByText("新角色", { selector: "strong" })).toBeTruthy();
    expect(within(reveal).getByText("No.001")).toBeTruthy();
    expect(within(await screen.findByRole("list", { name: "角色卡牌" })).getByRole("button", { name: "查看新角色" })).toBeTruthy();
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      name: "新角色", description: "",
      profile: { profile: "她的设定", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" },
    });
    expect(sessionStorage.getItem("meido:create-role-draft")).toBeNull();

    await user.click(within(reveal).getByRole("button", { name: "开始对话" }));
    expect(await chatHeading("新角色")).toBeTruthy();
    expect(within(await roleList()).getByText("新角色")).toBeTruthy();
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

    await user.click(within(cards).getByRole("button", { name: "查看贝拉" }));
    expect(within(await dialog("角色资料")).getByLabelText(/名称/)).toHaveValue("贝拉");
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

  it("查看角色资料为只读，编辑后保存，取消编辑恢复原值", async () => {
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
    expect(name).toBeDisabled();

    await user.click(within(panel).getByRole("button", { name: "编辑" }));
    await user.clear(name);
    await user.type(name, "临时");
    await user.click(within(panel).getByRole("button", { name: "取消" }));
    expect(name).toHaveValue("爱丽丝");
    expect(name).toBeDisabled();

    await user.click(within(panel).getByRole("button", { name: "编辑" }));
    await user.clear(name);
    await user.type(name, "爱丽丝二号");
    await user.click(within(panel).getByRole("button", { name: "保存" }));
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
