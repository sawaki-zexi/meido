import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import { IdentityBindings } from "./IdentityBindings";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows platform owner bindings across accounts and lets the user revoke them", async () => {
  const { calls } = fakeBackend({
    "GET /api/channels/identities": () => ({ body: { identities: [
      { identityId: "global-owner", channel: "qq", scope: "platform", platformUserId: "12345", accountId: null },
      { identityId: "other-channel", channel: "feishu", scope: "account", platformUserId: "private-id", accountId: "feishu-2" },
    ] } }),
    "DELETE /api/channels/identities/global-owner": () => ({ status: 204 }),
  });
  render(<IdentityBindings channel="qq" accountId="qq-2" />);
  expect(await screen.findByText("12345")).toBeVisible();
  expect(screen.queryByText("private-id")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "解绑主人 12345" }));
  await waitFor(() => expect(screen.queryByText("12345")).not.toBeInTheDocument());
  expect(calls.some((call) => call.method === "DELETE" && call.path === "/api/channels/identities/global-owner")).toBe(true);
});

it("shows application bindings only for the receiving account", async () => {
  fakeBackend({
    "GET /api/channels/identities": () => ({ body: { identities: [
      { identityId: "own", channel: "feishu", scope: "account", platformUserId: "own-user", accountId: "feishu-1" },
      { identityId: "other", channel: "feishu", scope: "account", platformUserId: "other-user", accountId: "feishu-2" },
    ] } }),
  });
  render(<IdentityBindings channel="feishu" accountId="feishu-1" />);
  expect(await screen.findByText("own-user")).toBeVisible();
  expect(screen.queryByText("other-user")).not.toBeInTheDocument();
});
