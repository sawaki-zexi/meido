import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import type { Account } from "./channelAccounts";
import { GroupSettingsPanel } from "./GroupSettingsPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const account: Account = { accountId: "qq-1", channel: "qq", enabled: true, platformEnabled: true, allowPrivate: true, allowGroup: true, blockedSenders: [], displayName: "角色QQ", platformId: "123", lastError: null, tokenConfigured: true, connectionStatus: "connected" };
const group = { chatId: "42", title: "读书群", threadId: null as string | null, repliesEnabled: true, listening: false, naturalEnabled: false, naturalEffective: false };
const base = "/api/roles/r1/channels/accounts/qq-1/groups";

it("按群关闭全部回复并保留旁听和主动接话偏好，其他群不受影响", async () => {
  let state = { ...group, listening: true, naturalEnabled: true, naturalEffective: true };
  const { calls } = fakeBackend({
    [`GET ${base}`]: () => ({ body: { groups: [state, { ...group, chatId: "43", title: "另一个群" }] } }),
    [`GET ${base}/42/settings`]: () => ({ body: state }),
    [`GET ${base}/43/settings`]: () => ({ body: { ...group, chatId: "43", title: "另一个群" } }),
    [`PUT ${base}/42/replies`]: () => ({ body: state = { ...state, repliesEnabled: !state.repliesEnabled, naturalEffective: !state.repliesEnabled } }),
  });
  const user = userEvent.setup();
  render(<GroupSettingsPanel roleId="r1" account={account} initialGroup={group} onBack={() => undefined} />);
  const toggle = await screen.findByRole("checkbox", { name: "允许本群回复" });
  expect(toggle).toBeChecked();
  await user.click(toggle);
  await waitFor(() => expect(toggle).not.toBeChecked());
  expect(screen.getByText(/已关闭 · @、引用和主动接话均不发送/)).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "开启本群旁听" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "群聊主动接话（无需 @）" })).toBeChecked();
  expect(screen.getByText("已保存开启 · 当前未生效")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "选择其他群聊" }));
  await user.click(await screen.findByRole("button", { name: /另一个群/ }));
  expect(await screen.findByRole("checkbox", { name: "允许本群回复" })).toBeChecked();
  expect(calls.filter(call => call.method === "PUT").map(call => [call.path, call.body])).toEqual([[`${base}/42/replies`, { enabled: false }]]);
});

it("目录中的首次群聊无需制造聊天记录即可开启旁听和主动接话", async () => {
  let state = { ...group };
  const { calls } = fakeBackend({
    [`GET ${base}`]: () => ({ body: { groups: [state] } }),
    [`GET ${base}/42/settings`]: () => ({ body: state }),
    [`PUT ${base}/42/listening`]: () => ({ body: state = { ...state, listening: true, threadId: "t1" } }),
    [`PUT ${base}/42/natural`]: () => ({ body: state = { ...state, naturalEnabled: true, naturalEffective: true } }),
    "GET .*threads/t1/listening": () => ({ body: { enabled: true, dailyCap: null, defaultDailyCap: 200, toggles: [] } }),
    "GET .*threads/t1/listening/messages": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
  });
  const user = userEvent.setup();
  render(<GroupSettingsPanel roleId="r1" account={account} onBack={() => undefined} />);
  await user.click(await screen.findByRole("button", { name: /读书群.*42/ }));
  expect(await screen.findByRole("checkbox", { name: "群聊主动接话（无需 @）" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: "开启本群旁听" }));
  await user.click(await screen.findByRole("checkbox", { name: "群聊主动接话（无需 @）" }));
  expect(await screen.findByText("已开启 · 当前可主动接话")).toBeVisible();
  expect(calls.filter(call => call.method === "PUT").map(call => call.path)).toEqual([`${base}/42/listening`, `${base}/42/natural`]);
  expect(calls.some(call => call.path.endsWith("/messages") && call.method !== "GET")).toBe(false);
});

