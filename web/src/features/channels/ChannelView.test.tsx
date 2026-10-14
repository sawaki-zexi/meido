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

function setupBackend(initialAccounts: ReturnType<typeof makeAccount>[] = [], groups: { chatId: string; title: string; listening: boolean }[] = []) {
  let accounts = initialAccounts;
  const { calls } = fakeBackend({
    "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts } }),
    "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [] } }),
    "PUT /api/roles/r1/channels/(telegram|qq|qqbot|feishu)": (_init, match) => {
      const channel = match[1];
      const body = calls.at(-1)?.body as Record<string, unknown>;
      const previous = accounts.find((item) => item.channel === channel);
      const account = makeAccount(channel, String(body.accountId ?? `${channel}-1`), {
        ...previous,
        enabled: body.enabled,
        allowPrivate: body.allowPrivate,
        allowGroup: body.allowGroup,
        displayName: body.displayName,
        blockedSenders: body.blockedSenders,
        platformId: body.appId ?? previous?.platformId ?? null,
        tokenConfigured: Boolean(body.token || body.accessToken || body.clientSecret || body.appSecret || previous?.tokenConfigured),
        credentialsConfigured: true,
      });
      accounts = [...accounts.filter((item) => item.channel !== channel), account];
      return { body: { account } };
    },
    "POST /api/roles/r1/channels/telegram/([^/]+)/test": () => ({ body: { account: accounts.find((item) => item.channel === "telegram") } }),
    "POST /api/roles/r1/channels/(qq|qqbot|feishu)/([^/]+)/test": (_init, match) => ({ body: { account: accounts.find((item) => item.channel === match[1]) } }),
    "POST /api/roles/r1/channels/accounts/([^/]+)/state": (_init, match) => {
      const body = calls.at(-1)?.body as { enabled: boolean };
      accounts = accounts.map((item) => item.accountId === match[1] ? { ...item, enabled: body.enabled } : item);
      return { body: { account: accounts.find((item) => item.accountId === match[1]) } };
    },
    "DELETE /api/roles/r1/channels/accounts/([^/]+)": (_init, match) => {
      accounts = accounts.filter((item) => item.accountId !== match[1]);
      return { status: 204 };
    },
    "GET /api/roles/r1/channels/accounts/qq-1/groups": () => ({ body: { groups } }),
    "PUT /api/roles/r1/channels/accounts/qq-1/groups/([^/]+)/listening": (_init, match) => ({ body: { chatId: match[1], listening: true } }),
    "DELETE /api/roles/r1/channels/accounts/qq-1/groups/([^/]+)/listening/messages": () => ({ status: 204 }),
  });
  return { calls, getAccounts: () => accounts };
}

