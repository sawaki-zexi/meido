import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import type { Message } from "../../api/types";
import { App } from "../../App";
import { fakeBackend, message, role } from "../../test/fakeBackend";

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
