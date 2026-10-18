import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, role } from "../../test/fakeBackend";

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("独立窗口授权默认关闭，保存其他配置不丢且重开一致", async () => {
  let saved = role({ id: "r1", name: "控制角色", agentConfig: { enabledTools: ["read_file"], browserUse: { enabled: true } } });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1": () => ({ body: { role: saved } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "PUT /api/roles/r1": (init) => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "控制角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "插件" }));
  await user.click(await screen.findByRole("button", { name: "桌面控制 设置" }));
  const form = within(await screen.findByRole("dialog", { name: "设置" }));
  expect(form.getByRole("checkbox", { name: "允许操作本机窗口" })).not.toBeChecked();
  expect(form.getByText(/窗口截图发送至当前聊天模型/)).toBeInTheDocument();
  await user.click(form.getByRole("checkbox", { name: "允许操作本机窗口" }));
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(saved.agentConfig?.computerUse).toEqual({ enabled: true }));
  expect(saved.agentConfig?.browserUse).toEqual({ enabled: true });
  expect(saved.agentConfig?.enabledTools).toEqual(["read_file"]);
  await user.click(form.getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "插件" }));
  await user.click(await screen.findByRole("button", { name: "桌面控制 设置" }));
  expect(within(await screen.findByRole("dialog", { name: "设置" })).getByRole("checkbox", { name: "允许操作本机窗口" })).toBeChecked();
});
