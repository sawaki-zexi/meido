import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import { GroupListeningPanel } from "./GroupListeningPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const heard = (sequence: number, content: string) => ({
  id: `heard-${sequence}`, sequence, content, createdAt: "2026-10-15T00:00:00Z", metadata: { senderName: "群友" },
});
const settings = { enabled: true, dailyCap: 200, defaultDailyCap: 200, toggles: [] };

it("当前群内启停与清理旁听，不展示其他群配置", async () => {
  let enabled = false;
  const { calls } = fakeBackend({
    "GET .*threads/t1/listening": () => ({ body: { ...settings, enabled } }),
    "GET .*threads/t1/listening/messages": () => ({ body: { messages: [heard(1, "待清理消息")], hasMore: false, nextBefore: null } }),
    "PUT /api/roles/r1/channels/accounts/qq-1/groups/group-7/listening": () => { enabled = true; return { body: { listening: true } }; },
    "DELETE /api/roles/r1/channels/accounts/qq-1/groups/group-7/listening/messages": () => ({ status: 204 }),
  });
  vi.stubGlobal("confirm", () => true);
  const user = userEvent.setup();
  render(<GroupListeningPanel roleId="r1" threadId="t1" accountId="qq-1" chatId="group-7" editable />);
  await user.click(screen.getByText(/群旁听记录/));
  await user.click(await screen.findByRole("checkbox", { name: "开启本群旁听" }));
  expect(await screen.findByRole("checkbox", { name: "开启本群旁听" })).toBeChecked();
  await user.click(screen.getByRole("button", { name: "清理本群旁听记录" }));
  expect(screen.queryByText("待清理消息")).not.toBeInTheDocument();
  expect(calls.filter((call) => call.method === "PUT").at(-1)?.body).toEqual({ enabled: true });
});

function events() {
  const sources: TestEventSource[] = [];
  class TestEventSource extends EventTarget {
    close = vi.fn();
    constructor(public url: string) { super(); sources.push(this); }
  }
  vi.stubGlobal("EventSource", TestEventSource);
  return {
    sources,
    async emit(payload: Record<string, unknown>) {
      await act(async () => { sources[0].dispatchEvent(new MessageEvent("channel_update", { data: JSON.stringify(payload) })); });
    },
  };
}

it("实时刷新保留已加载的旧旁听分页和正在编辑的每日上限", async () => {
  let latest = [heard(51, "当前旁听")];
  const source = events();
  const { calls } = fakeBackend({
    "GET .*threads/t1/listening": () => ({ body: settings }),
    "GET .*threads/t1/listening/messages": () => ({ body: { messages: latest, hasMore: true, nextBefore: 51 } }),
    "GET .*threads/t1/listening/messages\\?before=51": () => ({ body: { messages: [heard(1, "更早旁听")], hasMore: false, nextBefore: 1 } }),
  });
  const user = userEvent.setup();
  render(<GroupListeningPanel roleId="r1" threadId="t1" editable />);
  await user.click(screen.getByText(/群旁听记录/));
  await user.click(await screen.findByRole("button", { name: "读取更早旁听记录" }));
  expect(await screen.findByText("更早旁听")).toBeVisible();
  const cap = screen.getByRole("spinbutton", { name: "本群每日旁听上限" });
  await user.clear(cap); await user.type(cap, "350");
  latest = [heard(51, "当前旁听"), heard(52, "新增旁听")];
  await source.emit({ type: "listening_heard", threadId: "t1" });
  expect(await screen.findByText("新增旁听")).toBeVisible();
  expect(screen.getByText("更早旁听")).toBeVisible();
  expect(cap).toHaveValue(350);
  expect(screen.queryByRole("button", { name: "读取更早旁听记录" })).not.toBeInTheDocument();
  expect(calls.filter((call) => call.path.endsWith("/listening/messages"))).toHaveLength(2);
});

it("空记录收到实时刷新时也保留正在编辑的上限", async () => {
  const source = events();
  const { calls } = fakeBackend({
    "GET .*threads/t1/listening": () => ({ body: settings }),
    "GET .*threads/t1/listening/messages": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
  });
  const user = userEvent.setup();
  render(<GroupListeningPanel roleId="r1" threadId="t1" editable />);
  await user.click(screen.getByText(/群旁听记录/));
  const cap = await screen.findByRole("spinbutton", { name: "本群每日旁听上限" });
  await user.clear(cap); await user.type(cap, "350");
  await source.emit({ type: "avatars_updated" });
  await waitFor(() => expect(calls.filter((call) => call.path.endsWith("/listening/messages"))).toHaveLength(2));
  expect(cap).toHaveValue(350);
});

it("清理事件清空当前群记录并丢弃清理前仍在途的旧分页", async () => {
  const source = events();
  let cleared = false;
  let resolveOlder: ((reply: { body: unknown }) => void) | undefined;
  const older = new Promise<{ body: unknown }>((resolve) => { resolveOlder = resolve; });
  fakeBackend({
    "GET .*threads/t1/listening": () => ({ body: settings }),
    "GET .*threads/t1/listening/messages": () => ({ body: { messages: cleared ? [] : [heard(51, "待清理旁听")], hasMore: !cleared, nextBefore: cleared ? null : 51 } }),
    "GET .*threads/t1/listening/messages\\?before=51": () => older,
  });
  const user = userEvent.setup();
  render(<GroupListeningPanel roleId="r1" threadId="t1" editable />);
  await user.click(screen.getByText(/群旁听记录/));
  await user.click(await screen.findByRole("button", { name: "读取更早旁听记录" }));
  cleared = true;
  await source.emit({ type: "listening_cleared", threadId: "t1" });
  await act(async () => { resolveOlder?.({ body: { messages: [heard(1, "已清理的旧分页")], hasMore: false, nextBefore: 1 } }); await older; });
  expect(screen.queryByText("待清理旁听")).not.toBeInTheDocument();
  expect(screen.queryByText("已清理的旧分页")).not.toBeInTheDocument();
  expect(screen.getByText("暂无旁听记录")).toBeVisible();
  expect(screen.queryByRole("button", { name: "读取更早旁听记录" })).not.toBeInTheDocument();
});

