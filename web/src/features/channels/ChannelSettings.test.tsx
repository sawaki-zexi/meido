import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import { ChannelSettings } from "./ChannelSettings";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("按平台暂停渠道并保留账号数", async () => {
  let enabled = true;
  const state = () => ({ channels: [{ channel: "telegram", name: "Telegram", enabled, configuredAccounts: 2, connectedAccounts: enabled ? 1 : 0 }] });
  const { calls } = fakeBackend({
    "GET /api/channels/settings": () => ({ body: state() }),
    "POST /api/channels/settings": () => { enabled = false; return { body: state() }; },
  });
  const user = userEvent.setup();
  render(<ChannelSettings />);
  const toggle = await screen.findByRole("checkbox", { name: "启用 Telegram 渠道" });
  expect(toggle).toBeChecked();
  await user.click(toggle);
  await waitFor(() => expect(toggle).not.toBeChecked());
  expect(screen.getByText(/2 个账号/)).toBeVisible();
  expect(calls.find((call) => call.method === "POST")?.body).toEqual({ channel: "telegram", enabled: false });
});

it("停止连接失败时重新读取已保存的平台暂停状态并给出错误", async () => {
  let enabled = true;
  const state = () => ({ channels: [{ channel: "qq", name: "QQ", enabled, configuredAccounts: 1, connectedAccounts: 0 }] });
  fakeBackend({
    "GET /api/channels/settings": () => ({ body: state() }),
    "POST /api/channels/settings": () => { enabled = false; return { status: 503, body: { detail: "部分账号停止失败" } }; },
  });
  render(<ChannelSettings />);
  await userEvent.click(await screen.findByRole("checkbox", { name: "启用 QQ 渠道" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("部分账号停止失败");
  expect(screen.getByRole("checkbox", { name: "启用 QQ 渠道" })).not.toBeChecked();
});