describe("渠道会话查看", () => {
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
    expect(calls.filter((call) => call.path.includes("/messages"))).toHaveLength(3);
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

  it("移除渠道账号后仍可选择并读取历史会话", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    let accounts = [makeAccount("telegram", "telegram-1", { tokenConfigured: true })];
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts } }),
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "t1", sessionKey: "thread:t1", channel: "telegram", chatId: "42", chatType: "private", title: "历史访客", lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET .*channels/threads/t1/messages.*": () => ({ body: { messages: [{ id: "m1", sequence: 1, role: "user", content: "保留的历史消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "t1" } }], hasMore: false, nextBefore: null } }),
      "DELETE /api/roles/r1/channels/accounts/telegram-1": () => {
        accounts = [];
        return { status: 204 };
      },
    });

    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: "移除Telegram账号" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "移除Telegram账号" })).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /历史访客/ }));
    expect(await screen.findByText("保留的历史消息")).toBeInTheDocument();
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

  it("为四种渠道提交各自的账号配置字段", async () => {
    const { calls } = setupBackend();
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);

    await user.type(await screen.findByLabelText("Telegram Bot Token"), "tg-secret");
    await user.click(screen.getByRole("button", { name: "添加账号" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path.endsWith("/channels/telegram"))).toBe(true));
    expect(calls.find((call) => call.path.endsWith("/channels/telegram"))?.body).toMatchObject({ token: "tg-secret", enabled: false, allowGroup: true });

    await user.click(screen.getByRole("tab", { name: /NapCat QQ/ }));
    await user.type(await screen.findByLabelText("NapCat WebSocket 地址"), "wss://qq.local/ws");
    await user.type(screen.getByLabelText("NapCat Access Token"), "napcat-secret");
    await user.click(screen.getByRole("button", { name: "添加账号" }));
    await waitFor(() => expect(calls.some((call) => call.path.endsWith("/channels/qq"))).toBe(true));
    expect(calls.find((call) => call.path.endsWith("/channels/qq"))?.body).toMatchObject({ endpoint: "wss://qq.local/ws", accessToken: "napcat-secret" });

    await user.click(screen.getByRole("tab", { name: /官方 QQBot/ }));
    await user.type(await screen.findByLabelText("平台 API 地址"), "https://qqbot.example/api");
    await user.type(screen.getByLabelText("QQ Bot App ID"), "app-qq");
    await user.type(screen.getByLabelText("QQ Bot Client Secret"), "qq-secret");
    await user.click(screen.getByRole("button", { name: "添加账号" }));
    await waitFor(() => expect(calls.some((call) => call.path.endsWith("/channels/qqbot"))).toBe(true));
    expect(calls.find((call) => call.path.endsWith("/channels/qqbot"))?.body).toMatchObject({ endpoint: "https://qqbot.example/api", appId: "app-qq", clientSecret: "qq-secret" });

    await user.click(screen.getByRole("tab", { name: /飞书/ }));
    await user.type(await screen.findByLabelText("平台 API 地址"), "https://feishu.example/open-apis");
    await user.type(screen.getByLabelText("飞书 App ID"), "app-feishu");
    await user.type(screen.getByLabelText("飞书 App Secret"), "feishu-secret");
    await user.type(screen.getByLabelText(/Verification Token/), "verify-secret");
    await user.type(screen.getByLabelText(/Encrypt Key/), "encrypt-secret");
    await user.click(screen.getByRole("button", { name: "添加账号" }));
    await waitFor(() => expect(calls.some((call) => call.path.endsWith("/channels/feishu"))).toBe(true));
    expect(calls.find((call) => call.path.endsWith("/channels/feishu"))?.body).toMatchObject({ appId: "app-feishu", appSecret: "feishu-secret", verificationToken: "verify-secret", encryptKey: "encrypt-secret" });
  });

  it("测试、启停、编辑和移除账号", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const { calls, getAccounts } = setupBackend();
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await user.type(await screen.findByLabelText("Telegram Bot Token"), "tg-secret");
    await user.click(screen.getByRole("button", { name: "添加账号" }));
    await waitFor(() => expect(getAccounts()).toHaveLength(1));

    await user.click(await screen.findByRole("button", { name: "测试连接" }));
    expect(await screen.findByText(/最近测试通过/)).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST" && call.path.endsWith("/channels/telegram/telegram-1/test"))).toBe(true);

    await user.click(screen.getByRole("button", { name: "启用" }));
    await waitFor(() => expect(getAccounts()[0].enabled).toBe(true));
    expect(calls.some((call) => call.method === "POST" && call.path.endsWith("/channels/accounts/telegram-1/state"))).toBe(true);

    await user.clear(screen.getByLabelText("显示名称"));
    await user.type(screen.getByLabelText("显示名称"), "主 Bot");
    await user.click(screen.getByRole("button", { name: "保存设置" }));
    await waitFor(() => expect(getAccounts()[0].displayName).toBe("主 Bot"));
    expect(calls.filter((call) => call.method === "PUT" && call.path.endsWith("/channels/telegram")).at(-1)?.body).toMatchObject({ accountId: "telegram-1", displayName: "主 Bot", enabled: true });

    await user.click(screen.getByRole("button", { name: "移除Telegram账号" }));
    await waitFor(() => expect(getAccounts()).toHaveLength(0));
    expect(calls.some((call) => call.method === "DELETE" && call.path.endsWith("/channels/accounts/telegram-1"))).toBe(true);
  });

  it("按群保存 NapCat 旁听设置", async () => {
    const group = { chatId: "group-7", title: "测试群", listening: false };
    const { calls } = setupBackend([makeAccount("qq", "qq-1", { enabled: true, tokenConfigured: true })], [group]);
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("tab", { name: /NapCat QQ/ }));

    const checkbox = await screen.findByRole("checkbox", { name: "旁听群 测试群" });
    await user.click(checkbox);
    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path.endsWith("/groups/group-7/listening"))).toBe(true));
    expect(calls.find((call) => call.method === "PUT" && call.path.endsWith("/groups/group-7/listening"))?.body).toEqual({ enabled: true });
    expect(screen.getByText("按群旁听")).toBeInTheDocument();
  });

  it("按群清理 NapCat 旁听记录", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const group = { chatId: "group-7", title: "测试群", listening: true };
    const { calls } = setupBackend([makeAccount("qq", "qq-1", { enabled: true, tokenConfigured: true })], [group]);
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("tab", { name: /NapCat QQ/ }));
    await user.click(await screen.findByRole("button", { name: "清理旁听记录" }));
    await waitFor(() => expect(calls.some((call) => call.method === "DELETE" && call.path.endsWith("/groups/group-7/listening/messages"))).toBe(true));
    expect(await screen.findByText("旁听记录已清理")).toBeInTheDocument();
  });

  it("清理旁听记录保留其他群的当前消息", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const groups = [{ chatId: "group-7", title: "目标群", listening: true }, { chatId: "group-8", title: "当前群", listening: true }];
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [makeAccount("qq", "qq-1", { enabled: true })] } }),
      "GET /api/roles/r1/channels/threads": () => ({ body: { threads: [{ threadId: "t8", sessionKey: "thread:t8", accountId: "qq-1", channel: "qq", chatId: "group-8", chatType: "group", title: "当前群", lastActivity: "2026-10-14T00:00:00Z" }, { threadId: "t7", sessionKey: "thread:t7", accountId: "qq-1", channel: "qq", chatId: "group-7", chatType: "group", title: "目标群", lastActivity: "2026-10-14T00:00:00Z" }] } }),
      "GET /api/roles/r1/channels/accounts/qq-1/groups": () => ({ body: { groups } }),
      "GET .*channels/threads/t8/messages.*": () => ({ body: { messages: [{ id: "m8", sequence: 1, role: "user", content: "其他群的旁听消息", createdAt: "2026-10-14T00:00:00Z", metadata: { threadId: "t8", listening: true } }], hasMore: false, nextBefore: null } }),
      "DELETE /api/roles/r1/channels/accounts/qq-1/groups/group-7/listening/messages": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "Elysia" })} onBack={() => undefined} />);
    await user.click(await screen.findByRole("button", { name: /当前群/ }));
    expect(await screen.findByText("其他群的旁听消息")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /NapCat QQ/ }));
    const targetGroup = screen.getAllByText("目标群", { selector: "strong" }).at(-1)?.closest(".channel-group-option");
    await user.click(within(targetGroup as HTMLElement).getByRole("button", { name: "清理旁听记录" }));
    expect(await screen.findByText("旁听记录已清理")).toBeInTheDocument();
    expect(screen.getByText("其他群的旁听消息")).toBeInTheDocument();
  });
});