it("空目录提供手动群号入口且读取不写入任何开关", async () => {
  const { calls } = fakeBackend({
    [`GET ${base}`]: () => ({ body: { groups: [], directoryWarning: "账号离线，当前显示已知群聊。" } }),
    [`GET ${base}/999/settings`]: () => ({ body: { ...group, chatId: "999", title: "999" } }),
  });
  const user = userEvent.setup();
  render(<GroupSettingsPanel roleId="r1" account={account} onBack={() => undefined} />);
  expect(await screen.findByText(/暂无已知群聊/)).toBeVisible();
  await user.type(screen.getByRole("textbox", { name: "群号" }), "999");
  await user.click(screen.getByRole("button", { name: "打开群聊设置" }));
  expect(await screen.findByRole("checkbox", { name: "群聊主动接话（无需 @）" })).not.toBeChecked();
  expect(calls.every(call => call.method === "GET")).toBe(true);
});

it.each([
  [{ allowGroup: false }, "账号未开启“响应群聊”", "打开响应规则"],
  [{ enabled: false }, "账号已暂停", "打开账号设置"],
  [{ platformEnabled: false }, "QQ 平台已暂停", "打开平台设置"],
  [{ connectionStatus: "disconnected" }, "账号离线", "打开账号设置"],
])("保存的开关与实际生效原因分开显示，始终允许关闭：%j", async (overrides, reason, repair) => {
  const onAccount = vi.fn(); const onPlatform = vi.fn();
  const { calls } = fakeBackend({
    [`GET ${base}`]: () => ({ body: { groups: [group] } }),
    [`GET ${base}/42/settings`]: () => ({ body: { ...group, listening: true, naturalEnabled: true } }),
    [`PUT ${base}/42/natural`]: () => ({ body: { ...group, listening: true, naturalEnabled: false } }),
  });
  const user = userEvent.setup();
  render(<GroupSettingsPanel roleId="r1" account={{ ...account, ...overrides } as Account} initialGroup={group} onBack={() => undefined} onAccountSettings={onAccount} onOpenSettings={onPlatform} />);
  const toggle = await screen.findByRole("checkbox", { name: "群聊主动接话（无需 @）" });
  expect(toggle).toBeChecked(); expect(toggle).toBeEnabled();
  expect(screen.getByText(new RegExp(reason))).toBeVisible();
  await user.click(screen.getByRole("button", { name: repair }));
  expect(repair === "打开平台设置" ? onPlatform : onAccount).toHaveBeenCalledOnce();
  await user.click(toggle);
  await waitFor(() => expect(toggle).not.toBeChecked());
  expect(calls.find(call => call.method === "PUT")?.body).toEqual({ enabled: false });
});

it("未允许响应群聊时禁止开启但说明如何修复；离线不是权限拒绝", async () => {
  fakeBackend({
    [`GET ${base}`]: () => ({ body: { groups: [group] } }),
    [`GET ${base}/42/settings`]: () => ({ body: { ...group, listening: true } }),
  });
  const view = render(<GroupSettingsPanel roleId="r1" account={{ ...account, allowGroup: false }} initialGroup={group} onBack={() => undefined} />);
  expect(await screen.findByRole("checkbox", { name: "群聊主动接话（无需 @）" })).toBeDisabled();
  view.rerender(<GroupSettingsPanel roleId="r1" account={{ ...account, connectionStatus: "disconnected" }} initialGroup={group} onBack={() => undefined} />);
  expect(screen.getByRole("checkbox", { name: "群聊主动接话（无需 @）" })).toBeEnabled();
});

it("保存失败保留已确认状态，连点只发出一次写请求", async () => {
  let resolve: (value: { status: number; body: unknown }) => void = () => undefined;
  const pending = new Promise<{ status: number; body: unknown }>(done => { resolve = done; });
  const { calls } = fakeBackend({
    [`GET ${base}`]: () => ({ body: { groups: [group] } }),
    [`GET ${base}/42/settings`]: () => ({ body: { ...group, listening: true } }),
    [`PUT ${base}/42/natural`]: () => pending,
  });
  render(<GroupSettingsPanel roleId="r1" account={account} initialGroup={group} onBack={() => undefined} />);
  const toggle = await screen.findByRole("checkbox", { name: "群聊主动接话（无需 @）" });
  fireEvent.click(toggle); fireEvent.click(toggle);
  expect(calls.filter(call => call.method === "PUT")).toHaveLength(1);
  await act(async () => { resolve({ status: 500, body: { detail: "保存失败，请稍后重试" } }); await pending; });
  expect(await screen.findByText("保存失败，请稍后重试")).toBeVisible();
  expect(toggle).not.toBeChecked(); expect(toggle).toBeEnabled();
});

