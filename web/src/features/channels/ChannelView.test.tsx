import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChannelView } from "./ChannelView";
import { fakeBackend, role } from "../../test/fakeBackend";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const makeAccount = (channel: string, accountId: string, overrides: Record<string, unknown> = {}) => ({
  accountId,
  roleId: "r1",
  channel,
  enabled: false,
  allowPrivate: true,
  allowGroup: channel === "qq" || channel === "telegram",
  blockedSenders: [],
  displayName: "",
  platformId: channel === "qqbot" || channel === "feishu" ? `${channel}-app` : null,
  lastError: null,
  tokenConfigured: false,
  ...overrides,
});

function setupBackend(accounts: ReturnType<typeof makeAccount>[] = []) {
  return fakeBackend({
    "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts } }),
    "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [] } }),
  });
}

describe("手机账号应用导航", () => {
  it.each(["failed", "uncertain"])("投递 %s 不被当成生成失败，缺少投递字段的摘要使用中性提醒", async (deliveryStatus) => {
    const message = { id: "delivery", sequence: 1, role: "assistant", content: "已经生成的回复", status: "failed", createdAt: "2026-10-25T06:00:00Z", metadata: { threadId: "chat", deliveryStatus, error: "NetworkTimeout" } };
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [makeAccount("qq", "qq-1", { enabled: true, connectionStatus: "connected" })] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "chat", accountId: "qq-1", channel: "qq", chatId: "42", chatType: "private", title: "投递联系人", lastActivity: message.createdAt, latestMessage: message }] } }),
      "GET .*threads/chat/messages.*": () => ({ body: { messages: [message], hasMore: false } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "QQ" }));
    await user.click(screen.getByRole("button", { name: /投递联系人/ }));
    const timeline = within(screen.getByRole("region", { name: "会话消息" }));
    expect(await timeline.findByText("已经生成的回复")).toBeVisible();
    expect(timeline.getByText(deliveryStatus === "failed" ? "发送失败" : "发送结果不确定")).toBeVisible();
    expect(timeline.queryByText("回复失败")).not.toBeInTheDocument();
    expect(timeline.queryByText("回复未能完成")).not.toBeInTheDocument();
    expect(timeline.queryByText("NetworkTimeout")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "返回会话列表" }));
    expect(screen.getByRole("button", { name: /投递联系人/ })).toHaveTextContent("回复需关注");
  });

  it("实时失败回复立即显示原因，部分生成失败也不被投递状态掩盖", async () => {
    const sources: EventTarget[] = [];
    class Source extends EventTarget {
      constructor(_url: string) { super(); sources.push(this); }
      close() {}
    }
    vi.stubGlobal("EventSource", Source);
    const thread = { threadId: "live", accountId: "qq-1", channel: "qq", chatId: "42", chatType: "private", title: "状态联系人", lastActivity: "2026-10-25T06:00:00Z" };
    const previous = { id: "previous", sequence: 1, role: "user", content: "旧消息", status: "completed", createdAt: thread.lastActivity, metadata: { threadId: "live" } };
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [makeAccount("qq", "qq-1", { enabled: true, connectionStatus: "connected" })] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ ...thread, latestMessage: previous }] } }),
      "GET .*threads/live/messages.*": () => ({ body: { messages: [previous], hasMore: false } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "QQ" }));
    await user.click(screen.getByRole("button", { name: /状态联系人/ }));
    const timeline = within(screen.getByRole("region", { name: "会话消息" }));
    expect(await timeline.findByText("旧消息")).toBeVisible();
    await act(async () => sources[0].dispatchEvent(new MessageEvent("channel_update", { data: JSON.stringify({ type: "message", roleId: "r1", thread, message: { id: "failed-live", sequence: 2, role: "assistant", content: "", status: "failed", createdAt: "2026-10-25T06:01:00Z", metadata: { threadId: "live", error: "模型暂时不可用" } } }) })));
    expect(await timeline.findByText("模型暂时不可用")).toBeVisible();
    expect(timeline.getByText("回复失败")).toBeVisible();
    expect(timeline.queryByText("[非文本消息]")).not.toBeInTheDocument();
    await act(async () => sources[0].dispatchEvent(new MessageEvent("channel_update", { data: JSON.stringify({ type: "message", roleId: "r1", thread, message: { id: "partial-live", sequence: 3, role: "assistant", content: "只生成了一半", status: "failed", createdAt: "2026-10-25T06:02:00Z", metadata: { threadId: "live", runtimeReason: "failed", error: "模型连接中断", deliveryStatus: "uncertain" } } }) })));
    expect(await timeline.findByText("只生成了一半")).toBeVisible();
    expect(timeline.getByText("模型连接中断")).toBeVisible();
    expect(timeline.getByText("发送结果不确定")).toBeVisible();
    expect(timeline.getAllByText("回复失败")).toHaveLength(2);
  });

  it("按 Shiori 分组时间、折叠引用和展示媒体，保留不确定投递状态", async () => {
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [makeAccount("qq", "qq-1", { enabled: true, connectionStatus: "connected" })] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "chat", accountId: "qq-1", channel: "qq", chatId: "42", chatType: "private", title: "媒体联系人", lastActivity: "2026-10-25T06:10:01Z", latestMessage: { role: "assistant", content: "**稍后整理** [文档](https://example.test)", createdAt: "2026-10-25T06:10:01Z" } }] } }),
      "GET .*threads/chat/messages.*": () => ({ body: { messages: [
        { id: "text", sequence: 1, role: "user", content: "请帮我整理资料", createdAt: "2026-10-25T06:00:00Z", metadata: { threadId: "chat", senderId: "owner", senderName: "阿宁", senderIsOwner: true } },
        { id: "media", sequence: 2, role: "user", content: "", createdAt: "2026-10-25T06:05:00Z", metadata: { threadId: "chat", senderId: "owner", senderName: "阿宁", attachments: [{ id: "image", name: "示意图", kind: "image" }, { id: "file", name: "资料.pdf", kind: "file" }] } },
        { id: "quote", sequence: 3, role: "assistant", content: "稍后整理", createdAt: "2026-10-25T06:10:01Z", metadata: { threadId: "chat", replyTo: "older-quote", replyToSenderName: "阿宁", replyToContent: "这是一段足够长的引用内容，需要默认折叠到两行。展开后仍然可以看到完整的原文，再点击收起可以恢复简洁的聊天界面。", mentionedIds: ["owner"], deliveryStatus: "uncertain" } },
      ], hasMore: false } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "QQ" }));
    const row = screen.getByRole("button", { name: /媒体联系人/ });
    expect(within(row).getByText("稍后整理 文档")).toBeVisible();
    await user.click(row);
    const messageRegion = screen.getByRole("region", { name: "会话消息" });
    const messages = within(messageRegion);
    expect(await messages.findByText("这是我")).toBeVisible();
    expect(messages.getByText("@阿宁")).toBeVisible();
    expect(messages.queryByText("（无内容）")).not.toBeInTheDocument();
    expect(messages.getByRole("link", { name: "资料.pdf" })).toHaveAttribute("href", "/api/roles/r1/attachments/file");
    expect(messageRegion.querySelectorAll("time")).toHaveLength(2);
    expect(messages.getByText("发送结果不确定")).toBeVisible();
    expect(messages.queryByText("已发送")).not.toBeInTheDocument();
    await user.click(messages.getByRole("button", { name: "展开引用" }));
    expect(messages.getByRole("button", { name: "收起引用" })).toBeVisible();
    await user.click(messages.getByRole("img", { name: "示意图" }));
    expect(screen.getByRole("dialog", { name: "图片预览" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "图片预览" })).not.toBeInTheDocument();
    expect(messageRegion).toBeVisible();
  });

  it("手机消息将纯提及和失败回复显示为真实内容与状态，预览不再写无内容", async () => {
    const source = { platform: "qq", platformAccountId: "bot-id", accountId: "qq-1", displayName: "角色 QQ", avatarUrl: "/bot.png" };
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [makeAccount("qq", "qq-1", { enabled: true, connectionStatus: "connected", platformId: "bot-id", displayName: "角色 QQ" })] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "chat", accountId: "qq-1", channel: "qq", chatId: "42", chatType: "private", title: "联系人", lastActivity: "2026-10-25T06:58:00Z", latestMessage: { role: "assistant", content: "", status: "failed", createdAt: "2026-10-25T06:58:00Z" } }] } }),
      "GET .*threads/chat/messages.*": () => ({ body: { messages: [
        { id: "at-only", sequence: 1, role: "user", content: "", status: "completed", createdAt: "2026-10-25T06:58:00Z", metadata: { threadId: "chat", senderId: "visitor", senderName: "联系人", senderAvatarUrl: "/visitor.png", mentionedIds: ["bot-id"], viaAccount: source, platformMessageId: "p1" } },
        { id: "failed", sequence: 2, role: "assistant", content: "", status: "failed", createdAt: "2026-10-25T06:58:00Z", metadata: { threadId: "chat", viaAccount: source, error: "尚未设置模型连接" } },
        { id: "failed-without-detail", sequence: 3, role: "assistant", content: "", status: "failed", createdAt: "2026-10-25T06:58:00Z", metadata: { threadId: "chat" } },
      ], hasMore: false } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "QQ" }));
    const row = screen.getByRole("button", { name: /联系人/ });
    expect(within(row).getByText("回复需关注")).toBeVisible();
    await user.click(row);
    const messages = within(screen.getByRole("region", { name: "会话消息" }));
    expect(await messages.findByText("@角色 QQ")).toBeVisible();
    expect(messages.getAllByText("回复失败")).toHaveLength(2);
    expect(messages.getByText("回复未能完成")).toBeVisible();
    expect(messages.getByText("尚未设置模型连接")).toBeVisible();
    expect(messages.queryByText("（无内容）")).not.toBeInTheDocument();
    expect(messages.queryByAltText("经由账号头像")).not.toBeInTheDocument();
    await user.click(messages.getAllByLabelText(/消息详情/)[0]);
    expect(messages.getByText("消息编号：p1")).toBeVisible();
  });

  it("首次读取失败后关闭提示仍能重新读取手机应用", async () => {
    let attempts = 0;
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ++attempts === 1
        ? { status: 503, body: { detail: "账号目录暂不可用" } }
        : { body: { accounts: [makeAccount("qq", "qq-1", { enabled: true, connectionStatus: "connected" })] } },
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [] } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("账号目录暂不可用");
    await user.click(screen.getByRole("button", { name: "关闭提示" }));
    await user.click(screen.getByRole("button", { name: "重新读取" }));
    expect(await screen.findByRole("button", { name: "QQ" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("已连接且没有会话时显示 QQ 应用，打开后才显示单页空状态", async () => {
    setupBackend([makeAccount("qq", "qq-1", { enabled: true, connectionStatus: "connected" })]);
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
    const app = await screen.findByRole("button", { name: "QQ" });
    expect(screen.getByRole("button", { name: "设置渠道" })).toBeVisible();
    expect(screen.queryByText("选择一个会话")).not.toBeInTheDocument();
    expect(screen.queryByText("暂无渠道会话")).not.toBeInTheDocument();
    await user.click(app);
    expect(await screen.findByText("还没有会话")).toBeVisible();
    expect(screen.queryByRole("region", { name: "会话消息" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "返回手机主页" }));
    expect(screen.getByRole("button", { name: "QQ" })).toBeVisible();
  });

  it("按账号隔离会话，离线账号和已移除账号的历史仍能逐页读取", async () => {
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [
        makeAccount("qq", "qq-new", { enabled: true, connectionStatus: "connected" }),
        makeAccount("telegram", "tg-offline"),
      ] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [
        { threadId: "qq-chat", accountId: "qq-new", channel: "qq", chatId: "1", chatType: "private", title: "QQ 联系人", lastActivity: "2026-10-25T00:00:00Z" },
        { threadId: "tg-chat", accountId: "tg-offline", channel: "telegram", chatId: "2", chatType: "private", title: "Telegram 联系人", lastActivity: "2026-10-25T00:00:00Z" },
        { threadId: "old-chat", accountId: "qq-old", channel: "qq", chatId: "3", chatType: "private", title: "旧 QQ 联系人", accountRemoved: true, viaAccount: { accountId: "qq-old", displayName: "旧账号" }, lastActivity: "2026-10-25T00:00:00Z" },
      ] } }),
      "GET .*threads/qq-chat/messages.*": () => ({ body: { messages: [{ id: "m1", sequence: 1, role: "user", content: "当前账号消息", createdAt: "2026-10-25T00:00:00Z", metadata: { threadId: "qq-chat" } }], hasMore: false } }),
      "GET .*threads/old-chat/messages.*": () => ({ body: { messages: [{ id: "old1", sequence: 1, role: "user", content: "保留的旧账号消息", createdAt: "2026-10-25T00:00:00Z", metadata: { threadId: "old-chat" } }], hasMore: false } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "QQ" }));
    let list = within(screen.getByRole("complementary", { name: "渠道会话列表" }));
    expect(list.getByRole("button", { name: /QQ 联系人/ })).toBeVisible();
    expect(list.queryByRole("button", { name: /旧 QQ|Telegram/ })).not.toBeInTheDocument();
    await user.click(list.getByRole("button", { name: /QQ 联系人/ }));
    expect(await screen.findByText("当前账号消息")).toBeVisible();
    expect(screen.queryByRole("complementary", { name: "渠道会话列表" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "返回会话列表" }));
    await user.click(screen.getByRole("button", { name: "返回手机主页" }));
    const offline = screen.getByRole("button", { name: "Telegram，离线" });
    expect(offline).toBeEnabled();
    await user.click(offline);
    list = within(screen.getByRole("complementary", { name: "渠道会话列表" }));
    expect(list.getByRole("button", { name: /Telegram 联系人/ })).toBeVisible();
    expect(list.queryByRole("button", { name: /QQ 联系人/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "返回手机主页" }));
    await user.click(screen.getByRole("button", { name: /QQ 历史/ }));
    await user.click(screen.getByRole("button", { name: /旧 QQ 联系人/ }));
    expect(await screen.findByText("保留的旧账号消息")).toBeVisible();
  });

  it("打开空账号列表期间收到首条消息后显示会话，仍由用户点击进入聊天", async () => {
    const sources: EventTarget[] = [];
    class Source extends EventTarget {
      constructor(_url: string) { super(); sources.push(this); }
      close() {}
    }
    vi.stubGlobal("EventSource", Source);
    const thread = { threadId: "live", accountId: "qq-1", channel: "qq", chatId: "42", chatType: "private", title: "新联系人", lastActivity: "2026-10-25T00:00:00Z" };
    const message = { id: "live1", sequence: 1, role: "user", content: "实时到达的消息", createdAt: thread.lastActivity, metadata: { threadId: "live" } };
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [makeAccount("qq", "qq-1", { enabled: true, connectionStatus: "connected" })] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [] } }),
      "GET .*threads/live/messages.*": () => ({ body: { messages: [message], hasMore: false } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "QQ" }));
    expect(screen.getByText("还没有会话")).toBeVisible();
    await act(async () => sources[0].dispatchEvent(new MessageEvent("channel_update", { data: JSON.stringify({ type: "message", roleId: "r1", thread, message }) })));
    const row = await screen.findByRole("button", { name: /新联系人/ });
    expect(screen.queryByRole("region", { name: "会话消息" })).not.toBeInTheDocument();
    expect(screen.queryByText("还没有会话")).not.toBeInTheDocument();
    await user.click(row);
    expect(await within(screen.getByRole("region", { name: "会话消息" })).findByText("实时到达的消息")).toBeVisible();
  });
});

