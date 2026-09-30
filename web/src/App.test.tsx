import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { fakeBackend, message, role, sse } from "./test/fakeBackend";

const alice = role({ id: "r1", name: "爱丽丝", description: "安静的女仆" });
const session = { sessionKey: "role:r1", roleId: "r1", createdAt: "2026-09-30T00:00:00Z", updatedAt: "2026-09-30T00:00:00Z" };

beforeEach(() => { sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const roleList = () => screen.findByRole("list", { name: "角色列表" });

describe("角色管理", () => {
  it("加载中显示状态，空列表时给出提示", async () => {
    let release!: () => void;
    fakeBackend({ "GET /api/roles": () => new Promise((resolve) => { release = () => resolve({ body: { roles: [] } }); }) });
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("正在加载角色");
    release();
    expect(await screen.findByText("还没有角色。")).toBeTruthy();
  });

  it("后端不可用时显示明确错误", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("无法连接本地后端");
  });

  it("创建角色后出现在列表并显示详情", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/roles": () => ({ status: 201, body: { role: role({ id: "r2", name: "新角色", profile: { ...alice.profile, profile: "她的设定" } }) } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("还没有角色。");
    await user.click(screen.getByRole("button", { name: "创建角色" }));
    await user.type(screen.getByLabelText(/名称/), "新角色");
    await user.type(screen.getByLabelText(/角色设定/), "她的设定");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(await within(await roleList()).findByText("新角色")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "角色详情" })).toBeTruthy();
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      name: "新角色", description: "",
      profile: { profile: "她的设定", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" },
    });
    expect(sessionStorage.getItem("meido:create-role-draft")).toBeNull();
  });

  it("保存失败时保留草稿并显示原因", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "POST /api/roles": () => ({ status: 422, body: { detail: [{ msg: "名称不能为空" }] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("还没有角色。");
    await user.click(screen.getByRole("button", { name: "创建角色" }));
    await user.type(screen.getByLabelText(/名称/), "  ");
    await user.type(screen.getByLabelText(/角色设定/), "设定");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("名称不能为空");
    expect(screen.getByLabelText(/角色设定/)).toHaveValue("设定");
    expect(JSON.parse(sessionStorage.getItem("meido:create-role-draft")!).profile).toBe("设定");
  });

  it("刷新后恢复未保存的创建草稿", async () => {
    sessionStorage.setItem("meido:create-role-draft", JSON.stringify({ name: "草稿", profile: "未完成" }));
    fakeBackend({ "GET /api/roles": () => ({ body: { roles: [] } }) });
    render(<App />);
    expect(screen.getByRole("heading", { name: "创建角色" })).toBeTruthy();
    expect(screen.getByLabelText(/名称/)).toHaveValue("草稿");
  });

  it("查看角色为只读，编辑后保存，取消编辑恢复原值", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "PUT /api/roles/r1": (init) => ({ body: { role: { ...alice, name: JSON.parse(String(init!.body)).name } } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await within(await roleList()).findByRole("button", { name: /^爱丽丝/ }));
    const name = screen.getByLabelText(/名称/);
    expect(name).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "编辑" }));
    await user.clear(name);
    await user.type(name, "临时");
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(name).toHaveValue("爱丽丝");
    expect(name).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "编辑" }));
    await user.clear(name);
    await user.type(name, "爱丽丝二号");
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await within(await roleList()).findByText("爱丽丝二号")).toBeTruthy();
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
  });

  it("确认后删除角色；取消确认则不删除", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "DELETE /api/roles/r1": () => ({ status: 204 }),
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await within(await roleList()).findByRole("button", { name: /^爱丽丝/ }));

    await user.click(screen.getByRole("button", { name: "删除角色" }));
    expect(calls.some((call) => call.method === "DELETE")).toBe(false);

    await user.click(screen.getByRole("button", { name: "删除角色" }));
    expect(await screen.findByText("还没有角色。")).toBeTruthy();
    expect(confirm.mock.calls[1][0]).toContain("爱丽丝");
  });
});

describe("唯一会话", () => {
  it("进入会话显示历史；空会话提示发送第一条消息", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "进入“爱丽丝”的会话" }));
    expect(await screen.findByText("发送第一条消息开始对话。")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1, name: "爱丽丝" })).toBeTruthy();
    expect(screen.getByText(/role:r1/)).toBeTruthy();
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
    await user.click(await screen.findByRole("button", { name: "进入“爱丽丝”的会话" }));
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
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "进入“爱丽丝”的会话" }));
    const input = await screen.findByLabelText("消息内容");
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
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
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
    await user.click(await screen.findByRole("button", { name: "进入“爱丽丝”的会话" }));
    const input = await screen.findByLabelText("消息内容");
    await user.type(input, "你好{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("模型服务超时");
    expect(screen.getByText("未完成")).toBeTruthy();
    expect(screen.getByText("半句")).toBeTruthy();

    await waitFor(() => expect(input).not.toBeDisabled());
    await user.type(input, "再试{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("该角色正在生成回复");

    // Chat errors belong to the chat and are not carried back to the role page.
    await user.click(screen.getByRole("button", { name: "← 返回角色" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("打开会话失败时停留在角色页并显示错误", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ status: 404, body: { detail: "角色不存在" } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "进入“爱丽丝”的会话" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("角色不存在");
    expect(screen.getByRole("button", { name: "创建角色" })).toBeTruthy();
  });

  it("返回角色页后仍能看到刚才的角色", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "进入“爱丽丝”的会话" }));
    await user.click(await screen.findByRole("button", { name: "← 返回角色" }));
    expect(screen.getByRole("heading", { name: "角色详情" })).toBeTruthy();
    expect(screen.getByLabelText(/名称/)).toHaveValue("爱丽丝");
  });
});
