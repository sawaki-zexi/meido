import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, message, role, sse } from "../../test/fakeBackend";

const alice = role({ id: "r1", name: "爱丽丝" });
const session = { sessionKey: "role:r1", roleId: "r1", createdAt: alice.createdAt, updatedAt: alice.updatedAt };
beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("聊天输入与停止", () => {
  it("未接收的失败提交恢复文字和附件，重试不会丢失附件", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/attachments": () => ({ body: { id: "file-1", name: "notes.txt", mediaType: "text/plain", size: 4, kind: "file" } }),
      "POST /api/roles/r1/messages": () => ({ status: 503, body: { detail: "稍后重试" } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    await user.upload(screen.getByLabelText("附件文件"), new File(["note"], "notes.txt", { type: "text/plain" }));
    const input = screen.getByRole("textbox", { name: "消息内容" });
    await user.type(input, "未发送文字{Enter}");
    await screen.findByRole("alert");
    await waitFor(() => expect(input).toHaveValue("未发送文字"));
    expect(screen.getByRole("button", { name: "移除 notes.txt" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
  });

  it("已接收消息的回复连接失败后不回填原消息，也不覆盖新草稿", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/messages": () => {
        controller.enqueue(new TextEncoder().encode(
          sse("user_message_accepted", { message: message({ id: "u1", role: "user", content: "第一句" }) })
          + sse("assistant_generation_started", { messageId: "a1" })
          + sse("assistant_delta", { messageId: "a1", delta: "半句" }),
        ));
        return { stream };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    const input = screen.getByRole("textbox", { name: "消息内容" });
    await user.type(input, "第一句{Enter}");
    await screen.findByText("半句");
    await user.type(input, "下一句草稿");
    controller.error(new TypeError("连接中断"));
    await screen.findByRole("alert");
    expect(input).toHaveValue("下一句草稿");
    expect(screen.queryByText(/未发送的消息/)).toBeNull();
    expect(screen.getByText("未完成")).toBeTruthy();
  });

  it("响应头还没到达时停止，通过历史确认已接收消息而不重新回填", async () => {
    let releaseRequest!: (value: { stream: string[] }) => void;
    const response = new Promise<{ stream: string[] }>((resolve) => { releaseRequest = resolve; });
    const userMessage = { ...message({ id: "u1", role: "user", content: "早停" }), runId: "run-early" };
    const stopped = message({ id: "a1", role: "assistant", content: "", status: "failed", metadata: { runtimeReason: "cancelled" }, sequence: 2 });
    let posted = false;
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: posted ? [userMessage] : [] } }),
      "POST /api/roles/r1/messages": () => { posted = true; return response; },
      "POST /api/roles/r1/runs/run-early/stop": () => {
        releaseRequest({ stream: [] });
        return { body: { session, messages: [userMessage, stopped] } };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    const input = screen.getByRole("textbox", { name: "消息内容" });
    await user.type(input, "早停{Enter}");
    await user.click(screen.getByRole("button", { name: "停止回复" }));
    expect(await screen.findByText("已停止")).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeDisabled());
    expect(input).toHaveValue("");
  });

  it("发送被拒绝时保留未发送内容，不覆盖用户新输入", async () => {
    let rejectRequest!: (value: { status: number; body: { detail: string } }) => void;
    const response = new Promise<{ status: number; body: { detail: string } }>((resolve) => { rejectRequest = resolve; });
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/messages": () => response,
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    const input = screen.getByRole("textbox", { name: "消息内容" });
    await user.type(input, "原消息{Enter}");
    await user.type(input, "新草稿");
    rejectRequest({ status: 503, body: { detail: "服务暂时不可用" } });
    await screen.findByRole("alert");
    expect(input).toHaveValue("新草稿");
    expect(await screen.findByText("未发送的消息：原消息")).toBeTruthy();
  });

  it("收到响应头但尚未收到首个事件时也能停止，不恢复已接收的文字", async () => {
    const stream = new ReadableStream<Uint8Array>();
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/messages": () => ({ stream, headers: { "X-Meido-Run-Id": "run-before-token" } }),
      "POST /api/roles/r1/runs/run-before-token/stop": () => ({ body: { session, messages: [
        message({ id: "u1", role: "user", content: "首个字之前" }),
        message({ id: "a1", role: "assistant", content: "", status: "failed", metadata: { runtimeReason: "cancelled" }, sequence: 2 }),
      ] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    const input = screen.getByRole("textbox", { name: "消息内容" });
    await user.type(input, "首个字之前{Enter}");
    await user.click(await screen.findByRole("button", { name: "停止回复" }));
    await screen.findByText("已停止");
    await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeDisabled());
    expect(input).toHaveValue("");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("停止会取消当前运行并保留部分回复与新草稿，随后可以发送", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
    const userMessage = message({ id: "u1", role: "user", content: "第一句" });
    const stoppedMessage = message({ id: "a1", role: "assistant", content: "部分回复", status: "failed", metadata: { runtimeReason: "cancelled" }, sequence: 2 });
    let turn = 0;
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/messages": () => {
        turn += 1;
        if (turn > 1) return { stream: [
          sse("user_message_accepted", { message: message({ id: "u2", role: "user", content: "下一条" }) }),
          sse("assistant_generation_started", { messageId: "a2" }),
          sse("assistant_completed", { message: message({ id: "a2", role: "assistant", content: "新的回复", sequence: 4 }) }),
        ] };
        controller.enqueue(new TextEncoder().encode(
          sse("user_message_accepted", { message: userMessage, runId: "run-1" })
          + sse("assistant_generation_started", { messageId: "a1", runId: "run-1" })
          + sse("assistant_delta", { messageId: "a1", delta: "部分回复", runId: "run-1" }),
        ));
        return { stream };
      },
      "POST /api/roles/r1/runs/run-1/stop": () => {
        controller.error(new TypeError("terminated"));
        return { body: { session, messages: [userMessage, stoppedMessage] } };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    const input = screen.getByRole("textbox", { name: "消息内容" });
    await user.type(input, "第一句{Enter}");
    await screen.findByText("部分回复");
    await user.type(input, "下一条{Enter}");
    expect(calls.filter((call) => call.path.endsWith("/messages"))).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "停止回复" }));
    await screen.findByText("已停止");
    expect(screen.getByText("部分回复")).toBeTruthy();
    expect(input).toHaveValue("下一条");
    expect(screen.queryByRole("alert")).toBeNull();
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("新的回复")).toBeTruthy();
    expect(input).toHaveValue("");
  });

  it("慢回复时已发送文字立即清空，输入框可以继续编辑", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/messages": () => {
        controller.enqueue(new TextEncoder().encode(
          sse("user_message_accepted", { message: message({ id: "u1", role: "user", content: "第一句" }), runId: "run-1" })
          + sse("assistant_generation_started", { messageId: "a1", runId: "run-1" })
          + sse("assistant_delta", { messageId: "a1", delta: "正在回答", runId: "run-1" }),
        ));
        return { stream };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    const input = screen.getByRole("textbox", { name: "消息内容" });
    try {
      await user.type(input, "第一句{Enter}");
      await screen.findByText("正在回答");
      expect(input).toHaveValue("");
      expect(input).toBeEnabled();
      await user.type(input, "下一句草稿");
    } finally {
      controller.enqueue(new TextEncoder().encode(sse("assistant_completed", { message: message({ id: "a1", role: "assistant", content: "正在回答", sequence: 2 }) })));
      controller.close();
    }
    await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeEnabled());
    expect(input).toHaveValue("下一句草稿");
  });
});