describe("渠道记忆来源只读查看", () => {
  const sourceRole = role({ id: "r1", name: "Elysia" });
  const thread = { threadId: "source", sessionKey: "thread:source", channel: "telegram", chatId: "42", chatType: "private", title: "来源私聊", lastActivity: "2026-10-14T00:00:00Z" };

  it("直接验证角色所属线程并分页定位来源，不加载账号或线程目录，不显示管理控件", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=1&networkOnly=true": () => ({ body: { thread } }),
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=100&networkOnly=true": () => ({ body: { messages: [], hasMore: true, nextBefore: 51 } }),
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=100&networkOnly=true&before=51": () => ({ body: { messages: [{ id: "origin", sequence: 7, role: "user", content: "分页找到的原文", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "source" } }], hasMore: false, nextBefore: null } }),
    });
    render(<ChannelView role={sourceRole} sourceOnly onBack={() => undefined} initialSource={{ origin: { kind: "turn", sessionKey: "thread:source", messageIds: ["origin"], messageRange: null, stableSourceKey: "turn:source" } }} />);
    expect((await screen.findByText("分页找到的原文")).closest("li")).toHaveClass("source-highlight");
    expect(calls.map((call) => call.path)).toEqual([
      "/api/roles/r1/channels/threads/source/messages?limit=1&networkOnly=true",
      "/api/roles/r1/channels/threads/source/messages?limit=100&networkOnly=true",
      "/api/roles/r1/channels/threads/source/messages?limit=100&networkOnly=true&before=51",
    ]);
    expect(calls.every((call) => call.method === "GET")).toBe(true);
    expect(screen.queryByRole("complementary", { name: "渠道会话列表" })).not.toBeInTheDocument();
    expect(screen.queryByText("会话设置")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "角色账号" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /手机会话/ })).not.toBeInTheDocument();
  });

  it("群旁听来源从独立记录分页定位原文，只读展示且不挂载群管理面板", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=1&networkOnly=true": () => ({ body: { thread: { ...thread, channel: "qq", chatType: "group" } } }),
      "GET /api/roles/r1/channels/threads/source/listening/messages\\?limit=100": () => ({ body: { messages: [], hasMore: true, nextBefore: 20 } }),
      "GET /api/roles/r1/channels/threads/source/listening/messages\\?limit=100&before=20": () => ({ body: { messages: [{ id: "heard", sequence: 3, role: "user", content: "旁听记忆的原始发言", createdAt: "2026-10-14T00:00:00Z", metadata: {} }], hasMore: false, nextBefore: null } }),
    });
    render(<ChannelView role={sourceRole} sourceOnly onBack={() => undefined} initialSource={{ origin: { kind: "listening", sessionKey: "thread:source", messageIds: ["heard"], messageRange: null, stableSourceKey: "listening:source" } }} />);
    const source = await screen.findByRole("region", { name: "旁听记忆来源" });
    expect(within(source).getByText("旁听记忆的原始发言")).toBeVisible();
    expect(source).toHaveClass("source-highlight");
    expect(screen.queryByText(/^群旁听记录/, { selector: "summary" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(calls).toHaveLength(3);
    expect(calls.every((call) => call.method === "GET")).toBe(true);
  });

  it("线程归属验证失败时不继续读取消息或旁听原文", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/threads/foreign/messages\\?limit=1&networkOnly=true": () => ({ status: 404, body: { detail: "此线程不属于当前角色" } }),
    });
    render(<ChannelView role={sourceRole} sourceOnly onBack={() => undefined} initialSource={{ origin: { kind: "listening", sessionKey: "thread:foreign", messageIds: ["secret"], messageRange: null, stableSourceKey: "foreign" } }} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("来源渠道会话当前不可访问");
    expect(calls).toHaveLength(1);
  });

  it("切换来源角色后立即清空旧原文，归属拒绝时不保留之前角色的消息", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=1&networkOnly=true": () => ({ body: { thread } }),
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=100&networkOnly=true": () => ({ body: { messages: [{ id: "origin", sequence: 7, role: "user", content: "第一个角色的来源原文", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "source" } }], hasMore: false, nextBefore: null } }),
      "GET /api/roles/r2/channels/threads/source/messages\\?limit=1&networkOnly=true": () => ({ status: 404, body: { detail: "来源不存在" } }),
    });
    const initialSource = { origin: { kind: "turn", sessionKey: "thread:source", messageIds: ["origin"], messageRange: null, stableSourceKey: "turn:source" } };
    const view = render(<ChannelView role={sourceRole} sourceOnly onBack={() => undefined} initialSource={initialSource} />);
    await screen.findByText("第一个角色的来源原文");
    view.rerender(<ChannelView role={role({ id: "r2", name: "另一角色" })} sourceOnly onBack={() => undefined} initialSource={initialSource} />);
    expect(screen.queryByText("第一个角色的来源原文")).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("来源渠道会话当前不可访问");
    expect(calls.filter((call) => call.path.includes("/r2/"))).toHaveLength(1);
  });
});

