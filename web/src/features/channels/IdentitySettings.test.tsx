import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import { IdentitySettings } from "./IdentitySettings";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it("主人无需选择角色或账号即可生成并复制全局配对码", async () => {
  const user = userEvent.setup();
  const { calls } = fakeBackend({
    "GET /api/channels/identities": () => ({ body: { identities: [] } }),
    "POST /api/channels/pairing": () => ({ body: { code: "ABC23456", expiresAt: new Date(Date.now() + 600_000).toISOString() } }),
  });
  render(<IdentitySettings />);
  await user.click(screen.getByRole("button", { name: "生成配对码" }));
  expect(await screen.findByText("ABC23456")).toBeVisible();
  expect(screen.getByRole("timer", { name: "配对码剩余时间" })).toHaveTextContent(/10:00|09:59/);
  await user.click(screen.getByRole("button", { name: "复制配对码" }));
  expect(await navigator.clipboard.readText()).toBe("ABC23456");
  expect(calls.find((call) => call.method === "POST")?.path).toBe("/api/channels/pairing");
});

it("过期码停止展示与复制，重新生成失败也保留可重试反馈", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  const user = userEvent.setup();
  let attempts = 0;
  fakeBackend({
    "GET /api/channels/identities": () => ({ body: { identities: [] } }),
    "POST /api/channels/pairing": () => ++attempts === 1 ? { body: { code: "ABC23456", expiresAt: new Date(Date.now() + 2000).toISOString() } } : { status: 503, body: { detail: "暂时无法生成" } },
  });
  render(<IdentitySettings />);
  await user.click(screen.getByRole("button", { name: "生成配对码" }));
  expect(screen.getByText("ABC23456")).toBeVisible();
  await act(async () => { vi.advanceTimersByTime(3000); });
  expect(screen.queryByText("ABC23456")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "复制配对码" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重新生成配对码" }));
  expect(screen.getByText("暂时无法生成")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新生成配对码" })).toBeEnabled();
});

it("配对成功后显示新绑定并立即收起已消费的码", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  const user = userEvent.setup();
  let paired = false;
  fakeBackend({
    "GET /api/channels/identities": () => ({ body: { identities: paired ? [{ identityId: "owner", channel: "qq", scope: "platform", platformUserId: "12345", accountId: null, pairedAt: "2026-10-15T00:00:00Z" }] : [] } }),
    "POST /api/channels/pairing": () => ({ body: { code: "ABC23456", expiresAt: new Date(Date.now() + 600_000).toISOString() } }),
  });
  render(<IdentitySettings />);
  await user.click(screen.getByRole("button", { name: "生成配对码" }));
  paired = true;
  await act(async () => { vi.advanceTimersByTime(3000); });
  expect(screen.getByText("12345")).toBeVisible();
  expect(screen.queryByText("ABC23456")).not.toBeInTheDocument();
});
