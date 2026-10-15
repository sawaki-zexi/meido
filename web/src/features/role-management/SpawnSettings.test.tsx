import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, role } from "../../test/fakeBackend";

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("网页独立启用委托并保存有限预算，重新打开和关闭时保留已有工具及配置", async () => {
  const spawn = { enabled: false, timeoutSeconds: 120, maxTurns: 12, maxOutputChars: 20000, maxModelOutputChars: 60000, extension: "keep" };
  let saved = role({ id: "r1", name: "子任务角色", agentConfig: {
    enabledTools: ["read_file", "web_search", "spawn"], enabledPlugins: ["custom"], grantedCapabilities: ["custom.read"],
    spawn,
  } });
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "子任务角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const form = within(await screen.findByRole("dialog", { name: "角色资料" }));
  expect(form.getByLabelText("允许委托独立子任务")).not.toBeChecked();
  await user.click(form.getByLabelText("允许委托独立子任务"));
  await user.clear(form.getByLabelText("子任务超时（秒）"));
  await user.type(form.getByLabelText("子任务超时（秒）"), "45");
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1));
  expect(saved.agentConfig).toMatchObject({ enabledTools: ["read_file", "web_search", "spawn"],
    enabledPlugins: ["custom"], grantedCapabilities: ["custom.read"],
    spawn: { enabled: true, timeoutSeconds: 45, maxTurns: 12, maxOutputChars: 20000, maxModelOutputChars: 60000, extension: "keep" } });
  await user.click(form.getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const reopened = within(await screen.findByRole("dialog", { name: "角色资料" }));
  expect(reopened.getByLabelText("允许委托独立子任务")).toBeChecked();
  expect(reopened.getByLabelText("子任务超时（秒）")).toHaveValue(45);
  await user.click(reopened.getByLabelText("允许委托独立子任务"));
  await user.click(reopened.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PUT")).toHaveLength(2));
  expect(saved.agentConfig?.spawn?.enabled).toBe(false);
  expect(saved.agentConfig?.enabledTools).toEqual(["read_file", "web_search", "spawn"]);
});