describe("渠道会话查看", () => {
  it("从手机设置返回长会话后保留阅读位置", async () => {
    const messages = Array.from({ length: 50 }, (_, index) => ({
      id: `m-${index}`, sequence: index + 1, role: "user", content: `消息 ${index + 1}`,
      createdAt: "2026-10-24T00:00:00Z", metadata: { threadId: "t1" },
    }));
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [
        { threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "长会话", lastActivity: "2026-10-24T00:00:00Z" },
      ] } }),
      "GET /api/roles/r1/channels/threads/t1/messages\\?limit=50&networkOnly=true": () => ({ body: { messages, hasMore: false, nextBefore: null } }),
      "GET /api/channels/settings": () => ({ body: { channels: [] } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /Telegram 历史/ }));
    await user.click(await screen.findByRole("button", { name: /长会话/ }));
    await screen.findByText("消息 50");
    const list = within(screen.getByRole("region", { name: "会话消息" })).getByRole("list");
    Object.defineProperties(list, { clientHeight: { value: 200 }, scrollHeight: { value: 2000 } });
    list.scrollTop = 400;
    fireEvent.scroll(list);
    await user.click(screen.getByRole("button", { name: "返回会话列表" }));
    await user.click(screen.getByRole("button", { name: "返回手机主页" }));
    await user.click(screen.getByRole("button", { name: "设置渠道" }));
    await user.click(await screen.findByRole("button", { name: "返回多端会话" }));
    await user.click(await screen.findByRole("button", { name: /Telegram 历史/ }));
    await user.click(screen.getByRole("button", { name: /长会话/ }));
    expect(within(screen.getByRole("region", { name: "会话消息" })).getByRole("list").scrollTop).toBe(400);
  });

  it("迟到的首屏目录和消息页不能覆盖实时线程及同 ID 消息", async () => {
    const sources: EventTarget[] = [];
    class Source extends EventTarget {
      constructor(_url: string) { super(); sources.push(this); }
      close() {}
    }
    vi.stubGlobal("EventSource", Source);
    let releaseList!: (value: { body: unknown }) => void;
    let releasePage!: (value: { body: unknown }) => void;
    const pendingList = new Promise<{ body: unknown }>((resolve) => { releaseList = resolve; });
    const pendingPage = new Promise<{ body: unknown }>((resolve) => { releasePage = resolve; });
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads[?]networkOnly=true": () => pendingList,
      "GET .*threads/live/messages[?]limit=50&networkOnly=true": () => pendingPage,
    });
    const liveThread = { threadId: "live", sessionKey: "thread:live", channel: "telegram", chatId: "42", chatType: "private", title: "实时线程", lastActivity: "2026-10-23T00:02:00Z" };
    const liveMessage = { id: "live-id", sequence: 1, role: "assistant", content: "实时完成消息", createdAt: liveThread.lastActivity, metadata: { threadId: "live", deliveryStatus: "sent" } };
    const emit = () => sources[0].dispatchEvent(new MessageEvent("channel_update", { data: JSON.stringify({ type: "message", roleId: "r1", thread: liveThread, message: liveMessage }) }));
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await act(async () => { emit(); });
    await act(async () => { releaseList({ body: { threads: [] } }); await pendingList; });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /实时线程/ }));
    await act(async () => { emit(); });
    await act(async () => { releasePage({ body: { messages: [{ ...liveMessage, content: "迟到的旧消息", metadata: { threadId: "live", deliveryStatus: "pending" } }], hasMore: false, nextBefore: null } }); await pendingPage; });
    const timeline = within(screen.getByRole("region", { name: "会话消息" }));
    expect(await timeline.findByText("实时完成消息")).toBeVisible();
    expect(timeline.queryByText("迟到的旧消息")).not.toBeInTheDocument();
    expect(timeline.getByText("已发送")).toBeVisible();
  });

  it("目录首屏期间收到旁听事件仍保留完整群线程", async () => {
    const sources: EventTarget[] = [];
    class Source extends EventTarget {
      constructor(_url: string) { super(); sources.push(this); }
      close() {}
    }
    vi.stubGlobal("EventSource", Source);
    let releaseList!: (value: { body: unknown }) => void;
    const pendingList = new Promise<{ body: unknown }>((resolve) => { releaseList = resolve; });
    const heard = { id: "heard-live", sequence: 4, role: "user", content: "旁听实时消息", createdAt: "2026-10-23T00:04:00Z", metadata: { threadId: "group", listening: true } };
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads[?]networkOnly=true": () => pendingList,
      "GET .*threads/group/messages[?]limit=50&networkOnly=true": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
      "GET .*threads/group/listening/messages[?]limit=50": () => ({ body: { messages: [heard], hasMore: false, nextBefore: null } }),
      "GET .*threads/group/listening": () => ({ body: { enabled: true, dailyCap: 200, defaultDailyCap: 200, toggles: [] } }),
    });
    const group = { threadId: "group", sessionKey: "thread:group", channel: "qq", chatId: "42", chatType: "group", title: "实时群", lastActivity: heard.createdAt, latestMessage: { content: "触发消息", role: "user", createdAt: heard.createdAt } };
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await act(async () => {
      sources[0].dispatchEvent(new MessageEvent("channel_update", { data: JSON.stringify({ type: "listening_heard", roleId: "r1", threadId: "group", thread: group, message: heard }) }));
    });
    await act(async () => { releaseList({ body: { threads: [] } }); await pendingList; });
    const thread = await screen.findByRole("button", { name: /实时群/ });
    expect(thread).toBeVisible();
    await userEvent.setup().click(thread);
    expect(await within(screen.getByRole("region", { name: "会话消息" })).findByText("旁听实时消息")).toBeVisible();
  });

  it("刷新会话时恢复断线期间遗漏的群旁听记录", async () => {
    const sources: EventTarget[] = [];
    class Source extends EventTarget {
      constructor(_url: string) { super(); sources.push(this); }
      close() {}
    }
    vi.stubGlobal("EventSource", Source);
    let listeningPageCount = 0;
    const heard = { id: "heard-first", sequence: 1, role: "user", content: "已收到的旁听", createdAt: "2026-10-23T00:01:00Z", metadata: { threadId: "group" } };
    const missed = { id: "heard-missed", sequence: 2, role: "user", content: "断线期间的旁听", createdAt: "2026-10-23T00:02:00Z", metadata: { threadId: "group" } };
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "group", sessionKey: "thread:group", channel: "qq", chatId: "42", chatType: "group", title: "断线群",
        lastActivity: "2026-10-23T00:02:00Z", latestMessage: { content: "普通消息", role: "user", createdAt: "2026-10-23T00:02:00Z" },
      }] } }),
      "GET .*threads/group/messages\\?limit=50&networkOnly=true": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
      "GET .*threads/group/listening/messages\\?limit=50": () => {
        listeningPageCount += 1;
        return { body: { messages: listeningPageCount === 1 ? [heard] : [heard, missed], hasMore: false, nextBefore: null } };
      },
      "GET .*threads/group/listening": () => ({ body: { enabled: true, dailyCap: 200, defaultDailyCap: 200, toggles: [] } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /断线群/ }));
    const timeline = within(screen.getByRole("region", { name: "会话消息" }));
    expect(await timeline.findByText("已收到的旁听")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "刷新会话" }));
    expect(await timeline.findByText("断线期间的旁听")).toBeVisible();
    expect(sources.length).toBeGreaterThan(0);
  });

  it("旁听清理不会使尚未返回的普通消息首屏失效", async () => {
    const sources: EventTarget[] = [];
    class Source extends EventTarget {
      constructor(_url: string) { super(); sources.push(this); }
      close() {}
    }
    vi.stubGlobal("EventSource", Source);
    let releaseMessages!: (reply: { body: unknown }) => void;
    const pendingMessages = new Promise<{ body: unknown }>((resolve) => { releaseMessages = resolve; });
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "clear-race", sessionKey: "thread:clear-race", channel: "qq", chatId: "42", chatType: "group", title: "清理竞态", lastActivity: "2026-10-23T00:01:00Z",
        latestMessage: { content: "触发消息", role: "user", createdAt: "2026-10-23T00:01:00Z" },
      }] } }),
      "GET .*threads/clear-race/messages\\?limit=50&networkOnly=true": () => pendingMessages,
      "GET .*threads/clear-race/listening/messages\\?limit=50": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
      "GET .*threads/clear-race/listening": () => ({ body: { enabled: true, dailyCap: 200, defaultDailyCap: 200, toggles: [] } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /清理竞态/ }));
    await act(async () => {
      sources[0].dispatchEvent(new MessageEvent("channel_update", { data: JSON.stringify({ type: "listening_cleared", roleId: "r1", threadId: "clear-race" }) }));
      await Promise.resolve();
    });
    await act(async () => {
      releaseMessages({ body: { messages: [{ id: "normal", sequence: 1, role: "user", content: "清理前普通消息", createdAt: "2026-10-23T00:01:00Z", metadata: { threadId: "clear-race" } }], hasMore: false, nextBefore: null } });
      await pendingMessages;
    });
    expect(await screen.findByText("清理前普通消息")).toBeVisible();
  });

  it.each(["目录读取中", "消息读取中"])("刷新%s遇到旁听清理时，普通消息仍可读且迟到的旁听不会恢复", async (phase) => {
    const sources: EventTarget[] = [];
    class Source extends EventTarget {
      constructor(_url: string) { super(); sources.push(this); }
      close() {}
    }
    vi.stubGlobal("EventSource", Source);
    let releaseInitial!: (reply: { body: unknown }) => void;
    let releaseThreads!: (reply: { body: unknown }) => void;
    let releaseMessages!: (reply: { body: unknown }) => void;
    let releaseListening!: (reply: { body: unknown }) => void;
    const initialPage = new Promise<{ body: unknown }>((resolve) => { releaseInitial = resolve; });
    const refreshedThreads = new Promise<{ body: unknown }>((resolve) => { releaseThreads = resolve; });
    const refreshedMessages = new Promise<{ body: unknown }>((resolve) => { releaseMessages = resolve; });
    const refreshedListening = new Promise<{ body: unknown }>((resolve) => { releaseListening = resolve; });
    const threads = [{
      threadId: "refresh-clear", sessionKey: "thread:refresh-clear", channel: "qq", chatId: "42", chatType: "group", title: "刷新清理竞态", lastActivity: "2026-10-23T00:01:00Z",
      latestMessage: { content: "触发消息", role: "user", createdAt: "2026-10-23T00:01:00Z" },
    }];
    const message = (id: string, content: string) => ({ id, sequence: 1, role: "user", content, createdAt: "2026-10-23T00:01:00Z", metadata: { threadId: "refresh-clear" } });
    let threadPageCount = 0;
    let messagePageCount = 0;
    let listeningPageCount = 0;
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ++threadPageCount === 1 ? { body: { threads } } : refreshedThreads,
      "GET .*threads/refresh-clear/messages\\?limit=50&networkOnly=true": () => ++messagePageCount === 1 ? initialPage : refreshedMessages,
      "GET .*threads/refresh-clear/listening/messages\\?limit=50": () => ++listeningPageCount === 1 ? { body: { messages: [], hasMore: false, nextBefore: null } } : refreshedListening,
      "GET .*threads/refresh-clear/listening": () => ({ body: { enabled: true, dailyCap: 200, defaultDailyCap: 200, toggles: [] } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /刷新清理竞态/ }));
    await user.click(screen.getByRole("button", { name: "刷新会话" }));
    if (phase === "消息读取中") {
      await act(async () => { releaseThreads({ body: { threads } }); await refreshedThreads; });
      await waitFor(() => expect(calls.filter((call) => call.path.endsWith("refresh-clear/messages?limit=50&networkOnly=true"))).toHaveLength(2));
    }
    await act(async () => {
      sources[0].dispatchEvent(new MessageEvent("channel_update", { data: JSON.stringify({ type: "listening_cleared", roleId: "r1", threadId: "refresh-clear" }) }));
    });
    if (phase === "目录读取中") {
      await act(async () => { releaseThreads({ body: { threads } }); await refreshedThreads; });
    }
    await act(async () => {
      releaseMessages({ body: { messages: [message("fresh", "刷新后普通消息")], hasMore: false, nextBefore: null } });
      await refreshedMessages;
    });
    expect(await screen.findByText("刷新后普通消息")).toBeVisible();
    await act(async () => {
      releaseListening({ body: { messages: [message("heard", "已清理的旁听消息")], hasMore: false, nextBefore: null } });
      releaseInitial({ body: { messages: [message("stale", "迟到的旧首屏消息")], hasMore: false, nextBefore: null } });
      await Promise.all([refreshedListening, initialPage]);
    });
    expect(screen.queryByText("已清理的旁听消息")).not.toBeInTheDocument();
    expect(screen.queryByText("迟到的旧首屏消息")).not.toBeInTheDocument();
    expect(screen.getByText("刷新后普通消息")).toBeVisible();
  });

  it("刷新返回后忽略迟到的首屏消息响应", async () => {
    let releaseInitial!: (reply: { body: unknown }) => void;
    const initialPage = new Promise<{ body: unknown }>((resolve) => { releaseInitial = resolve; });
    let messagePageCount = 0;
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "refresh-race", sessionKey: "thread:refresh-race", channel: "telegram", chatId: "42", chatType: "private", title: "刷新竞态", lastActivity: "2026-10-23T00:02:00Z",
        latestMessage: { content: "最新摘要", role: "user", createdAt: "2026-10-23T00:02:00Z" },
      }] } }),
      "GET .*threads/refresh-race/messages\\?limit=50&networkOnly=true": () => {
        messagePageCount += 1;
        if (messagePageCount === 1) return initialPage;
        return { body: { messages: [{ id: "fresh", sequence: 2, role: "user", content: "刷新后的消息", createdAt: "2026-10-23T00:02:00Z", metadata: { threadId: "refresh-race" } }], hasMore: false, nextBefore: null } };
      },
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /刷新竞态/ }));
    await user.click(screen.getByRole("button", { name: "刷新会话" }));
    expect(await screen.findByText("刷新后的消息")).toBeVisible();
    await act(async () => {
      releaseInitial({ body: { messages: [{ id: "stale", sequence: 1, role: "user", content: "迟到的旧消息", createdAt: "2026-10-23T00:01:00Z", metadata: { threadId: "refresh-race" } }], hasMore: false, nextBefore: null } });
      await initialPage;
    });
    expect(screen.queryByText("迟到的旧消息")).not.toBeInTheDocument();
    expect(screen.getByText("刷新后的消息")).toBeVisible();
  });

  it("刷新会话保留断线缺口的分页边界", async () => {
    let messagePageCount = 0;
    const message = (id: string, sequence: number, content: string) => ({ id, sequence, role: "user", content, createdAt: new Date(Date.UTC(2026, 9, 23, 0, sequence)).toISOString(), metadata: { threadId: "refresh-gap" } });
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "refresh-gap", sessionKey: "thread:refresh-gap", channel: "telegram", chatId: "42", chatType: "private", title: "刷新缺口", lastActivity: "2026-10-23T00:02:00Z",
        latestMessage: { content: "最新", role: "user", createdAt: "2026-10-23T00:02:00Z" },
      }] } }),
      "GET .*threads/refresh-gap/messages\\?limit=50&networkOnly=true": () => {
        messagePageCount += 1;
        return { body: { messages: [message(messagePageCount === 1 ? "old-page" : "new-page", messagePageCount === 1 ? 50 : 101, messagePageCount === 1 ? "断线前消息" : "刷新后的最新消息")], hasMore: messagePageCount > 1, nextBefore: messagePageCount > 1 ? 101 : null } };
      },
      "GET .*threads/refresh-gap/messages\\?limit=50&networkOnly=true&before=101": () => ({ body: { messages: [message("gap-page", 100, "缺口中的消息")], hasMore: false, nextBefore: null } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /刷新缺口/ }));
    await screen.findByText("断线前消息");
    await user.click(screen.getByRole("button", { name: "刷新会话" }));
    await waitFor(() => expect(calls.filter((call) => call.path.includes("refresh-gap/messages")).length).toBe(2));
    expect(await screen.findByText("刷新后的最新消息")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "读取更早消息" }));
    expect(await screen.findByText("缺口中的消息")).toBeVisible();
  });

  it("合并时间线只显示两条流均完整的时间范围，并先补齐较新的分页边界", async () => {
    const item = (id: string, sequence: number, minute: number) => ({
      id, sequence, role: "user", content: id, createdAt: "2026-10-23T00:0" + minute + ":00Z", metadata: { threadId: "group" },
    });
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "group", sessionKey: "thread:group", channel: "qq", chatId: "42", chatType: "group", title: "分页群", lastActivity: "2026-10-23T00:05:00Z",
      }] } }),
      "GET .*threads/group/messages\\?limit=50&networkOnly=true": () => ({ body: { messages: [item("normal-older", 10, 1), item("normal-new", 11, 5)], hasMore: true, nextBefore: 10 } }),
      "GET .*threads/group/listening/messages\\?limit=50": () => ({ body: { messages: [item("heard-new", 30, 4)], hasMore: true, nextBefore: 30 } }),
      "GET .*threads/group/listening/messages\\?limit=50&before=30": () => ({ body: { messages: [item("heard-old", 29, 2)], hasMore: false, nextBefore: null } }),
      "GET .*threads/group/listening": () => ({ body: { enabled: true, dailyCap: 200, defaultDailyCap: 200, toggles: [] } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /分页群/ }));
    const timeline = within(screen.getByRole("region", { name: "会话消息" }));
    await timeline.findByText("heard-new");
    expect(timeline.queryByText("normal-older")).not.toBeInTheDocument();
    await user.click(timeline.getByRole("button", { name: "读取更早消息" }));
    expect(await timeline.findByText("heard-old")).toBeVisible();
    expect(timeline.getByText("normal-older")).toBeVisible();
    expect(calls.some((call) => call.path.endsWith("listening/messages?limit=50&before=30"))).toBe(true);
  });

  it("视口未填满时自动补页，填满后停止", async () => {
    Object.defineProperty(HTMLOListElement.prototype, "clientHeight", { configurable: true, value: 200 });
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "short", sessionKey: "thread:short", channel: "telegram", chatId: "42", chatType: "private", title: "短会话", lastActivity: "2026-10-23T00:03:00Z",
      }] } }),
      "GET .*threads/short/messages\\?limit=50&networkOnly=true": () => ({ body: { messages: [{
        id: "newest", sequence: 3, role: "user", content: "最新", createdAt: "2026-10-23T00:03:00Z", metadata: { threadId: "short" },
      }], hasMore: true, nextBefore: 3 } }),
      "GET .*threads/short/messages\\?limit=50&networkOnly=true&before=3": () => ({ body: { messages: [{
        id: "older", sequence: 2, role: "user", content: "更早", createdAt: "2026-10-23T00:02:00Z", metadata: { threadId: "short" },
      }], hasMore: true, nextBefore: 2 } }),
      "GET .*threads/short/messages\\?limit=50&networkOnly=true&before=2": () => ({ body: { messages: [{
        id: "oldest", sequence: 1, role: "user", content: "最早", createdAt: "2026-10-23T00:01:00Z", metadata: { threadId: "short" },
      }], hasMore: false, nextBefore: null } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /短会话/ }));
    const timeline = within(screen.getByRole("region", { name: "会话消息" }));
    expect(await timeline.findByText("最早")).toBeVisible();
    expect(timeline.getByText("最新")).toBeVisible();
    expect(calls.filter((call) => call.path.includes("&before="))).toHaveLength(2);
  });

  it("视口尚未测量时不自动补页", async () => {
    Object.defineProperty(HTMLOListElement.prototype, "clientHeight", { configurable: true, value: 0 });
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "short", sessionKey: "thread:short", channel: "telegram", chatId: "42", chatType: "private", title: "未测量会话", lastActivity: "2026-10-23T00:03:00Z",
      }] } }),
      "GET .*threads/short/messages\\?limit=50&networkOnly=true": () => ({ body: { messages: [{
        id: "newest", sequence: 3, role: "user", content: "最新", createdAt: "2026-10-23T00:03:00Z", metadata: { threadId: "short" },
      }], hasMore: true, nextBefore: 3 } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /未测量会话/ }));
    await screen.findByText("最新");
    expect(calls.filter((call) => call.path.includes("&before=")).length).toBe(0);
  });

  it("在底部时图片加载后保持贴底", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "image", sessionKey: "thread:image", channel: "telegram", chatId: "42", chatType: "private", title: "图片会话", lastActivity: "2026-10-23T00:03:00Z",
      }] } }),
      "GET .*threads/image/messages\\?limit=50&networkOnly=true": () => ({ body: { messages: [{
        id: "photo", sequence: 1, role: "user", content: "图片消息", createdAt: "2026-10-23T00:03:00Z", metadata: {
          threadId: "image", attachments: [{ id: "photo-file", name: "照片.png", kind: "image" }],
        },
      }], hasMore: false, nextBefore: null } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /图片会话/ }));
    const timeline = screen.getByRole("region", { name: "会话消息" }).querySelector("ol");
    expect(timeline).not.toBeNull();
    await screen.findByRole("img", { name: "照片.png" });
    let scrollHeight = 100;
    Object.defineProperty(timeline, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(timeline, "scrollHeight", { configurable: true, get: () => scrollHeight });
    timeline!.scrollTop = 100;
    scrollHeight = 300;
    fireEvent.load(screen.getByRole("img", { name: "照片.png" }));
    expect(timeline!.scrollTop).toBe(300);
    timeline!.scrollTop = 40;
    fireEvent.scroll(timeline!);
    scrollHeight = 500;
    fireEvent.load(screen.getByRole("img", { name: "照片.png" }));
    expect(timeline!.scrollTop).toBe(40);
    expect(calls.filter((call) => call.path.includes("&before=")).length).toBe(0);
  });

  it("旧页请求期间图片加载仍按请求开始时的滚动锚点恢复", async () => {
    let releaseOlder!: (reply: { body: unknown }) => void;
    const olderPage = new Promise<{ body: unknown }>((resolve) => { releaseOlder = resolve; });
    let scrollHeight = 200;
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "anchor", sessionKey: "thread:anchor", channel: "telegram", chatId: "42", chatType: "private", title: "滚动锚点", lastActivity: "2026-10-23T00:03:00Z",
      }] } }),
      "GET .*threads/anchor/messages\\?limit=50&networkOnly=true": () => ({ body: { messages: [{
        id: "photo", sequence: 2, role: "user", content: "图片消息", createdAt: "2026-10-23T00:03:00Z", metadata: {
          threadId: "anchor", attachments: [{ id: "photo-file", name: "照片.png", kind: "image" }],
        },
      }], hasMore: true, nextBefore: 2 } }),
      "GET .*threads/anchor/messages\\?limit=50&networkOnly=true&before=2": async () => {
        const reply = await olderPage;
        scrollHeight = 400;
        return reply;
      },
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /滚动锚点/ }));
    const timeline = screen.getByRole("region", { name: "会话消息" }).querySelector("ol");
    expect(timeline).not.toBeNull();
    const image = await screen.findByRole("img", { name: "照片.png" });
    Object.defineProperty(timeline, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(timeline, "scrollHeight", { configurable: true, get: () => scrollHeight });
    timeline!.scrollTop = 40;
    fireEvent.scroll(timeline!);
    await user.click(screen.getByRole("button", { name: "读取更早消息" }));
    await waitFor(() => expect(calls.filter((call) => call.path.includes("&before=2")).length).toBe(1));
    scrollHeight = 260;
    fireEvent.load(image);
    expect(timeline!.scrollTop).toBe(40);
    await act(async () => {
      releaseOlder({ body: { messages: [{ id: "older", sequence: 1, role: "user", content: "更早消息", createdAt: "2026-10-23T00:02:00Z", metadata: { threadId: "anchor" } }], hasMore: false, nextBefore: null } });
      await olderPage;
      await Promise.resolve();
    });
    expect(await screen.findByText("更早消息")).toBeVisible();
    expect(timeline!.scrollTop).toBe(240);
  });

  it("多端列表只显示外部渠道，桌面线程仍由主聊天查看", async () => {
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [
        { threadId: "desktop:r1", sessionKey: "role:r1", channel: "desktop", chatId: "r1", chatType: "private", title: "桌面历史", lastActivity: "2026-10-23T12:00:00Z" },
        { threadId: "telegram", sessionKey: "thread:telegram", channel: "telegram", chatId: "42", chatType: "private", title: "手机私聊", lastActivity: "2026-10-23T11:00:00Z" },
      ] } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    const threads = screen.getByRole("complementary", { name: "渠道会话列表" });
    expect(await within(threads).findByRole("button", { name: /手机私聊/ })).toBeVisible();
    expect(within(threads).queryByRole("button", { name: /桌面历史/ })).not.toBeInTheDocument();
    expect(screen.getByText("1 个会话")).toBeVisible();
  });

  it("兼容省略最近消息字段的旧接口，同时过滤明确为空的线程", async () => {
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [
        { threadId: "legacy", sessionKey: "thread:legacy", channel: "telegram", chatId: "42", chatType: "private", title: "旧接口有效会话", lastActivity: "2026-10-23T00:00:00Z" },
        { threadId: "empty", sessionKey: "thread:empty", channel: "telegram", chatId: "43", chatType: "private", title: "明确为空的会话", lastActivity: "2026-10-23T00:00:00Z", latestMessage: null },
      ] } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    const threads = screen.getByRole("complementary", { name: "渠道会话列表" });
    expect(await within(threads).findByRole("button", { name: /旧接口有效会话/ })).toBeVisible();
    expect(within(threads).queryByRole("button", { name: /明确为空的会话/ })).not.toBeInTheDocument();
  });

  it("手机会话只展示聊天，账号接入通过角色账号入口打开", async () => {
    setupBackend([makeAccount("telegram", "telegram-1", { enabled: true })]);
    const onAccounts = vi.fn();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} onAccounts={onAccounts} />);
    await screen.findByText("暂无渠道会话");
    expect(screen.queryByLabelText("Telegram Bot Token")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "生成配对码" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "角色账号" }));
    expect(onAccounts).toHaveBeenCalledOnce();
  });

  it("切换角色后丢弃旧角色延迟返回的会话目录", async () => {
    let release: ((reply: { body: unknown }) => void) | undefined;
    const pending = new Promise<{ body: unknown }>((resolve) => { release = resolve; });
    fakeBackend({
      "GET /api/roles/r[12]/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => pending,
      "GET /api/roles/r2/channels/threads\\?networkOnly=true": () => ({ body: { threads: [
        { threadId: "r2-thread", sessionKey: "thread:r2-thread", channel: "qqbot", chatId: "c2c", chatType: "private", title: "第二角色私聊", lastActivity: "2026-10-23T00:00:00Z" },
      ] } }),
    });
    const view = render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    view.rerender(<ChannelView role={role({ id: "r2", name: "爱丽丝" })} onBack={() => undefined} />);
    await screen.findByRole("button", { name: /第二角色私聊/ });
    await act(async () => {
      release?.({ body: { threads: [{ threadId: "r1-thread", sessionKey: "thread:r1-thread", channel: "telegram", chatId: "42", chatType: "private", title: "第一角色秘密", lastActivity: "2026-10-23T00:00:00Z" }] } });
      await pending;
    });
    expect(screen.queryByRole("button", { name: /第一角色秘密/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /第二角色私聊/ })).toBeVisible();
  });

  it("实时消息直接合并当前线程和列表，不重新读取会话目录", async () => {
    const sources: TestEventSource[] = [];
    class TestEventSource extends EventTarget {
      close = vi.fn();
      constructor(public url: string) { super(); sources.push(this); }
    }
    vi.stubGlobal("EventSource", TestEventSource);
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "live", sessionKey: "thread:live", channel: "telegram", chatId: "42", chatType: "private", title: "实时联系人",
        lastActivity: "2026-10-23T00:00:00Z", latestMessage: { content: "旧消息", role: "user", status: "completed", createdAt: "2026-10-23T00:00:00Z" },
      }] } }),
      "GET /api/roles/r1/channels/threads/live/messages\\?limit=50&networkOnly=true": () => ({ body: { messages: [{
        id: "old", sequence: 1, role: "user", content: "旧消息", createdAt: "2026-10-23T00:00:00Z", metadata: { threadId: "live", senderName: "联系人" },
      }], hasMore: false, nextBefore: null } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /实时联系人/ }));
    expect(await screen.findByText("旧消息")).toBeVisible();
    await act(async () => {
      sources[0].dispatchEvent(new MessageEvent("channel_update", { data: JSON.stringify({
        type: "message", roleId: "r1", thread: {
          threadId: "live", sessionKey: "thread:live", channel: "telegram", chatId: "42", chatType: "private", title: "实时联系人", lastActivity: "2026-10-23T00:01:00Z",
        }, message: { id: "new", sequence: 2, role: "user", content: "新到消息", createdAt: "2026-10-23T00:01:00Z", metadata: { threadId: "live", senderName: "联系人" } },
      }) }));
    });
    expect(await screen.findByText("新到消息")).toBeVisible();
    expect(calls.filter((call) => call.path === "/api/roles/r1/channels/threads?networkOnly=true")).toHaveLength(1);
  });

  it("QQ 群的旁听记录与正常消息按时间合并为只读时间线", async () => {
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [{ accountId: "qq-1", enabled: true, platformEnabled: true }] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "group", sessionKey: "thread:group", accountId: "qq-1", channel: "qq", chatId: "42", chatType: "group", title: "测试群", lastActivity: "2026-10-23T00:02:00Z",
      }] } }),
      "GET /api/roles/r1/channels/threads/group/messages\\?limit=50&networkOnly=true": () => ({ body: { messages: [{
        id: "said", sequence: 1, role: "user", content: "点名后的消息", createdAt: "2026-10-23T00:02:00Z", metadata: { threadId: "group", senderName: "群友" },
      }], hasMore: false, nextBefore: null } }),
      "GET /api/roles/r1/channels/threads/group/listening": () => ({ body: { enabled: true, dailyCap: 200, defaultDailyCap: 200, toggles: [] } }),
      "GET /api/roles/r1/channels/threads/group/listening/messages\\?limit=50": () => ({ body: { messages: [{
        id: "heard", sequence: 1, role: "user", content: "没有点名的旁听消息", createdAt: "2026-10-23T00:01:00Z", metadata: { threadId: "group", senderName: "路人" },
      }], hasMore: false, nextBefore: null } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "诗织" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /测试群/ }));
    const messages = screen.getByRole("region", { name: "会话消息" });
    expect(await within(messages).findByText("没有点名的旁听消息")).toBeVisible();
    expect(within(messages).getByText("点名后的消息")).toBeVisible();
    expect(within(messages).getByText("旁听记录 · 独立保存")).toBeVisible();
    expect(within(messages).getByText("旁听记录 · 独立保存").compareDocumentPosition(within(messages).getByText("点名后的消息")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
  it("按已验证渠道线程的记忆序号范围定位并突出来源消息", async () => {
    const source = { kind: "turn", sessionKey: "thread:t1", messageIds: ["source-7"], messageRange: [7, 8] as [number, number], stableSourceKey: "turn:source" };
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "主人 Telegram", lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET .*channels/threads/t1/messages.*": (_init, match) => ({ body: { messages: [{ id: match[0].includes("before=9") ? "source-7" : "recent", sequence: match[0].includes("before=9") ? 7 : 99, role: "user", content: match[0].includes("before=9") ? "手机上的来源消息" : "最新消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "t1" } }], hasMore: false, nextBefore: null } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} initialSource={{ origin: source, messageId: "source-7" }} />);
    const message = await screen.findByText("手机上的来源消息");
    expect(message.closest("li")).toHaveClass("source-highlight");
    expect(within(message.closest("li")!).getByText("记忆来源")).toBeInTheDocument();
    expect(calls.some((call) => call.path.includes("before=9"))).toBe(true);
  });

  it("来源不在列表且角色归属查询拒绝时不读取其消息", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [] } }),
      "GET .*threads/other-role/messages\\?limit=1&networkOnly=true": () => ({ status: 404, body: { detail: "来源不存在" } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} initialSource={{ origin: { kind: "turn", sessionKey: "thread:other-role", messageIds: ["source"], messageRange: null, stableSourceKey: "source" }, messageId: "source" }} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("来源渠道会话当前不可访问");
    expect(calls.filter((call) => call.path.includes("/messages"))).toHaveLength(1);
  });

  it("能通过角色范围接口验证并定位首页线程列表之外的旧来源", async () => {
    const thread = { threadId: "old", sessionKey: "thread:old", channel: "telegram", chatId: "42", chatType: "private", title: "旧手机会话", lastActivity: "2026-10-14T00:00:00Z" };
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [] } }),
      "GET .*threads/old/messages.*": () => ({ body: { thread, messages: [{ id: "old-source", sequence: 7, role: "user", content: "旧线程的来源消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "old" } }], hasMore: false, nextBefore: null } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} initialSource={{ origin: { kind: "turn", sessionKey: "thread:old", messageIds: ["old-source"], messageRange: [7, 8], stableSourceKey: "source" }, messageId: "old-source" }} />);
    expect((await screen.findByText("旧线程的来源消息")).closest("li")).toHaveClass("source-highlight");
    expect(screen.getByRole("button", { name: /旧手机会话/ })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "刷新会话" }));
    await waitFor(() => expect(calls.filter((call) => call.path === "/api/roles/r1/channels/threads?networkOnly=true")).toHaveLength(2));
    expect(screen.getByRole("button", { name: /旧手机会话/ })).toBeInTheDocument();
    expect(screen.getByText("旧线程的来源消息")).toBeInTheDocument();
  });

  it("旁听来源从独立记录中定位并展开当前群的旁听面板", async () => {
    const heard = { id: "listen:source", sequence: 3, role: "user", content: "主人的旁听发言", createdAt: "2026-10-14T00:00:00Z", metadata: { senderId: "owner" } };
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "group", sessionKey: "thread:group", channel: "qq", chatId: "42", chatType: "group", title: "来源群", lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET .*threads/group/messages.*": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
      "GET .*threads/group/listening": () => ({ body: { enabled: true, dailyCap: null, defaultDailyCap: 200, toggles: [] } }),
      "GET .*threads/group/listening/messages.*": () => ({ body: { messages: [heard], hasMore: false, nextBefore: null } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} initialSource={{ origin: { kind: "listening", sessionKey: "thread:group", messageIds: ["listen:source"], messageRange: null, stableSourceKey: "source" }, messageId: "listen:source" }} />);
    const source = await screen.findByRole("region", { name: "旁听记忆来源" });
    expect(within(source).getByText("主人的旁听发言")).toBeVisible();
    expect(source).toHaveClass("source-highlight");
    await waitFor(() => expect(screen.getByText(/^群旁听记录/, { selector: "summary" }).closest("details")).toHaveAttribute("open"));
  });

  it("来源分页游标不前进时停止请求并明确提示消息不可访问", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "主人 Telegram", lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET .*channels/threads/t1/messages.*": () => ({ body: { messages: [], hasMore: true, nextBefore: 51 } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} initialSource={{ origin: { kind: "turn", sessionKey: "thread:t1", messageIds: ["deleted"], messageRange: null, stableSourceKey: "source" }, messageId: "deleted" }} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("来源消息当前不可访问");
    // The normal thread head may load before or after the source lookup;
    // only the source pagination must stop when its cursor stops advancing.
    expect(calls.filter((call) => call.path.includes("/messages?limit=100&networkOnly=true")).map((call) => call.path)).toEqual([
      "/api/roles/r1/channels/threads/t1/messages?limit=100&networkOnly=true",
      "/api/roles/r1/channels/threads/t1/messages?limit=100&networkOnly=true&before=51",
    ]);
  });

  it("引用原文未加载时仍展示引用消息的图片和文件附件", async () => {
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "访客", lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET .*channels/threads/t1/messages.*": () => ({ body: { messages: [{
        id: "quoted", sequence: 1, role: "user", content: "请看这些附件", createdAt: "2026-10-14T00:00:00Z", metadata: {
          threadId: "t1", replyTo: "original", replyToSenderName: "Alice", replyToContent: "附件说明",
          quotedAttachments: [{ id: "quote-image", name: "引用图片.png", kind: "image" }, { id: "quote-file", name: "引用笔记.md", kind: "file" }],
          attachments: [{ id: "own-file", name: "当前附件.txt", kind: "file" }],
        },
      }], hasMore: false, nextBefore: null } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /访客/ }));
    expect(await screen.findByText("回复 Alice：附件说明")).toBeInTheDocument();
    const quoted = screen.getByRole("group", { name: "引用消息附件" });
    expect(within(quoted).getByRole("link", { name: "引用图片.png" })).toHaveAttribute("href", "/api/roles/r1/attachments/quote-image");
    expect(within(quoted).getByRole("img", { name: "引用图片.png" })).toHaveAttribute("src", "/api/roles/r1/attachments/quote-image");
    expect(within(quoted).getByRole("link", { name: "引用笔记.md" })).toHaveAttribute("href", "/api/roles/r1/attachments/quote-file");
    expect(screen.getByRole("link", { name: "当前附件.txt" })).toHaveAttribute("href", "/api/roles/r1/attachments/own-file");
  });

  it("分页读取更早消息并显示消息附件", async () => {
    const makeMessage = (sequence: number) => ({
      id: `m${sequence}`,
      sequence,
      role: sequence % 2 === 0 ? "assistant" : "user",
      content: sequence === 100 ? "[收到附件]" : `消息 ${sequence}`,
      createdAt: "2026-10-14T00:00:00Z",
      metadata: {
        threadId: "t1",
        senderName: "访客",
        ...(sequence === 100 ? { attachments: [{ id: "a1", name: "photo.jpg", kind: "image" }] } : {}),
      },
    });
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "访客", lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET .*channels/threads/t1/messages.*": (_init, match) => {
        const olderPage = match[0].includes("before=51");
        return { body: { messages: Array.from({ length: 50 }, (_, index) => makeMessage(olderPage ? index + 1 : index + 51)), hasMore: !olderPage, nextBefore: olderPage ? null : 51 } };
      },
    });

    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /访客/ }));
    expect(await screen.findByText("消息 51")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "photo.jpg" })).toHaveAttribute("href", "/api/roles/r1/attachments/a1");
    await user.click(screen.getByRole("button", { name: "读取更早消息" }));
    expect(await screen.findByText("消息 1")).toBeInTheDocument();
    expect(screen.getByText("[收到附件]")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "读取更早消息" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "刷新会话" }));
    await waitFor(() => expect(calls.filter((call) => call.path.includes("/channels/threads/t1/messages")).length).toBe(3));
    expect(screen.queryByRole("button", { name: "读取更早消息" })).not.toBeInTheDocument();
  });

  it("切换会话后不会把延迟返回的分页消息混入当前会话", async () => {
    let releaseOlder: ((reply: { body: unknown }) => void) | undefined;
    const olderPage = new Promise<{ body: unknown }>((resolve) => { releaseOlder = resolve; });
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [
        { threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "1", chatType: "private", title: "会话 A", lastActivity: "2026-10-14T00:00:00Z" },
        { threadId: "t2", sessionKey: "thread:t2", channel: "telegram", chatId: "2", chatType: "private", title: "会话 B", lastActivity: "2026-10-14T00:00:00Z" },
      ] } }),
      "GET .*channels/threads/([^/]+)/messages.*": (_init, match) => {
        if (match[0].includes("t1") && match[0].includes("before=51")) return olderPage;
        if (match[0].includes("t1")) return { body: { messages: [{ id: "a51", sequence: 51, role: "user", content: "A 当前消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "t1" } }], hasMore: true, nextBefore: 51 } };
        return { body: { messages: [{ id: "b1", sequence: 1, role: "user", content: "B 当前消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "t2" } }], hasMore: false, nextBefore: null } };
      },
    });

    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /会话 A/ }));
    expect(await screen.findByText("A 当前消息")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "读取更早消息" }));
    await user.click(await screen.findByRole("button", { name: /会话 B/ }));
    expect(await screen.findByText("B 当前消息")).toBeInTheDocument();

    await act(async () => {
      releaseOlder?.({ body: { messages: [{ id: "a1", sequence: 1, role: "user", content: "A 延迟旧消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "t1" } }], hasMore: false, nextBefore: null } });
      await olderPage;
    });
    expect(screen.queryByText("A 延迟旧消息")).not.toBeInTheDocument();
    expect(screen.getByText("B 当前消息")).toBeInTheDocument();
  });

  it("切走再返回后旧分页请求不会改变新的分页状态", async () => {
    let releaseOlder: ((reply: { body: unknown }) => void) | undefined;
    const olderPage = new Promise<{ body: unknown }>((resolve) => { releaseOlder = resolve; });
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [
        { threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "1", chatType: "private", title: "会话 A", lastActivity: "2026-10-14T00:00:00Z" },
        { threadId: "t2", sessionKey: "thread:t2", channel: "telegram", chatId: "2", chatType: "private", title: "会话 B", lastActivity: "2026-10-14T00:00:00Z" },
      ] } }),
      "GET .*channels/threads/([^/]+)/messages.*": (_init, match) => {
        if (match[0].includes("before=")) return olderPage;
        return { body: { messages: [{ id: match[1], sequence: 51, role: "user", content: `${match[1]} 当前消息`, createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: match[1] } }], hasMore: true, nextBefore: 51 } };
      },
    });

    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /会话 A/ }));
    await user.click(await screen.findByRole("button", { name: "读取更早消息" }));
    await user.click(screen.getByRole("button", { name: /会话 B/ }));
    expect(await screen.findByRole("button", { name: "读取更早消息" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: /会话 A/ }));
    expect(await screen.findByRole("button", { name: "读取更早消息" })).toBeEnabled();
    await act(async () => {
      releaseOlder?.({ body: { messages: [{ id: "a1", sequence: 1, role: "user", content: "过期分页消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "t1" } }], hasMore: false, nextBefore: null } });
      await olderPage;
    });
    expect(screen.queryByText("过期分页消息")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "读取更早消息" })).toBeEnabled();
  });

  it("账号已移除时仍可读取历史，会话页不提供账号删除操作", async () => {
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", accountId: "removed", channel: "telegram", chatId: "42", chatType: "private", title: "历史访客", accountRemoved: true, lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET .*channels/threads/t1/messages.*": () => ({ body: { messages: [{ id: "m1", sequence: 1, role: "user", content: "保留的历史消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "t1" } }], hasMore: false, nextBefore: null } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: /历史访客/ }));
    expect(await screen.findByText("保留的历史消息")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /移除.*账号/ })).not.toBeInTheDocument();
  });

  it("展示会话预览、已移除账号来源、引用提及和平台发送回执", async () => {
    const source = { platform: "telegram", platformAccountId: "bot-7", accountId: "telegram-1", displayName: "历史 Bot" };
    const makeMessage = (id: string, sequence: number, content: string, metadata: Record<string, unknown>, messageRole = "user") => ({
      id, sequence, role: messageRole, content, createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "t1", viaAccount: source, ...metadata },
    });
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "group", title: "测试群", lastActivity: "2026-10-14T00:00:00Z", viaAccount: source, accountRemoved: true, latestMessage: { content: "最近的角色回复", role: "assistant", status: "sent", createdAt: "2026-10-14T00:00:00Z" } }] } }),
      "GET .*channels/threads/t1/messages.*": () => ({ body: { messages: [
        makeMessage("m1", 1, "群里的原始问题", { senderId: "alice", senderName: "Alice", platformMessageId: "p-1" }),
        makeMessage("m2", 2, "最近的角色回复", { replyTo: "p-1", replyToSenderId: "alice", mentionedIds: ["alice"], deliveryStatus: "sent", platformMessageId: "p-2" }, "assistant"),
        makeMessage("m3", 3, "另一次回复", { replyTo: "p-missing", replyToSenderId: "bob", deliveryStatus: "uncertain" }, "assistant"),
        makeMessage("m4", 4, "发送中的回复", { deliveryStatus: "pending" }, "assistant"),
        makeMessage("m5", 5, "失败的回复", { deliveryStatus: "failed" }, "assistant"),
      ], hasMore: false, nextBefore: null } }),
    });

    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    const thread = await screen.findByRole("button", { name: /测试群/ });
    expect(within(thread).getByText("Elysia：最近的角色回复")).toBeInTheDocument();
    expect(within(thread).getByText(/历史 Bot/)).toBeInTheDocument();
    expect(within(thread).getByText("账号已移除")).toBeInTheDocument();
    await user.click(thread);
    const messages = screen.getByRole("region", { name: "会话消息" });
    expect(await within(messages).findByText("回复 Alice：群里的原始问题")).toBeInTheDocument();
    expect(within(messages).getByText("提及：@Alice")).toBeInTheDocument();
    expect(within(messages).getByText("回复 bob 的消息 p-missing（原文未加载）")).toBeInTheDocument();
    expect(within(messages).getAllByText("来源：历史 Bot · Telegram")).toHaveLength(5);
    expect(within(messages).getByText("已发送")).toBeInTheDocument();
    expect(within(messages).getByText("发送结果不确定")).toBeInTheDocument();
    expect(within(messages).getByText("发送中")).toBeInTheDocument();
    expect(within(messages).getByText("发送失败")).toBeInTheDocument();
    await user.click(within(messages).getByText("平台回执"));
    expect(within(messages).getByText("消息编号：p-2")).toBeVisible();
  });

});

