import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
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
    "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [] } }),
  });
}

describe("渠道记忆来源只读查看", () => {
  const sourceRole = role({ id: "r1", name: "Elysia" });
  const thread = { threadId: "source", sessionKey: "thread:source", channel: "telegram", chatId: "42", chatType: "private", title: "来源私聊", lastActivity: "2026-10-14T00:00:00Z" };

  it("直接验证角色所属线程并分页定位来源，不加载账号或线程目录，不显示管理控件", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=1": () => ({ body: { thread } }),
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=100": () => ({ body: { messages: [], hasMore: true, nextBefore: 51 } }),
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=100&before=51": () => ({ body: { messages: [{ id: "origin", sequence: 7, role: "user", content: "分页找到的原文", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "source" } }], hasMore: false, nextBefore: null } }),
    });
    render(<ChannelView role={sourceRole} sourceOnly onBack={() => undefined} initialSource={{ origin: { kind: "turn", sessionKey: "thread:source", messageIds: ["origin"], messageRange: null, stableSourceKey: "turn:source" } }} />);
    expect((await screen.findByText("分页找到的原文")).closest("li")).toHaveClass("source-highlight");
    expect(calls.map((call) => call.path)).toEqual([
      "/api/roles/r1/channels/threads/source/messages?limit=1",
      "/api/roles/r1/channels/threads/source/messages?limit=100",
      "/api/roles/r1/channels/threads/source/messages?limit=100&before=51",
    ]);
    expect(calls.every((call) => call.method === "GET")).toBe(true);
    expect(screen.queryByRole("complementary", { name: "渠道会话列表" })).not.toBeInTheDocument();
    expect(screen.queryByText("会话设置")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "角色账号" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /手机会话/ })).not.toBeInTheDocument();
  });

  it("群旁听来源从独立记录分页定位原文，只读展示且不挂载群管理面板", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=1": () => ({ body: { thread: { ...thread, channel: "qq", chatType: "group" } } }),
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
      "GET /api/roles/r1/channels/threads/foreign/messages\\?limit=1": () => ({ status: 404, body: { detail: "此线程不属于当前角色" } }),
    });
    render(<ChannelView role={sourceRole} sourceOnly onBack={() => undefined} initialSource={{ origin: { kind: "listening", sessionKey: "thread:foreign", messageIds: ["secret"], messageRange: null, stableSourceKey: "foreign" } }} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("来源渠道会话当前不可访问");
    expect(calls).toHaveLength(1);
  });

  it("切换来源角色后立即清空旧原文，归属拒绝时不保留之前角色的消息", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=1": () => ({ body: { thread } }),
      "GET /api/roles/r1/channels/threads/source/messages\\?limit=100": () => ({ body: { messages: [{ id: "origin", sequence: 7, role: "user", content: "第一个角色的来源原文", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "source" } }], hasMore: false, nextBefore: null } }),
      "GET /api/roles/r2/channels/threads/source/messages\\?limit=1": () => ({ status: 404, body: { detail: "来源不存在" } }),
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
  it("按已验证渠道线程的记忆序号范围定位并突出来源消息", async () => {
    const source = { kind: "turn", sessionKey: "thread:t1", messageIds: ["source-7"], messageRange: [7, 8] as [number, number], stableSourceKey: "turn:source" };
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "主人 Telegram", lastActivity: "2026-10-14T00:00:00Z" }] } }),
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
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [] } }),
      "GET .*threads/other-role/messages\\?limit=1": () => ({ status: 404, body: { detail: "来源不存在" } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} initialSource={{ origin: { kind: "turn", sessionKey: "thread:other-role", messageIds: ["source"], messageRange: null, stableSourceKey: "source" }, messageId: "source" }} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("来源渠道会话当前不可访问");
    expect(calls.filter((call) => call.path.includes("/messages"))).toHaveLength(1);
  });

  it("能通过角色范围接口验证并定位首页线程列表之外的旧来源", async () => {
    const thread = { threadId: "old", sessionKey: "thread:old", channel: "telegram", chatId: "42", chatType: "private", title: "旧手机会话", lastActivity: "2026-10-14T00:00:00Z" };
    const { calls } = fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [] } }),
      "GET .*threads/old/messages.*": () => ({ body: { thread, messages: [{ id: "old-source", sequence: 7, role: "user", content: "旧线程的来源消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "old" } }], hasMore: false, nextBefore: null } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} initialSource={{ origin: { kind: "turn", sessionKey: "thread:old", messageIds: ["old-source"], messageRange: [7, 8], stableSourceKey: "source" }, messageId: "old-source" }} />);
    expect((await screen.findByText("旧线程的来源消息")).closest("li")).toHaveClass("source-highlight");
    expect(screen.getByRole("button", { name: /旧手机会话/ })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "刷新会话" }));
    await waitFor(() => expect(calls.filter((call) => call.path === "/api/roles/r1/channels/threads")).toHaveLength(2));
    expect(screen.getByRole("button", { name: /旧手机会话/ })).toBeInTheDocument();
    expect(screen.getByText("旧线程的来源消息")).toBeInTheDocument();
  });

  it("旁听来源从独立记录中定位并展开当前群的旁听面板", async () => {
    const heard = { id: "listen:source", sequence: 3, role: "user", content: "主人的旁听发言", createdAt: "2026-10-14T00:00:00Z", metadata: { senderId: "owner" } };
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "group", sessionKey: "thread:group", channel: "qq", chatId: "42", chatType: "group", title: "来源群", lastActivity: "2026-10-14T00:00:00Z" }] } }),
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
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "主人 Telegram", lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET .*channels/threads/t1/messages.*": () => ({ body: { messages: [], hasMore: true, nextBefore: 51 } }),
    });
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} initialSource={{ origin: { kind: "turn", sessionKey: "thread:t1", messageIds: ["deleted"], messageRange: null, stableSourceKey: "source" }, messageId: "deleted" }} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("来源消息当前不可访问");
    // The normal thread head may load before or after the source lookup;
    // only the source pagination must stop when its cursor stops advancing.
    expect(calls.filter((call) => call.path.includes("/messages?limit=100")).map((call) => call.path)).toEqual([
      "/api/roles/r1/channels/threads/t1/messages?limit=100",
      "/api/roles/r1/channels/threads/t1/messages?limit=100&before=51",
    ]);
  });

  it("引用原文未加载时仍展示引用消息的图片和文件附件", async () => {
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "访客", lastActivity: "2026-10-14T00:00:00Z" }] } }),
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
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "访客", lastActivity: "2026-10-14T00:00:00Z" }] } }),
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
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [
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
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [
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
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", accountId: "removed", channel: "telegram", chatId: "42", chatType: "private", title: "历史访客", accountRemoved: true, lastActivity: "2026-10-14T00:00:00Z" }] } }),
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
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "group", title: "测试群", lastActivity: "2026-10-14T00:00:00Z", viaAccount: source, accountRemoved: true, latestMessage: { content: "最近的角色回复", role: "assistant", status: "sent", createdAt: "2026-10-14T00:00:00Z" } }] } }),
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
