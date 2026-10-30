import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { App } from "../../App";
import { fakeBackend, message, role, sse } from "../../test/fakeBackend";

const alice = role({ id: "r1", name: "爱丽丝" });
const bella = role({ id: "r2", name: "贝拉" });
const session = { sessionKey: "role:r1", roleId: "r1" };
const call = { ...session, runId: "run-1", sequence: 3, toolCallId: "call-1", toolName: "mcp_connect" };
const userMessage = message({ id: "u1", role: "user", content: "执行工具", runId: "run-1" });
const assistant = message({ id: "a1", role: "assistant", content: "真实最终回复", sequence: 2, runId: "run-1" });

function feed() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const cancel = vi.fn();
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; }, cancel });
  const emit = (type: string, data: unknown) => controller.enqueue(new TextEncoder().encode(sse(type, data)));
  const begin = (argumentsPreview?: string) => {
    emit("user_message_accepted", { message: userMessage, runId: "run-1", sequence: 1 });
    emit("assistant_generation_started", { messageId: "a1", runId: "run-1", sequence: 2 });
    emit("tool_execution_start", { ...call, argumentsPreview, arguments: { token: "never-show-secret" } });
  };
  return { stream, emit, begin, cancel };
}

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("renders assistant Markdown beside live tool progress without formatting or executing the tool summary", async () => {
  const events = feed();
  const content = "**正在处理**，会使用 `mcp_connect`。";
  const summary = "**连接阶段** [不跳转](https://example.test/status) <img src=x onerror=alert(1)>";
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [alice] } }),
    "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
    "POST /api/roles/r1/messages": () => {
      events.emit("user_message_accepted", { message: userMessage, runId: "run-1", sequence: 1 });
      events.emit("assistant_generation_started", { messageId: "a1", runId: "run-1", sequence: 2 });
      events.emit("assistant_delta", { messageId: "a1", delta: content, runId: "run-1", sequence: 3 });
      events.emit("tool_execution_start", { ...call, sequence: 4 });
      events.emit("tool_execution_update", { ...call, sequence: 5, result: summary, status: "running", isError: false });
      return { stream: events.stream, headers: { "X-Meido-Run-Id": "run-1" } };
    },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "爱丽丝" });
  await user.type(screen.getByRole("textbox", { name: "消息内容" }), "执行工具{Enter}");
  const progress = await screen.findByRole("status", { name: "工具执行" });
  expect(screen.getByText("正在处理", { selector: ".message.assistant strong" })).toBeVisible();
  expect(screen.getByText("mcp_connect", { selector: ".message.assistant code" })).toBeVisible();
  await user.click(within(progress).getByRole("button", { name: /mcp_connect · 运行中/ }));
  expect(within(progress).getByText(summary)).toBeVisible();
  expect(within(progress).getByRole("button", { name: /mcp_connect · 运行中/ })).toBeVisible();
  expect(progress.querySelector("a, img, .message-markdown")).toBeNull();
  act(() => events.emit("tool_execution_end", { ...call, sequence: 6, result: "派发完成，结果不确定", status: "unknown", isError: true }));
  await within(progress).findByRole("button", { name: /mcp_connect · 结果不确定/ });
  act(() => events.emit("assistant_completed", { message: { ...assistant, content: "**最终回复**保留真实结果。" }, runId: "run-1", sequence: 7 }));
  expect(await screen.findByText("最终回复", { selector: ".message.assistant strong" })).toBeVisible();
  await waitFor(() => expect(screen.queryByRole("status", { name: "工具执行" })).toBeNull());
});