it.each([false, true])("群聊标题提供独立群聊设置入口，返回不丢失当前消息（手机模式 %s）", async phoneMode => {
  const message = { id: "m", sequence: 1, role: "user", content: "群聊现有消息", createdAt: "2026-10-25T06:00:00Z", metadata: { threadId: "group-thread" } };
  fakeBackend({
    "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [makeAccount("qq", "qq-1", { enabled: true, connectionStatus: "connected" })] } }),
    "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "group-thread", accountId: "qq-1", channel: "qq", chatId: "42", chatType: "group", title: "导航群", lastActivity: message.createdAt, latestMessage: message }] } }),
    "GET .*threads/group-thread/messages.*": () => ({ body: { messages: [message], hasMore: false } }),
    "GET .*threads/group-thread/listening": () => ({ body: { enabled: true, dailyCap: null, defaultDailyCap: 200, toggles: [] } }),
    "GET .*threads/group-thread/listening/messages.*": () => ({ body: { messages: [], hasMore: false } }),
    "GET /api/roles/r1/channels/accounts/qq-1/groups": () => ({ body: { groups: [{ chatId: "42", title: "导航群" }] } }),
    "GET /api/roles/r1/channels/accounts/qq-1/groups/42/settings": () => ({ body: { chatId: "42", title: "导航群", threadId: "group-thread", listening: true, naturalEnabled: false } }),
  });
  const user = userEvent.setup();
  render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode={phoneMode} onBack={() => undefined} onAccounts={() => undefined} />);
  if (phoneMode) await user.click(await screen.findByRole("button", { name: "QQ" }));
  await user.click(await screen.findByRole("button", { name: /导航群/ }));
  expect(await within(screen.getByRole("region", { name: "会话消息" })).findByText("群聊现有消息")).toBeVisible();
  const timeline = within(screen.getByRole("region", { name: "会话消息" })).getAllByRole("list")[0];
  Object.defineProperties(timeline, { scrollHeight: { configurable: true, value: 2000 }, clientHeight: { configurable: true, value: 400 } });
  timeline.scrollTop = 520;
  fireEvent.scroll(timeline);
  await user.click(screen.getByRole("button", { name: "群聊设置" }));
  expect(await screen.findByRole("checkbox", { name: "自然回复（无需 @）" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "返回上一页" }));
  expect(await within(screen.getByRole("region", { name: "会话消息" })).findByText("群聊现有消息")).toBeVisible();
  expect(screen.getByRole("button", { name: "群聊设置" })).toHaveFocus();
  expect(timeline).toBeInTheDocument();
  expect(timeline.scrollTop).toBe(520);
});

it("手机 QQ 应用没有任何正常会话时仍可进入群聊目录", async () => {
  fakeBackend({
    "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [makeAccount("qq", "qq-1", { enabled: true, connectionStatus: "connected" })] } }),
    "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [] } }),
    "GET /api/roles/r1/channels/accounts/qq-1/groups": () => ({ body: { groups: [{ chatId: "42", title: "还没聊过的群" }] } }),
  });
  const user = userEvent.setup();
  render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} onAccounts={() => undefined} />);
  await user.click(await screen.findByRole("button", { name: "QQ" }));
  await user.click(screen.getByRole("button", { name: "群聊设置" }));
  expect(await screen.findByRole("button", { name: /还没聊过的群/ })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "返回上一页" }));
  expect(screen.getByText("还没有会话")).toBeVisible();
});