it("切换群聊或角色丢弃旧保存结果与错误", async () => {
  let resolve: (value: { body: unknown }) => void = () => undefined;
  const pending = new Promise<{ body: unknown }>(done => { resolve = done; });
  fakeBackend({
    [`GET ${base}`]: () => ({ body: { groups: [group, { ...group, chatId: "43", title: "另一个群" }] } }),
    [`GET ${base}/42/settings`]: () => ({ body: { ...group, listening: true } }),
    [`GET ${base}/43/settings`]: () => ({ body: { ...group, chatId: "43", title: "另一个群" } }),
    [`PUT ${base}/42/natural`]: () => pending,
    "GET /api/roles/r2/channels/accounts/qq-2/groups": () => ({ body: { groups: [] } }),
  });
  const user = userEvent.setup();
  const view = render(<GroupSettingsPanel roleId="r1" account={account} initialGroup={group} onBack={() => undefined} />);
  await user.click(await screen.findByRole("checkbox", { name: "群聊主动接话（无需 @）" }));
  await user.click(screen.getByRole("button", { name: "选择其他群聊" }));
  await user.click(await screen.findByRole("button", { name: /另一个群/ }));
  expect(await screen.findByRole("checkbox", { name: "群聊主动接话（无需 @）" })).not.toBeChecked();
  await act(async () => { resolve({ body: { ...group, listening: true, naturalEnabled: true } }); await pending; });
  expect(screen.getByRole("checkbox", { name: "群聊主动接话（无需 @）" })).not.toBeChecked();
  view.rerender(<GroupSettingsPanel roleId="r2" account={{ ...account, accountId: "qq-2" }} onBack={() => undefined} />);
  expect(await screen.findByText(/暂无已知群聊/)).toBeVisible();
  expect(screen.queryByRole("checkbox", { name: "群聊主动接话（无需 @）" })).not.toBeInTheDocument();
});

it.each([
  { connectionStatus: "disconnected" }, { allowGroup: false }, { enabled: false }, { platformEnabled: false },
])("账号恢复后立即更新有效状态，不被旧的 naturalEffective=false 卡住：%j", async overrides => {
  fakeBackend({
    [`GET ${base}`]: () => ({ body: { groups: [group] } }),
    [`GET ${base}/42/settings`]: () => ({ body: { ...group, listening: true, naturalEnabled: true, naturalEffective: false } }),
  });
  const view = render(<GroupSettingsPanel roleId="r1" account={{ ...account, ...overrides } as Account} initialGroup={group} onBack={() => undefined} />);
  expect(await screen.findByText("已保存开启 · 当前未生效")).toBeVisible();
  view.rerender(<GroupSettingsPanel roleId="r1" account={account} initialGroup={group} onBack={() => undefined} />);
  expect(screen.getByText("已开启 · 当前可主动接话")).toBeVisible();
});

it("无效手动群号不能请求设置，已移除账号只能查看历史开关", async () => {
  const { calls } = fakeBackend({
    [`GET ${base}`]: () => ({ body: { groups: [] } }),
    "GET /api/roles/r1/channels/threads/old/listening": () => ({ body: { enabled: true, naturalEnabled: true, dailyCap: null, defaultDailyCap: 200, toggles: [] } }),
    "GET /api/roles/r1/channels/threads/old/listening/messages": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
  });
  const user = userEvent.setup();
  const view = render(<GroupSettingsPanel roleId="r1" account={account} onBack={() => undefined} />);
  await user.type(screen.getByRole("textbox", { name: "群号" }), "abc/42");
  expect(screen.getByRole("button", { name: "打开群聊设置" })).toBeDisabled();
  expect(calls.filter(call => call.path.endsWith("/settings"))).toHaveLength(0);
  view.rerender(<GroupSettingsPanel roleId="r1" account={null} initialGroup={{ ...group, threadId: "old" }} onBack={() => undefined} />);
  expect(await screen.findByText(/账号已移除，历史设置仅供查看/)).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "群聊主动接话（无需 @）" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "群聊主动接话（无需 @）" })).toBeDisabled();
  expect(calls.every(call => call.method === "GET")).toBe(true);
});
