import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, role } from "../../test/fakeBackend";

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("从角色卡进入 Shiori 风格工作区，提供记忆、能力和账号页且不显示好感度", async () => {
  const alice = role({ id: "r1", name: "爱丽丝", description: "安静的女仆" });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [alice] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/roles/r1/memories.*": () => ({ body: { memories: [], total: 0 } }),
    "GET /api/roles/r1/tools": () => ({ body: { roleId: "r1", sessionKey: "role:r1", preview: true, tools: [], plugins: [] } }),
    "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
    "GET /api/channels/settings": () => ({ body: { channels: [] } }),
    "GET /api/roles/r1/channels/events": () => ({ stream: [] }),
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "角色" }));
  await user.click(await screen.findByRole("button", { name: "查看爱丽丝工作区" }));

  const workspace = await screen.findByRole("region", { name: "爱丽丝的角色工作区" });
  expect(within(workspace).getByRole("tab", { name: "资料" })).toBeInTheDocument();
  expect(within(workspace).getByRole("tab", { name: "记忆" })).toBeInTheDocument();
  expect(within(workspace).getByRole("tab", { name: "能力" })).toBeInTheDocument();
  expect(within(workspace).getByRole("tab", { name: "账号" })).toBeInTheDocument();
  expect(within(workspace).queryByRole("tab", { name: "好感度" })).not.toBeInTheDocument();

  await user.click(within(workspace).getByRole("tab", { name: "记忆" }));
  expect(await within(workspace).findByRole("region", { name: "爱丽丝的记忆" })).toBeInTheDocument();
  await user.click(within(workspace).getByRole("tab", { name: "能力" }));
  expect(await within(workspace).findByRole("region", { name: "角色能力" })).toBeInTheDocument();
});
