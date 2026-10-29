import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import type { Message } from "../../api/types";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, message, role, sse } from "../../test/fakeBackend";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

it("窗口重新获得焦点时显示无正文桌面图片和文件，使用本角色附件端点", async () => {
  let messages: Message[] = [];
  const saved = role({ id: "r1", name: "桌面推送" });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages } }),
  });
  render(<App />);
  await screen.findByRole("heading", { name: "桌面推送", level: 1 });
  await waitFor(() => expect(screen.getByRole("textbox", { name: "消息内容" })).toBeEnabled());
  messages = [message({ id: "pushed", role: "assistant", content: "", messageType: "desktop_push", metadata: {
    channel: "desktop", deliveryStatus: "sent", attachments: [
      { id: "image-id", name: "推送.png", mediaType: "image/png", size: 32, kind: "image" },
      { id: "file-id", name: "报告.txt", mediaType: "text/plain", size: 12, kind: "file" },
    ],
  } })];
  act(() => window.dispatchEvent(new Event("focus")));
  const image = await screen.findByRole("img", { name: "推送.png" });
  expect(image).toHaveAttribute("src", "/api/roles/r1/attachments/image-id");
  expect(image.closest("a")).toHaveAttribute("href", "/api/roles/r1/attachments/image-id");
  expect(screen.getByRole("link", { name: "报告.txt" })).toHaveAttribute("href", "/api/roles/r1/attachments/file-id");
  expect(screen.queryByText("（无内容）")).not.toBeInTheDocument();
});


it("shows committed media while the tool turn continues and keeps the new final reply ID", async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  const emit = (type: string, payload: unknown) => controller.enqueue(new TextEncoder().encode(sse(type, payload)));
  const saved = role({ id: 'r1', name: '文件输出' });
  const session = { sessionKey: 'role:r1', roleId: 'r1' };
  fakeBackend({
    'GET /api/roles': () => ({ body: { roles: [saved] } }),
    'GET /api/roles/r1/session': () => ({ body: { session, messages: [] } }),
    'POST /api/roles/r1/messages': () => {
      emit('user_message_accepted', { runId: 'run', sequence: 1, message: message({ id: 'u', role: 'user', content: '发送文件' }) });
      emit('assistant_generation_started', { runId: 'run', sequence: 2, messageId: 'old-placeholder' });
      emit('assistant_message_committed', { runId: 'run', sequence: 3, message: message({ id: 'push', role: 'assistant', content: '', sequence: 5, messageType: 'desktop_push', metadata: { attachments: [{ id: 'owned', name: 'report.html', kind: 'file' }] } }) });
      return { stream, headers: { 'X-Meido-Run-Id': 'run' } };
    },
  });
  render(<App />);
  await screen.findByRole('heading', { name: '文件输出' });
  const user = userEvent.setup();
  await user.type(screen.getByRole('textbox', { name: '消息内容' }), '发送文件{Enter}');
  expect(await screen.findByRole('link', { name: 'report.html' })).toHaveAttribute('href', '/api/roles/r1/attachments/owned');
  expect(screen.getByRole('button', { name: '停止回复' })).toBeEnabled();
  act(() => {
    emit('assistant_generation_started', { runId: 'run', sequence: 4, messageId: 'actual-final', messageSequence: 7 });
    emit('assistant_delta', { runId: 'run', sequence: 5, messageId: 'actual-final', delta: '报告已附上' });
    emit('assistant_completed', { runId: 'run', sequence: 6, message: message({ id: 'actual-final', role: 'assistant', content: '报告已附上', sequence: 7 }) });
  });
  expect(await screen.findByText('报告已附上')).toBeVisible();
  await waitFor(() => expect(screen.queryByText('正在回复…')).toBeNull());
  expect(screen.getAllByRole('link', { name: 'report.html' })).toHaveLength(1);
});
