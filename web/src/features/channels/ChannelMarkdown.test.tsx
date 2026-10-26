import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChannelView } from "./ChannelView";
import { fakeBackend, role } from "../../test/fakeBackend";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("多端消息 Markdown", () => {
  it.each([
    { channel: "qq", label: "QQ", phoneMode: true },
    { channel: "telegram", label: "Telegram", phoneMode: true },
    { channel: "qqbot", label: "官方 QQBot", phoneMode: true },
    { channel: "feishu", label: "飞书", phoneMode: true },
    { channel: "qq", label: "QQ", phoneMode: false },
  ])("$label（手机视图：$phoneMode）格式化历史和实时消息并保留引用、提及和投递状态", async ({ channel, label, phoneMode }) => {
    const sources: EventTarget[] = [];
    vi.stubGlobal("EventSource", class extends EventTarget {
      constructor() { super(); sources.push(this); }
      close() {}
    });
    fakeBackend({
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [{
        accountId: "account", roleId: "r1", channel, enabled: true, connectionStatus: "connected",
        allowPrivate: true, allowGroup: false, blockedSenders: [], displayName: "", platformId: "bot",
        lastError: null, tokenConfigured: true,
      }] } }),
      "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{
        threadId: "chat", accountId: "account", channel, chatId: "42", chatType: "private",
        title: "测试联系人", lastActivity: "2026-10-26T06:00:00Z",
      }] } }),
      "GET .*threads/chat/messages.*": () => ({ body: { messages: [
        { id: "incoming", sequence: 1, role: "user", content: "请按**今天**整理。", status: "completed", createdAt: "2026-10-26T06:00:00Z", metadata: { threadId: "chat", platformMessageId: "incoming", senderId: "owner", senderName: "阿宁", senderIsOwner: true } },
        { id: "reply", sequence: 2, role: "assistant", content: "## 渠道回复\n\n**规则：**默认今天。\n\n| 日期 | 状态 |\n| --- | --- |\n| 今天 | 待办 |", status: "completed", createdAt: "2026-10-26T06:01:00Z", metadata: { threadId: "chat", replyTo: "incoming", mentionedIds: ["owner"], deliveryStatus: "uncertain" } },
      ], hasMore: false } }),
    });
    const user = userEvent.setup();
    render(<ChannelView role={role({ id: "r1", name: "hiyori" })} phoneMode={phoneMode} onBack={() => undefined} onAccounts={() => undefined} />);
    if (phoneMode) await user.click(await screen.findByRole("button", { name: label }));
    await user.click(await screen.findByRole("button", { name: /测试联系人/ }));
    const timeline = within(screen.getByRole("region", { name: "会话消息" }));
    expect(await timeline.findByRole("heading", { name: "渠道回复", level: 2 })).toBeVisible();
    expect(timeline.getByText("今天", { selector: "strong" })).toBeVisible();
    expect(timeline.getByText("规则：", { selector: "strong" })).toBeVisible();
    expect(within(timeline.getByRole("table")).getByRole("cell", { name: "待办" })).toBeVisible();
    expect(timeline.getByText(/@阿宁/)).toBeVisible();
    expect(timeline.getByText("发送结果不确定")).toBeVisible();
    expect(timeline.getByText(/请按\*\*今天\*\*整理/)).toBeVisible();

    const live = { id: "live", sequence: 3, role: "assistant", content: "**新进度", status: "streaming", createdAt: "2026-10-26T06:02:00Z", metadata: { threadId: "chat" } };
    const emit = async (message: Record<string, unknown>) => act(async () => sources[0].dispatchEvent(new MessageEvent("channel_update", {
      data: JSON.stringify({ type: "message", roleId: "r1", threadId: "chat", message }),
    })));
    await emit(live);
    expect(await timeline.findByText("**新进度")).toBeVisible();
    await emit({ ...live, content: "**新进度：**已同步。", status: "completed", metadata: { threadId: "chat", deliveryStatus: "sent" } });
    expect(await timeline.findByText("新进度：", { selector: "strong" })).toBeVisible();
    expect(timeline.queryByText("**新进度")).not.toBeInTheDocument();
    expect(timeline.getByText("已发送")).toBeVisible();
  });
});