it("shows a safe update before the producer barrier releases, then truthful independent call states (#233)", async () => {
  const events = feed();
  let release!: () => void;
  const barrier = new Promise<void>(resolve => { release = resolve; });
  let completed = false;
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [alice, bella] } }),
    "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
    "POST /api/roles/r1/messages": () => {
      events.begin();
      events.emit("tool_execution_update", { ...call, sequence: 4, result: "连接握手已开始 <img src=x onerror=alert(1)>", status: "running", droppedUpdates: 8, isError: false, images: ["raw-base64"], details: { token: "never-show-secret" } });
      void barrier.then(() => {
        completed = true;
        events.emit("tool_execution_end", { ...call, sequence: 5, result: "派发完成，结果不确定", status: "unknown", isError: true, droppedUpdates: 8 });
        events.emit("tool_execution_start", { ...call, sequence: 6, toolCallId: "call-2" });
      });
      return { stream: events.stream, headers: { "X-Meido-Run-Id": "run-1" } };
    },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "爱丽丝" });
  await user.type(screen.getByRole("textbox", { name: "消息内容" }), "执行工具{Enter}");
  const progress = await screen.findByRole("status", { name: "工具执行" });
  await user.click(within(progress).getByRole("button", { name: /mcp_connect · 运行中/ }));
  expect(within(progress).getByText("连接握手已开始 <img src=x onerror=alert(1)>")).toBeVisible();
  expect(within(progress).getByRole("button", { name: /mcp_connect · 运行中/ })).toBeVisible();
  expect(within(progress).getByText(/已合并 8 次中间更新/)).toBeVisible();
  expect(progress.querySelector("img")).toBeNull();
  expect(progress).not.toHaveTextContent("never-show-secret");
  expect(progress).not.toHaveTextContent("raw-base64");
  expect(completed).toBe(false);
  expect(screen.getByRole("button", { name: /贝拉/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: "选择聊天模型" })).toBeDisabled();
  await act(async () => { release(); await barrier; });
  await within(progress).findByRole("button", { name: /mcp_connect · 结果不确定/ });
  expect(within(progress).getAllByText("mcp_connect")).toHaveLength(2);
  expect(within(progress).getByRole("button", { name: /mcp_connect · 运行中/ })).toBeVisible();
  act(() => {
    events.emit("tool_execution_update", { ...call, sequence: 7, result: "late-old-call" });
    events.emit("tool_execution_update", { ...call, toolCallId: "call-2", sequence: 6, result: "duplicate" });
    events.emit("tool_execution_update", { ...call, toolCallId: "call-2", sequence: 8, runId: "other-run", result: "wrong-run" });
    events.emit("tool_execution_update", { ...call, toolCallId: "call-2", sequence: 8, roleId: "r2", result: "wrong-role" });
    events.emit("tool_execution_update", { ...call, toolCallId: "call-2", sequence: 8, sessionKey: "other-session", result: "wrong-session" });
    events.emit("tool_execution_update", { ...call, toolCallId: "call-2", sequence: 8, result: "新调用有效进度" });
  });
  await user.click(within(progress).getByRole("button", { name: /mcp_connect · 运行中/ }));
  await within(progress).findByText("新调用有效进度");
  expect(progress).not.toHaveTextContent(/late-old-call|duplicate|wrong-run|wrong-role|wrong-session/);
  act(() => {
    events.emit("tool_execution_end", { ...call, toolCallId: "call-2", sequence: 9, result: "已完成", status: "succeeded" });
    events.emit("assistant_completed", { message: assistant, runId: "run-1", sequence: 10 });
  });
  expect(await screen.findByText("真实最终回复", { selector: ".bubble p" })).toBeVisible();
  await waitFor(() => expect(screen.queryByRole("status", { name: "工具执行" })).toBeNull());
  expect(events.cancel).toHaveBeenCalledOnce();
});

