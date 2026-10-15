import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import { PushTargets } from "./PushTargets";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("只向显式选择的已配对主人投递提醒并能取消", async () => {
  let enabled = false;
  const destination = () => ({ accountId: "qq-1", targetId: "target-owner", displayName: "主人QQ", enabled, available: true });
  const { calls } = fakeBackend({
    "GET /api/roles/r1/channels/push-targets": () => ({ body: { targets: [destination(), { accountId: "telegram-1", targetId: "other", displayName: "另一个账号", enabled: false, available: true }] } }),
    "PUT /api/roles/r1/channels/accounts/qq-1/push-target": () => {
      enabled = (calls.at(-1)?.body as { targetId: string | null }).targetId !== null;
      return { body: { targets: [destination()] } };
    },
  });
  const user = userEvent.setup();
  render(<PushTargets roleId="r1" accountId="qq-1" />);
  const checkbox = await screen.findByRole("checkbox", { name: "主人QQ" });
  expect(checkbox).not.toBeChecked();
  expect(screen.queryByText("另一个账号")).not.toBeInTheDocument();
  await user.click(checkbox);
  await waitFor(() => expect(checkbox).toBeChecked());
  await user.click(checkbox);
  await waitFor(() => expect(checkbox).not.toBeChecked());
  expect(calls.filter((call) => call.method === "PUT").map((call) => call.body)).toEqual([{ targetId: "target-owner" }, { targetId: null }]);
});

it("未配对时显示需要先配对的提示", async () => {
  fakeBackend({ "GET /api/roles/r1/channels/push-targets": () => ({ body: { targets: [] } }) });
  render(<PushTargets roleId="r1" accountId="qq-1" />);
  expect(await screen.findByText("请先在此账号私聊配对，再刷新目标。")).toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
});

it("会话设置只显示当前私聊的提醒目标", async () => {
  fakeBackend({ "GET /api/roles/r1/channels/push-targets": () => ({ body: { targets: [
    { accountId: "qq-1", targetId: "here", chatId: "42", displayName: "当前私聊", enabled: true, available: true },
    { accountId: "qq-1", targetId: "other", chatId: "99", displayName: "其他私聊", enabled: false, available: true },
  ] } }) });
  render(<PushTargets roleId="r1" accountId="qq-1" chatId="42" />);
  expect(await screen.findByRole("checkbox", { name: "当前私聊" })).toBeChecked();
  expect(screen.queryByText("其他私聊")).not.toBeInTheDocument();
});
