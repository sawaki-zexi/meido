import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import * as streamApi from "../../api/stream";
import { fakeBackend, message, role, sse } from "../../test/fakeBackend";
import { useChat } from "./useChat";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("rejects old reply callbacks during retry and after unmount, without stealing the run ID (#233)", async () => {
  const replies: { emit: Parameters<typeof streamApi.streamRoleMessage>[3]; options: Parameters<typeof streamApi.streamRoleMessage>[4]; release: () => void }[] = [];
  vi.spyOn(streamApi, "streamRoleMessage").mockImplementation((_roleId, _content, _attachments, emit, options) => new Promise<void>(release => { replies.push({ emit, options, release }); }));
  const session = { roleId: "r1", sessionKey: "role:r1" };
  fakeBackend({ "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }) });
  const { result, unmount } = renderHook(useChat);
  await act(async () => { await result.current.open(role({ id: "r1", name: "Alice" })); });
  let first!: Promise<boolean>;
  act(() => { first = result.current.send("first"); });
  const call = { ...session, type: "tool_execution_start" as const, sequence: 1, runId: "run-1", toolCallId: "call-1", toolName: "same_tool" };
  act(() => {
    replies[0].options?.onRun?.("run-1");
    replies[0].emit(call);
    replies[0].emit({ type: "assistant_failed", runId: "run-1", sequence: 2, message: message({ id: "a1", role: "assistant", content: "失败", status: "failed" }) });
  });
  await act(async () => { replies[0].release(); await first; });
  expect(result.current.toolProgress).toBeNull();
  let second!: Promise<boolean>;
  act(() => { second = result.current.send("retry"); });
  act(() => {
    replies[1].options?.onRun?.("run-2");
    replies[1].emit({ ...call, runId: "run-2" });
  });
  expect(result.current.toolProgress?.runId).toBe("run-2");
  act(() => {
    replies[0].options?.onRun?.("run-1");
    expect(replies[0].emit({ ...call, sequence: 3, toolCallId: "old-late-call" })).toBe(false);
    expect(replies[1].emit({ type: "assistant_completed", runId: "run-1", sequence: 3, message: message({ id: "old", role: "assistant", content: "stale terminal" }) })).toBe(false);
  });
  expect(result.current.sending).toBe(true);
  expect(result.current.toolProgress?.calls.map(call => call.toolCallId)).toEqual(["call-1"]);
  const before = result.current;
  unmount();
  expect(replies[1].options?.signal?.aborted).toBe(true);
  expect(replies[1].emit({ ...call, runId: "run-2", sequence: 4, toolCallId: "unmounted-call" })).toBe(false);
  replies[1].release();
  await second;
  expect(result.current).toBe(before);
});

it("a delayed history refresh cannot replace a newer role/session or revive progress", async () => {
  let respond!: (value: { body: unknown }) => void;
  const delayed = new Promise<{ body: unknown }>(resolve => { respond = resolve; });
  let loads = 0;
  fakeBackend({
    "GET /api/roles/r1/session": () => ++loads === 1 ? { body: { session: { roleId: "r1", sessionKey: "role:r1" }, messages: [] } } : delayed,
    "GET /api/roles/r2/session": () => ({ body: { session: { roleId: "r2", sessionKey: "role:r2" }, messages: [] } }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(role({ id: "r1", name: "Alice" })); });
  let refreshing!: Promise<void>;
  act(() => { refreshing = result.current.refresh(); });
  await act(async () => { await result.current.open(role({ id: "r2", name: "Bella" })); });
  await act(async () => {
    respond({ body: { session: { roleId: "r1", sessionKey: "role:r1" }, messages: [message({ id: "old", role: "assistant", content: "old history" })] } });
    await refreshing;
  });
  expect(result.current.roleId).toBe("r2");
  expect(result.current.session?.sessionKey).toBe("role:r2");
  expect(result.current.messages).toEqual([]);
  expect(result.current.toolProgress).toBeNull();
});

it("a pre-run history refresh cannot erase the completed reply after the new run ends", async () => {
  let respond!: (value: { body: unknown }) => void;
  const delayed = new Promise<{ body: unknown }>(resolve => { respond = resolve; });
  let loads = 0;
  const session = { roleId: "r1", sessionKey: "role:r1" };
  fakeBackend({ "GET /api/roles/r1/session": () => ++loads === 1 ? { body: { session, messages: [] } } : delayed });
  vi.spyOn(streamApi, "streamRoleMessage").mockImplementation(async (_role, _content, _attachments, emit) => {
    emit({ type: "assistant_generation_started", runId: "new-run", sequence: 1, messageId: "new" });
    emit({ type: "assistant_completed", runId: "new-run", sequence: 2, message: message({ id: "new", role: "assistant", content: "已持久化的最终回复" }) });
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(role({ id: "r1", name: "Alice" })); });
  let refreshing!: Promise<void>;
  act(() => { refreshing = result.current.refresh(); });
  await act(async () => { await result.current.send("new"); });
  await act(async () => { respond({ body: { session, messages: [] } }); await refreshing; });
  expect(result.current.messages.map(message => message.content)).toEqual(["已持久化的最终回复"]);
});

it("commits natural-chat segments without ending the active reply or duplicating a placeholder", async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  const session = { roleId: "r1", sessionKey: "role:r1" };
  fakeBackend({
    "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
    "POST /api/roles/r1/messages": () => ({ stream, headers: { "X-Meido-Run-Id": "sequence-run" } }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(role({ id: "r1", name: "Alice" })); });
  let sending!: Promise<boolean>;
  act(() => { sending = result.current.send("hello"); });
  const emit = (type: string, data: object) => controller.enqueue(new TextEncoder().encode(sse(type, { runId: "sequence-run", ...data })));
  await act(async () => {
    emit("assistant_generation_started", { sequence: 1, messageId: "first" });
    emit("assistant_message_committed", { sequence: 2, message: message({ id: "first", role: "assistant", content: "第一条" }) });
  });
  await waitFor(() => expect(result.current.messages.map(item => item.content)).toEqual(["第一条"]));
  expect(result.current.messages[0].status).toBe("completed");
  expect(result.current.sending).toBe(true);
  await act(async () => { expect(await result.current.send("too early")).toBe(false); });
  act(() => { result.current.reset(); });
  expect(result.current.session?.sessionKey).toBe("role:r1");
  await act(async () => {
    emit("assistant_message_committed", { sequence: 3, message: message({ id: "middle", role: "assistant", content: "第二条" }) });
    emit("assistant_completed", { sequence: 4, message: message({ id: "last", role: "assistant", content: "最后一条" }) });
    await sending;
  });
  expect(result.current.messages.map(item => item.content)).toEqual(["第一条", "第二条", "最后一条"]);
  expect(result.current.sending).toBe(false);
});

it("stops a natural sequence using the same run and retains its committed prefix", async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  const session = { roleId: "r1", sessionKey: "role:r1" };
  const first = message({ id: "first", role: "assistant", content: "已发送前段" });
  const { calls } = fakeBackend({
    "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
    "POST /api/roles/r1/messages": () => ({ stream, headers: { "X-Meido-Run-Id": "sequence-run" } }),
    "POST /api/roles/r1/runs/sequence-run/stop": () => ({ body: { session, messages: [first] } }),
  });
  const { result } = renderHook(useChat);
  await act(async () => { await result.current.open(role({ id: "r1", name: "Alice" })); });
  let sending!: Promise<boolean>;
  act(() => { sending = result.current.send("hello"); });
  await act(async () => {
    controller.enqueue(new TextEncoder().encode(sse("assistant_message_committed", { runId: "sequence-run", sequence: 1, message: first })));
  });
  await waitFor(() => expect(result.current.messages).toEqual([first]));
  await act(async () => { result.current.stop(); await sending; });
  expect(calls.filter(call => call.path.endsWith("/stop"))).toHaveLength(1);
  expect(result.current.messages).toEqual([first]);
  expect(result.current.sending).toBe(false);
  expect(result.current.error).toBe("");
});