it("does not accept progress after stop, waits for cleanup, and never resurrects it on history/role switches", async () => {
  const events = feed();
  let finishStop!: (value: { body: unknown }) => void;
  const stopped = new Promise<{ body: unknown }>(resolve => { finishStop = resolve; });
  const argumentsPreview = '{"description":"连接工具服务"}';
  const saved = message({ ...assistant, status: "failed", content: "已派发的动作结果不确定", metadata: { runtimeReason: "cancelled",
    process: [{ type: 'tool', id: call.toolCallId, name: call.toolName, argumentsPreview, status: 'unknown', result: '尚无最终结果' }],
  } });
  let history: typeof userMessage[] = [];
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [alice, bella] } }),
    "GET /api/roles/r1/session": () => ({ body: { session, messages: history } }),
    "GET /api/roles/r2/session": () => ({ body: { session: { roleId: "r2", sessionKey: "role:r2" }, messages: [] } }),
    "POST /api/roles/r1/messages": () => { history = []; events.begin(argumentsPreview); return { stream: events.stream, headers: { "X-Meido-Run-Id": "run-1" } }; },
    "POST /api/roles/r1/runs/run-1/stop": () => stopped,
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "爱丽丝" });
  await user.type(screen.getByRole("textbox", { name: "消息内容" }), "执行工具{Enter}");
  const progress = await screen.findByRole("status", { name: "工具执行" });
  expect(within(progress).getByText(argumentsPreview)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "停止回复" }));
  expect(within(progress).getByText("正在停止…")).toBeVisible();
  expect(screen.getByRole("button", { name: /贝拉/ })).toBeDisabled();
  act(() => events.emit("tool_execution_update", { ...call, sequence: 4, result: "late-after-stop" }));
  await act(async () => { history = [userMessage, saved]; finishStop({ body: { session, messages: history } }); await stopped; });
  expect(await screen.findByText("已派发的动作结果不确定")).toBeVisible();
  expect(screen.queryByText("late-after-stop")).toBeNull();
  expect(screen.queryByRole("status", { name: "工具执行" })).toBeNull();
  await user.click(screen.getByRole("button", { name: /贝拉/ }));
  await screen.findByRole("heading", { name: "贝拉" });
  await user.click(screen.getByRole("button", { name: /爱丽丝/ }));
  await screen.findByRole("heading", { name: "爱丽丝" });
  expect(screen.getByText("已派发的动作结果不确定")).toBeVisible();
  expect(screen.queryByRole("status", { name: "工具执行" })).toBeNull();
  await user.click(screen.getByRole('button', { name: '1 次工具调用' }));
  expect(within(screen.getByRole('button', { name: 'mcp_connect · 结果不确定' })).getByText(argumentsPreview)).toBeVisible();
});

it("a failed stop does not reopen the progress window or claim that cleanup completed", async () => {
  const events = feed();
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [alice] } }),
    "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
    "POST /api/roles/r1/messages": () => { events.begin(); return { stream: events.stream, headers: { "X-Meido-Run-Id": "run-1" } }; },
    "POST /api/roles/r1/runs/run-1/stop": () => ({ status: 503, body: { detail: "停止结果尚未确认" } }),
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "爱丽丝" });
  await user.type(screen.getByRole("textbox", { name: "消息内容" }), "执行工具{Enter}");
  const progress = await screen.findByRole("status", { name: "工具执行" });
  await user.click(screen.getByRole("button", { name: "停止回复" }));
  await screen.findByText("停止结果尚未确认");
  expect(within(progress).getByText("等待最终状态")).toBeVisible();
  act(() => {
    events.emit("tool_execution_update", { ...call, sequence: 4, result: "late-after-stop-error" });
    events.emit("tool_execution_start", { ...call, sequence: 5, toolCallId: "late-new-call" });
    events.emit("tool_execution_end", { ...call, sequence: 6, result: "真实已完成回执", status: "succeeded", isError: false });
  });
  await user.click(await within(progress).findByRole("button", { name: /mcp_connect · 已完成/ }));
  expect(await within(progress).findByText("真实已完成回执")).toBeVisible();
  expect(within(progress).getByRole("button", { name: /mcp_connect · 已完成/ })).toBeVisible();
  expect(within(progress).getAllByText("mcp_connect")).toHaveLength(1);
  expect(progress).not.toHaveTextContent("late-after-stop-error");
  act(() => events.emit("assistant_completed", { message: assistant, runId: "run-1", sequence: 7 }));
  await screen.findByText("真实最终回复", { selector: ".bubble p" });
});