it("日常聊天默认关闭，仅开启旁听后可启用，并保存当前群开关", async () => {
  let naturalEnabled = false;
  const { calls } = fakeBackend({
    "GET .*threads/t1/listening": () => ({ body: { ...settings, naturalEnabled } }),
    "GET .*threads/t1/listening/messages": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
    "PUT .*threads/t1/listening/natural": () => { naturalEnabled = true; return { body: { ...settings, naturalEnabled } }; },
  });
  const user = userEvent.setup();
  render(<GroupListeningPanel roleId="r1" threadId="t1" accountId="qq-1" chatId="g1" editable />);
  await user.click(screen.getByText(/群旁听记录/));
  const toggle = await screen.findByRole("checkbox", { name: "开启本群日常聊天" });
  expect(toggle).not.toBeChecked();
  await user.click(toggle);
  expect(toggle).toBeChecked();
  expect(calls.filter(call => call.method === "PUT")).toEqual([{ method: "PUT", path: "/api/roles/r1/channels/threads/t1/listening/natural", body: { enabled: true } }]);
});

it("旁听关闭保留日常聊天选择并显示暂停，未保存开启时不能直接开启", async () => {
  let naturalEnabled = true;
  fakeBackend({
    "GET .*threads/t1/listening": () => ({ body: { ...settings, enabled: false, naturalEnabled, naturalPaused: naturalEnabled } }),
    "GET .*threads/t1/listening/messages": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
    "PUT .*threads/t1/listening/natural": () => { naturalEnabled = false; return { body: { ...settings, enabled: false, naturalEnabled } }; },
  });
  const user = userEvent.setup();
  render(<GroupListeningPanel roleId="r1" threadId="t1" accountId="qq-1" chatId="g1" editable />);
  await user.click(screen.getByText(/群旁听记录/));
  expect(await screen.findByText("日常聊天已暂停：请先开启本群旁听。" )).toBeVisible();
  const toggle = screen.getByRole("checkbox", { name: "开启本群日常聊天" });
  expect(toggle).toBeChecked();
  await user.click(toggle);
  expect(toggle).not.toBeChecked();
  expect(toggle).toBeDisabled();
});

it("切换群时丢弃旧日常聊天保存回执，新群仍可操作", async () => {
  let finish: ((reply: { body: unknown }) => void) | undefined;
  const pending = new Promise<{ body: unknown }>(resolve => { finish = resolve; });
  const { calls } = fakeBackend({
    "GET .*threads/t[12]/listening": () => ({ body: { ...settings, naturalEnabled: false } }),
    "GET .*threads/t[12]/listening/messages": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
    "PUT .*threads/t1/listening/natural": () => pending,
  });
  const user = userEvent.setup();
  const view = render(<GroupListeningPanel roleId="r1" threadId="t1" accountId="qq-1" chatId="g1" editable />);
  await user.click(screen.getByText(/群旁听记录/));
  const toggle = await screen.findByRole("checkbox", { name: "开启本群日常聊天" });
  await user.dblClick(toggle);
  expect(calls.filter(call => call.method === "PUT")).toHaveLength(1);
  view.rerender(<GroupListeningPanel roleId="r1" threadId="t2" accountId="qq-1" chatId="g2" editable />);
  await waitFor(() => expect(screen.getByRole("checkbox", { name: "开启本群日常聊天" })).toBeEnabled());
  await act(async () => { finish?.({ body: { ...settings, naturalEnabled: true } }); await pending; });
  expect(screen.getByRole("checkbox", { name: "开启本群日常聊天" })).not.toBeChecked();
});

it("日常聊天保存失败保留旧选择并允许重试，只读入口不能修改", async () => {
  let failed = true;
  fakeBackend({
    "GET .*threads/t1/listening": () => ({ body: { ...settings, naturalEnabled: false } }),
    "GET .*threads/t1/listening/messages": () => ({ body: { messages: [], hasMore: false, nextBefore: null } }),
    "PUT .*threads/t1/listening/natural": () => failed ? { status: 409, body: { detail: "请先开启本群旁听" } } : { body: { ...settings, naturalEnabled: true } },
  });
  const user = userEvent.setup();
  const view = render(<GroupListeningPanel roleId="r1" threadId="t1" editable />);
  await user.click(screen.getByText(/群旁听记录/));
  const toggle = await screen.findByRole("checkbox", { name: "开启本群日常聊天" });
  await user.click(toggle);
  expect(await screen.findByRole("status")).toHaveTextContent("请先开启本群旁听");
  expect(toggle).not.toBeChecked();
  expect(toggle).toBeEnabled();
  failed = false;
  await user.click(toggle);
  expect(toggle).toBeChecked();
  view.rerender(<GroupListeningPanel roleId="r1" threadId="t1" editable={false} />);
  expect(toggle).toBeDisabled();
});
