import { fakeBackend as directBackend } from '../../test/fakeBackend';
import { fakeChatBackend as fakeBackend } from '../../test/fakeChatBackend';
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { message, role, sse } from "../../test/fakeBackend";

const alice = role({ id: "r1", name: "爱丽丝" });
const session = { sessionKey: "role:r1", roleId: "r1", createdAt: alice.createdAt, updatedAt: alice.updatedAt };
beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("聊天输入与停止", () => {
  it("部分附件失败后等整批结束，保留成功项并仅发送成功附件", async () => {
    type UploadReply = { status?: number; body: object };
    let finishGood!: (reply: UploadReply) => void;
    let finishEmpty!: (reply: UploadReply) => void;
    const goodResponse = new Promise<UploadReply>((resolve) => { finishGood = resolve; });
    const emptyResponse = new Promise<UploadReply>((resolve) => { finishEmpty = resolve; });
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/attachments": (init) => new Headers(init?.headers).get("x-attachment-filename") === "good.txt" ? goodResponse : emptyResponse,
      "CHAT /api/roles/r1": () => ({ stream: [
        sse("user_message_accepted", { message: message({ id: "u-batch", role: "user", content: "查看文件" }) }),
        sse("assistant_generation_started", { messageId: "a-batch" }),
        sse("assistant_completed", { message: message({ id: "a-batch", role: "assistant", content: "收到文件", sequence: 2 }) }),
      ] }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    await user.type(screen.getByRole("textbox", { name: "消息内容" }), "查看文件");
    await user.upload(screen.getByLabelText("附件文件"), [
      new File(["note"], "good.txt", { type: "text/plain" }),
      new File([], "empty.txt", { type: "text/plain" }),
    ]);
    await act(async () => { finishEmpty({ status: 422, body: { detail: "附件不能为空" } }); });
    expect(screen.getByRole("button", { name: "添加附件" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    await act(async () => { finishGood({ body: { id: "good-id", name: "good.txt", mediaType: "text/plain", size: 4, kind: "file" } }); });
    expect(await screen.findByRole("button", { name: "移除 good.txt" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "移除 empty.txt" })).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("empty.txt：附件不能为空");
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("收到文件", { selector: ".bubble p" });
    expect(calls.filter((call) => call.method === "POST" && call.path === "/api/roles/r1/runs").map((call) => call.body)).toMatchObject([
      { content: "查看文件", attachments: ["good-id"] },
    ]);
    expect(calls.filter((call) => call.path === "/api/roles/r1/attachments")).toHaveLength(2);
  });

  it("收到回复完成事件后立即恢复发送，即使响应连接还没有关闭", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; }, cancel });
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "CHAT /api/roles/r1": () => {
        controller.enqueue(new TextEncoder().encode(
          sse("user_message_accepted", { message: message({ id: "u1", role: "user", content: "第一句" }), runId: "run-1" })
          + sse("assistant_generation_started", { messageId: "a1", runId: "run-1" })
          + sse("assistant_completed", { message: message({ id: "a1", role: "assistant", content: "回复已经完成", sequence: 2 }), runId: "run-1" }),
        ));
        return { stream };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    const input = screen.getByRole("textbox", { name: "消息内容" });
    await user.type(input, "第一句{Enter}");
    await screen.findByText("回复已经完成", { selector: ".bubble p" });
    await waitFor(() => expect(screen.queryByRole("button", { name: "停止回复" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(screen.queryByText("正在输入…")).not.toBeInTheDocument();
    expect(input).toHaveValue("");
    expect(cancel).toHaveBeenCalledOnce();
    expect(calls.some((call) => call.path.endsWith("/stop"))).toBe(false);
  });

  it.each(["assistant_completed", "assistant_failed"] as const)("%s 终态不等待连接回收，保留生成期间的新草稿并可继续发送", async (terminal) => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const cancel = vi.fn(() => new Promise<void>(() => undefined));
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; }, cancel });
    let turn = 0;
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice, role({ id: "r2", name: "贝拉" })] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: [{ id: "model-1", model: "test-model" }] } }),
      "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: null, effectiveConfigurationId: "model-1" } }),
      "CHAT /api/roles/r1": () => {
        if (++turn > 1) return { stream: [
          sse("user_message_accepted", { message: message({ id: "u2", role: "user", content: "下一句草稿", sequence: 3 }) }),
          sse("assistant_generation_started", { messageId: "a2" }),
          sse("assistant_completed", { message: message({ id: "a2", role: "assistant", content: "第二轮回复", sequence: 4 }) }),
        ] };
        controller.enqueue(new TextEncoder().encode(
          sse("user_message_accepted", { message: message({ id: "u1", role: "user", content: "第一句" }), runId: "run-1" })
          + sse("assistant_generation_started", { messageId: "a1", runId: "run-1" })
          + sse("assistant_delta", { messageId: "a1", delta: "已保留的回复", runId: "run-1" }),
        ));
        return { stream };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    const input = screen.getByRole("textbox", { name: "消息内容" });
    await user.type(input, "第一句{Enter}");
    await screen.findByText("已保留的回复");
    await user.type(input, "下一句草稿");
    expect(screen.getByRole("button", { name: "选择聊天模型" })).toBeDisabled();
    expect(screen.getByRole("button", { name: /贝拉/ })).toBeEnabled();
    act(() => controller.enqueue(new TextEncoder().encode(sse(terminal, {
      message: message({ id: "a1", role: "assistant", content: "已保留的回复", sequence: 2, status: terminal === "assistant_completed" ? "completed" : "failed" }),
      error: terminal === "assistant_failed" ? "模型回复未能完成" : undefined,
      runId: "run-1",
    }))));
    await waitFor(() => expect(screen.queryByRole("button", { name: "停止回复" })).not.toBeInTheDocument());
    expect(screen.queryByText("正在输入…")).not.toBeInTheDocument();
    expect(input).toHaveValue("下一句草稿");
    expect(screen.getByRole("button", { name: "选择聊天模型" })).toBeEnabled();
    expect(screen.getByRole("button", { name: /贝拉/ })).toBeEnabled();
    if (terminal === "assistant_failed") {
      expect(screen.getByRole("alert")).toHaveTextContent("模型回复未能完成");
      expect(screen.getByText("未完成")).toBeInTheDocument();
    } else expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("第二轮回复", { selector: ".bubble p" })).toBeInTheDocument();
    expect(input).toHaveValue("");
    expect(calls.filter((call) => call.path.endsWith("/runs")).map((call) => call.body)).toMatchObject([{ content: "第一句" }, { content: "下一句草稿" }]);
    expect(calls.some((call) => call.path.endsWith("/stop"))).toBe(false);
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("后端已接收但接受响应丢失时，按稳定标识恢复消息，避免回填重复发送", async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('lost-header-0-0-0');
    const accepted = message({ id: 'u1', role: 'user', content: '已收到的消息', runId: 'run-lost-header-0-0-0' });
    directBackend({
      'GET /api/roles': () => ({ body: { roles: [alice] } }),
      'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
      'POST /api/roles/r1/runs': () => { throw new TypeError('接受响应丢失'); },
      'GET /api/roles/r1/runs/run-lost-header-0-0-0': () => ({ body: { session, messages: [accepted],
        run: { ...session, runId: 'run-lost-header-0-0-0', sequence: 3, status: 'completed', phase: 'finished', tools: [], stopping: false } } }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole('heading', { name: '爱丽丝' });
    const input = screen.getByRole('textbox', { name: '消息内容' });
    await user.type(input, '已收到的消息{Enter}');
    await screen.findByText('已收到的消息');
    await waitFor(() => expect(screen.queryByRole('button', { name: '停止回复' })).toBeNull());
    expect(input).toHaveValue('');
    expect(screen.queryByText(/未发送的消息/)).toBeNull();
  });

  it.each(['network', '503'])('提交和确切查询均不可达（%s）时只恢复原请求，保留下一条草稿和附件', async failure => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('unknown-0-0-0-0');
    const attachment = { id: 'file-1', name: 'notes.txt', mediaType: 'text/plain', size: 4, kind: 'file' };
    const run = { ...session, runId: 'run-unknown-0-0-0-0', sequence: 3, status: 'completed', phase: 'finished', tools: [], stopping: false };
    let ready = false, reads = 0;
    const unavailable = () => {
      if (failure === 'network') throw new TypeError('synthetic lost response');
      return { status: 503, body: { detail: 'synthetic unavailable' } };
    };
    const { calls } = directBackend({
      'GET /api/roles': () => ({ body: { roles: [alice] } }),
      'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
      'POST /api/roles/r1/attachments': () => ({ body: attachment }),
      'POST /api/roles/r1/runs': unavailable,
      'GET /api/roles/r1/runs/run-unknown-0-0-0-0': () => {
        reads += 1;
        return ready ? { body: { session, run, messages: [
          message({ id: 'u1', role: 'user', content: '原消息', runId: run.runId, metadata: { attachments: [attachment] } }),
          message({ id: 'a1', role: 'assistant', content: '已保存回复', sequence: 2, runId: run.runId }),
        ] } } : unavailable();
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole('heading', { name: '爱丽丝' });
    await user.upload(screen.getByLabelText('附件文件'), new File(['note'], 'notes.txt', { type: 'text/plain' }));
    const input = screen.getByRole('textbox', { name: '消息内容' });
    await user.type(input, '原消息{Enter}');
    await waitFor(() => expect(reads).toBeGreaterThan(0));
    expect(input).toHaveValue('');
    expect(screen.getByRole('alert')).toHaveTextContent('发送结果尚未确认');
    expect(screen.getByRole('button', { name: '停止回复' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '移除 notes.txt' })).toBeNull();
    await user.type(input, '下一条草稿{Enter}');
    await act(async () => { window.dispatchEvent(new Event('focus')); });
    expect(calls.filter(call => call.path.endsWith('/session'))).toHaveLength(1);
    ready = true;
    await screen.findByText('已保存回复', { selector: '.bubble p' });
    expect(input).toHaveValue('下一条草稿');
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText(/未发送的消息/)).toBeNull();
    expect(screen.getAllByRole('link', { name: 'notes.txt' })).toHaveLength(1);
    expect(calls.filter(call => call.method === 'POST' && call.path.endsWith('/runs')).map(call => call.body))
      .toEqual([{ content: '原消息', attachments: ['file-1'], requestId: 'unknown-0-0-0-0' }]);
  });

  it('切换角色等待期间仍挂载的旧输入框不回填未知消息或附件', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('detached-0-0-0-0');
    let finishOpening!: (value: { body: unknown }) => void;
    const opening = new Promise<{ body: unknown }>(resolve => { finishOpening = resolve; });
    let recoverySignal: AbortSignal | null | undefined;
    directBackend({
      'GET /api/roles': () => ({ body: { roles: [alice, role({ id: 'r2', name: '贝拉' })] } }),
      'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
      'GET /api/roles/r2/session': () => opening,
      'POST /api/roles/r1/attachments': () => ({ body: { id: 'file-1', name: 'notes.txt', mediaType: 'text/plain', size: 4, kind: 'file' } }),
      'POST /api/roles/r1/runs': () => { throw new TypeError('synthetic lost response'); },
      'GET /api/roles/r1/runs/run-detached-0-0-0-0': init => {
        recoverySignal = init?.signal;
        throw new TypeError('synthetic unavailable');
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole('heading', { name: '爱丽丝' });
    await user.upload(screen.getByLabelText('附件文件'), new File(['note'], 'notes.txt', { type: 'text/plain' }));
    const input = screen.getByRole('textbox', { name: '消息内容' });
    await user.type(input, '原消息{Enter}');
    await screen.findByRole('alert');
    await user.click(screen.getByRole('button', { name: /贝拉/ }));
    expect(input).toBeInTheDocument();
    expect(input).toBeDisabled();
    expect(input).toHaveValue('');
    expect(screen.queryByRole('button', { name: '移除 notes.txt' })).toBeNull();
    expect(screen.queryByText(/未发送的消息/)).toBeNull();
    expect(recoverySignal?.aborted).toBe(true);
    await act(async () => { finishOpening({ body: { session: { ...session, roleId: 'r2', sessionKey: 'role:r2' }, messages: [] } }); });
    await screen.findByRole('heading', { name: '贝拉' });
    expect(screen.getByRole('textbox', { name: '消息内容' })).toHaveValue('');
  });

  it("未接收的失败提交恢复文字和附件，重试不会丢失附件", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/attachments": () => ({ body: { id: "file-1", name: "notes.txt", mediaType: "text/plain", size: 4, kind: "file" } }),
      "CHAT /api/roles/r1": () => ({ status: 503, body: { detail: "稍后重试" } }),
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
      "CHAT /api/roles/r1": () => {
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
    expect(screen.getByRole("status", { name: "回复状态" })).toHaveTextContent("正在输入…");
    expect(screen.getByRole("button", { name: "停止回复" })).toBeEnabled();
  });

  it("响应头还没到达时停止，按确切接受标识停止已接收消息而不重新回填", async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('early-0-0-0-0');
    const userMessage = { ...message({ id: "u1", role: "user", content: "早停" }), runId: "run-early-0-0-0-0" };
    const stopped = message({ id: "a1", role: "assistant", content: "", status: "failed", metadata: { runtimeReason: "cancelled" }, sequence: 2 });
    let posted = false;
    directBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: posted ? [userMessage] : [] } }),
      "POST /api/roles/r1/runs": init => new Promise((_resolve, reject) => {
        posted = true; init?.signal?.addEventListener('abort', () => reject(new TypeError('aborted')), { once: true });
      }),
      "POST /api/roles/r1/runs/run-early-0-0-0-0/stop": () => {
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
      "CHAT /api/roles/r1": () => response,
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
      "CHAT /api/roles/r1": () => ({ stream, headers: { "X-Meido-Run-Id": "run-before-token" } }),
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
      "CHAT /api/roles/r1": () => {
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
    expect(calls.filter((call) => call.path.endsWith("/runs"))).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "停止回复" }));
    await screen.findByText("已停止");
    expect(screen.getByText("部分回复")).toBeTruthy();
    expect(input).toHaveValue("下一条");
    expect(screen.queryByRole("alert")).toBeNull();
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText("新的回复", { selector: ".bubble p" })).toBeTruthy();
    expect(input).toHaveValue("");
  });

  it("慢回复时已发送文字立即清空，输入框可以继续编辑", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "CHAT /api/roles/r1": () => {
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
