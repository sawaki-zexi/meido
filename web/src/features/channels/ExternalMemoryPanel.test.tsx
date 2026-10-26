import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { fakeBackend, role } from "../../test/fakeBackend";
import { ExternalMemoryPanel } from "./ExternalMemoryPanel";
import { ChannelView } from "./ChannelView";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const base = "/api/roles/r1/channels/threads/t1/external-memory";
const member = { key: "person", channel: "qq", scope: "platform", senderId: "22", name: "阿宁", nicknames: ["阿宁", "新昵称"],
  profile: "爱读书", shorthand: "书友", state: "published", revision: 1, readOnly: false,
  sources: [{ threadId: "t1", title: "读书群" }, { threadId: "t2", title: "茶友群" }], candidates: [] };
const data = { threadId: "t1", note: "原笔记", activity: { text: "聊了新书", updatedAt: "2026-10-26T01:00:00Z" }, revision: 1, readOnly: false, members: [member] };

it("笔记草稿在成员页往返保留，冲突不会清空草稿，重新读取后可合并保存", async () => {
  let note = "原笔记", revision = 1;
  fakeBackend({
    [`GET ${base}`]: () => ({ body: { ...data, note, revision } }),
    [`GET ${base}/members/person`]: () => ({ body: member }),
    [`PUT ${base}/note`]: (init) => {
      const body = JSON.parse(String(init?.body));
      return body.expectedRevision !== revision ? { status: 409, body: { detail: "资料已更新" } }
        : { body: { ...data, note: note = String(body.note), revision: ++revision } };
    },
  });
  const user = userEvent.setup();
  render(<ExternalMemoryPanel roleId="r1" threadId="t1" title="读书群" onBack={() => undefined} />);
  const input = await screen.findByLabelText("会话笔记");
  await user.clear(input); await user.type(input, "我的草稿");
  await user.click(screen.getByRole("button", { name: /阿宁/ }));
  expect(await screen.findByLabelText("完整档案")).toHaveValue("爱读书");
  expect(screen.getByText("阿宁 · 新昵称")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "返回聊天信息" }));
  expect(screen.getByLabelText("会话笔记")).toHaveValue("我的草稿");
  revision = 2; note = "另一页面的修改";
  await user.click(screen.getByRole("button", { name: "保存笔记" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("草稿已保留");
  expect(screen.getByLabelText("会话笔记")).toHaveValue("我的草稿");
  await user.click(screen.getByRole("button", { name: "重新读取并保留草稿" }));
  expect(await screen.findByText("另一页面的修改")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "保存笔记" }));
  await waitFor(() => expect(note).toBe("我的草稿"));
});

it("删除共享档案要明确确认，返回后原笔记草稿仍在", async () => {
  let deleted = false;
  fakeBackend({
    [`GET ${base}`]: () => ({ body: { ...data, members: deleted ? [] : [member] } }),
    [`GET ${base}/members/person`]: () => ({ body: member }),
    [`DELETE ${base}/members/person\\?expectedRevision=1`]: () => { deleted = true; return { status: 204 }; },
  });
  const user = userEvent.setup();
  render(<ExternalMemoryPanel roleId="r1" threadId="t1" title="读书群" onBack={() => undefined} />);
  await user.type(await screen.findByLabelText("会话笔记"), "草稿");
  await user.click(screen.getByRole("button", { name: /阿宁/ }));
  await user.click(await screen.findByRole("button", { name: "删除档案" }));
  expect(screen.getByRole("alertdialog")).toHaveTextContent("多个会话共用");
  expect(deleted).toBe(false);
  await user.click(screen.getByRole("button", { name: "确认删除" }));
  expect(await screen.findByText("暂无成员档案")).toBeVisible();
  expect(screen.getByLabelText("会话笔记")).toHaveValue("原笔记草稿");
});

it("旧角色迟到的读取不会覆盖新角色页面，移除账号只能查看", async () => {
  let resolveOld!: (value: { body: typeof data }) => void;
  fakeBackend({
    [`GET ${base}`]: () => new Promise(resolve => { resolveOld = resolve; }),
    "GET /api/roles/r2/channels/threads/t2/external-memory": () => ({ body: { ...data, note: "新角色笔记", readOnly: true } }),
  });
  const view = render(<ExternalMemoryPanel roleId="r1" threadId="t1" title="旧会话" onBack={() => undefined} />);
  await waitFor(() => expect(resolveOld).toBeDefined());
  view.rerender(<ExternalMemoryPanel roleId="r2" threadId="t2" title="新会话" onBack={() => undefined} />);
  expect(await screen.findByLabelText("会话笔记")).toHaveValue("新角色笔记");
  await act(async () => resolveOld({ body: data }));
  expect(screen.getByLabelText("会话笔记")).toHaveValue("新角色笔记");
  expect(screen.getByLabelText("会话笔记")).toHaveAttribute("readonly");
  expect(screen.queryByRole("button", { name: "保存笔记" })).not.toBeInTheDocument();
});

it("手机从聊天信息返回保留原消息节点、滚动位置与入口焦点", async () => {
  const backend = fakeBackend({
    "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [{ accountId: "tg", roleId: "r1", channel: "telegram", enabled: false, allowPrivate: true, allowGroup: true, blockedSenders: [], displayName: "测试账号" }] } }),
    "GET /api/roles/r1/channels/threads\\?networkOnly=true": () => ({ body: { threads: [{ threadId: "t1", accountId: "tg", channel: "telegram", chatId: "22", chatType: "private", title: "阿宁", lastActivity: "2026-10-26T01:00:00Z", externalMemoryAvailable: true }] } }),
    "GET .*threads/t1/messages.*": () => ({ body: { messages: [{ id: "m1", sequence: 1, role: "user", content: "保留的聊天消息", status: "completed", createdAt: "2026-10-26T01:00:00Z", metadata: { threadId: "t1", senderId: "22" } }], hasMore: false } }),
    [`GET ${base}`]: () => ({ body: data }),
  });
  const user = userEvent.setup();
  render(<ChannelView role={role({ id: "r1", name: "诗织" })} phoneMode onBack={() => undefined} />);
  await user.click(await screen.findByRole("button", { name: "Telegram，离线" }));
  await user.click(await screen.findByRole("button", { name: /阿宁/ }));
  const message = await screen.findByText("保留的聊天消息");
  const list = message.closest("ol")!;
  Object.defineProperties(list, { scrollHeight: { configurable: true, value: 900 }, clientHeight: { configurable: true, value: 100 } });
  list.scrollTop = 180; fireEvent.scroll(list);
  await user.click(screen.getByRole("button", { name: "聊天信息" }));
  expect(await screen.findByLabelText("会话笔记")).toHaveValue("原笔记");
  expect(message).not.toBeVisible();
  await user.click(screen.getByRole("button", { name: "返回聊天" }));
  expect(screen.getByText("保留的聊天消息")).toBe(message);
  expect(list.scrollTop).toBe(180);
  await waitFor(() => expect(screen.getByRole("button", { name: "聊天信息" })).toHaveFocus());
  expect(backend.calls.filter(call => call.path.includes("/messages")).length).toBe(1);
});
