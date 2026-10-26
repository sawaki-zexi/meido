import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ChannelView } from "./ChannelView";
import { fakeBackend, role } from "../../test/fakeBackend";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("手机显示确认段、未知段和未发送尾部，保留原生成内容供核对", async () => {
  const message = { id: "m", sequence: 1, role: "assistant", content: "确认正文未知正文未发正文", status: "completed",
    createdAt: "2026-10-26T06:00:00Z", metadata: { threadId: "chat", deliveryStatus: "uncertain", partialDelivery: true,
      deliveredContent: "确认正文", parts: [
        { kind: "text", content: "确认正文", status: "sent", platformMessageId: "receipt-1" },
        { kind: "text", content: "未知正文", status: "uncertain", error: "TimeoutError" },
        { kind: "text", content: "未发正文", status: "pending" },
      ] } };
  const account = { accountId: "tg", roleId: "r1", channel: "telegram", enabled: true, connectionStatus: "connected", displayName: "测试" };
  fakeBackend({
    "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [account] } }),
    "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "chat", accountId: "tg", channel: "telegram", chatId: "1", chatType: "private", title: "投递测试", lastActivity: message.createdAt, latestMessage: message }] } }),
    "GET .*threads/chat/messages.*": () => ({ body: { messages: [message], hasMore: false } }),
  });
  const user = userEvent.setup();
  render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} />);
  await user.click(await screen.findByRole("button", { name: "Telegram" }));
  await user.click(screen.getByRole("button", { name: /投递测试/ }));
  const timeline = within(screen.getByRole("region", { name: "会话消息" }));
  expect(await timeline.findByText("部分已发送 · 其余结果不确定")).toBeVisible();
  await user.click(timeline.getByText("分段送达详情"));
  expect(timeline.getByText("第 1 段 · 已发送")).toBeVisible();
  expect(timeline.getByText("第 2 段 · 结果不确定")).toBeVisible();
  expect(timeline.getByText("第 3 段 · 未发送")).toBeVisible();
  expect(timeline.getByText("确认正文")).toBeVisible();
  expect(timeline.getByText("未发正文")).toBeVisible();
  expect(timeline.queryByText("回复失败")).not.toBeInTheDocument();
});
