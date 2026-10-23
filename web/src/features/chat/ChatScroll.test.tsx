import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, message, role, sse } from "../../test/fakeBackend";

const alice = role({ id: "r1", name: "爱丽丝" });
const session = { sessionKey: "role:r1", roleId: "r1", createdAt: alice.createdAt, updatedAt: alice.updatedAt };
const history = [message({ id: "old-u", role: "user", content: "旧消息" }), message({ id: "old-a", role: "assistant", content: "旧回复", sequence: 2 })];
let height = 2000;
let viewportHeight = 500;
const resizeCallbacks = new Set<() => void>();

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear();
  height = 2000; viewportHeight = 500;
  // jsdom has no layout. Supply browser geometry at the rendered scroll viewport.
  vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(function (this: Element) { return this.classList.contains("chat-scroll") ? height : 0; });
  vi.spyOn(Element.prototype, "clientHeight", "get").mockImplementation(function (this: Element) { return this.classList.contains("chat-scroll") ? viewportHeight : 0; });
  vi.stubGlobal("ResizeObserver", class {
    private notify: () => void;
    constructor(callback: ResizeObserverCallback) {
      this.notify = () => callback([], this as unknown as ResizeObserver);
      resizeCallbacks.add(this.notify);
    }
    observe() {}
    disconnect() { resizeCallbacks.delete(this.notify); }
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function conversationViewport() {
  const conversation = screen.getByRole("region", { name: "与爱丽丝的会话" });
  return within(conversation).getByRole("list").parentElement!;
}

function backend(extra: Parameters<typeof fakeBackend>[0] = {}) {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [alice] } }),
    "GET /api/roles/r1/session": () => ({ body: { session, messages: history } }),
    "POST /api/roles/r1/messages": () => ({ stream }),
    ...extra,
  });
  return (event: string, data: unknown) => act(() => controller.enqueue(new TextEncoder().encode(sse(event, data))));
}

describe("聊天滚动", () => {
  it("从历史位置发送后立即回到底部，首个事件尚未到达也能跟随", async () => {
    backend();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("旧回复");
    const viewport = conversationViewport();
    viewport.scrollTop = 100;
    fireEvent.scroll(viewport);
    await user.type(screen.getByRole("textbox", { name: "消息内容" }), "新消息{Enter}");
    await screen.findByRole("button", { name: "停止回复" });
    expect(viewport.scrollTop).toBe(1500);
  });

  it("流式内容增长时跟随，向上阅读后保持位置，回到底部后恢复跟随", async () => {
    const emit = backend();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("旧回复");
    const viewport = conversationViewport();
    await user.type(screen.getByRole("textbox", { name: "消息内容" }), "继续{Enter}");
    emit("user_message_accepted", { message: message({ id: "new-u", role: "user", content: "继续", sequence: 3 }) });
    emit("assistant_generation_started", { messageId: "new-a" });
    await screen.findByLabelText("正在回复");
    height = 2600;
    emit("assistant_delta", { messageId: "new-a", delta: "第一段" });
    await screen.findByText("第一段");
    expect(viewport.scrollTop).toBe(2100);

    viewport.scrollTop = 200;
    fireEvent.scroll(viewport);
    height = 3200;
    emit("assistant_delta", { messageId: "new-a", delta: "第二段" });
    await screen.findByText("第一段第二段");
    expect(viewport.scrollTop).toBe(200);

    viewport.scrollTop = 2690;
    fireEvent.scroll(viewport);
    height = 3600;
    emit("assistant_delta", { messageId: "new-a", delta: "第三段" });
    await screen.findByText("第一段第二段第三段");
    expect(viewport.scrollTop).toBe(3100);
  });

  it("图片和窗口尺寸改变时保持底部，历史阅读位置不被尺寸回调覆盖", async () => {
    backend();
    render(<App />);
    await screen.findByText("旧回复");
    const viewport = conversationViewport();
    expect(viewport.scrollTop).toBe(1500);
    height = 2800;
    act(() => resizeCallbacks.forEach(callback => callback()));
    expect(viewport.scrollTop).toBe(2300);
    viewportHeight = 350;
    act(() => resizeCallbacks.forEach(callback => callback()));
    expect(viewport.scrollTop).toBe(2450);

    viewport.scrollTop = 100;
    fireEvent.scroll(viewport);
    height = 3200;
    act(() => resizeCallbacks.forEach(callback => callback()));
    expect(viewport.scrollTop).toBe(100);
  });

  it("窗口重排先触发滚动事件时，仍继续跟随而不误判成向上阅读", async () => {
    backend();
    render(<App />);
    await screen.findByText("旧回复");
    const viewport = conversationViewport();
    expect(viewport.scrollTop).toBe(1500);
    height = 3600;
    viewportHeight = 350;
    // Browsers may emit scroll after reflow but before ResizeObserver delivery.
    fireEvent.scroll(viewport);
    act(() => resizeCallbacks.forEach(callback => callback()));
    expect(viewport.scrollTop).toBe(3250);
  });

  it("后台刷新历史时不打断正在阅读旧消息的位置", async () => {
    let stored = history;
    backend({ "GET /api/roles/r1/session": () => ({ body: { session, messages: stored } }) });
    render(<App />);
    await screen.findByText("旧回复");
    const viewport = conversationViewport();
    viewport.scrollTop = 100;
    fireEvent.scroll(viewport);
    height = 2800;
    stored = [...history, message({ id: "refreshed", role: "assistant", content: "刷新后的回复", sequence: 3 })];
    fireEvent(window, new Event("focus"));
    await screen.findByText("刷新后的回复");
    expect(viewport.scrollTop).toBe(100);
  });

  it("来源定位后发送新消息恢复底部跟随", async () => {
    const origin = { kind: "message", sessionKey: "role:r1", messageIds: ["old-u"], messageRange: null, stableSourceKey: "message:old-u" };
    backend({
      "GET /api/roles/r1/memories.*": () => ({ body: { memories: [{ id: "mem1", roleId: "r1", memoryType: "preference", summary: "一条记忆", status: "active", happenedAt: null, createdAt: alice.createdAt, updatedAt: alice.updatedAt, sourceRef: { ...origin, sourceKeys: [], sources: [origin] } }] } }),
      "GET /api/roles/r1/memory-documents": () => ({ body: { documents: [], journals: [] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("旧回复");
    await user.click(screen.getByRole("button", { name: "角色记忆" }));
    await screen.findByText("一条记忆");
    await user.click(screen.getByRole('button', { name: '查看记忆：一条记忆' }));
    await user.click(screen.getByRole("button", { name: "定位到消息" }));
    await screen.findByText("记忆来源");
    const viewport = conversationViewport();
    expect(viewport.scrollTop).toBe(0);
    viewport.scrollTop = 120;
    fireEvent.scroll(viewport);
    await user.type(screen.getByRole("textbox", { name: "消息内容" }), "新问题{Enter}");
    await screen.findByRole("button", { name: "停止回复" });
    await waitFor(() => expect(viewport.scrollTop).toBe(1500));
  });
});
